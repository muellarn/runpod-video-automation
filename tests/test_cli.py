from argparse import Namespace
from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from runpod_video_automation import cli
from runpod_video_automation.adapters import resolve_start_image_generation
from runpod_video_automation.cli import build_parser
from runpod_video_automation.config import ModelFile, Profile, WorkflowPreset
from runpod_video_automation.prompt_refiner.refinement import RefinementResult
from runpod_video_automation.render_metadata import (
    build_shot_inputs,
    build_start_image_inputs,
    fingerprint,
    write_shot_metadata,
    write_start_image_metadata,
)


def _profile(
    video_models: tuple[ModelFile, ...] = (),
    start_image_models: tuple[ModelFile, ...] = (),
) -> Profile:
    return Profile(
        name="test",
        image="image",
        data_center_id="US-MO-1",
        volume_name="volume",
        volume_size_gb=1,
        gpu_type_ids=("GPU",),
        min_ram_per_gpu=1,
        min_vcpu_per_gpu=1,
        container_disk_gb=1,
        max_hourly_cost=1.0,
        model_groups={"video": video_models, "start-image": start_image_models},
        workflows={
            "video": WorkflowPreset(
                Path("wan.json"), "wan22_i2v", ("video",)
            ),
            "start_image": WorkflowPreset(
                Path("start.json"), "z_image_turbo", ("start-image",)
            ),
        },
    )


def test_cli_parser_includes_scene_command() -> None:
    args = build_parser().parse_args(
        ["scene", "scene.json", "--plan"]
    )

    assert args.command == "scene"
    assert args.start_image_workflow is None
    assert args.output is None


def test_cli_parser_includes_prompt_refiner_commands() -> None:
    refine_args = build_parser().parse_args(
        ["refine", "scene.json", "--apply", "--force"]
    )
    chat_args = build_parser().parse_args(
        [
            "chat",
            "--apply",
            "--no-browser",
            "--scene-context",
            "--max-output-tokens",
            "40000",
            "--duration-seconds",
            "5",
        ]
    )
    setup_args = build_parser().parse_args(["setup", "--apply", "--include-refiner"])
    scene_args = build_parser().parse_args(
        ["scene", "scene.json", "--apply", "--refine-prompts"]
    )

    assert refine_args.func is cli.refine
    assert refine_args.force is True
    assert chat_args.func is cli.chat
    assert chat_args.duration_seconds == 5
    assert chat_args.scene_context is True
    assert chat_args.max_output_tokens == 40000
    assert setup_args.include_refiner is True
    assert scene_args.refine_prompts is True


def test_chat_output_token_limit_defaults_and_validation() -> None:
    profile = Namespace(context_size=65536)

    assert cli._chat_max_output_tokens(
        Namespace(scene_context=True, max_output_tokens=None), profile
    ) == 32768
    assert cli._chat_max_output_tokens(
        Namespace(scene_context=True, max_output_tokens=40000), profile
    ) == 40000
    with pytest.raises(ValueError, match="requires --scene-context"):
        cli._chat_max_output_tokens(
            Namespace(scene_context=False, max_output_tokens=1000), profile
        )
    with pytest.raises(ValueError, match="below the model context size"):
        cli._chat_max_output_tokens(
            Namespace(scene_context=True, max_output_tokens=65536), profile
        )


def test_scene_refinement_cache_hit_does_not_start_pod(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Cached",
                "global_prompt": "Adult character",
                "shots": [
                    {
                        "name": "Opening",
                        "prompt": "Walks",
                        "generate_start_image": {"prompt": "Adult character"},
                    }
                ],
            }
        )
    )
    scene = cli.Scene.load(manifest)
    result = RefinementResult(
        scene=scene,
        document=json.loads(manifest.read_text()),
        manifest_path=tmp_path / "output/prompt-refinement/refined.json",
        provenance={"cache_key": "cached"},
        cache_hit=True,
    )
    rendered: list[RefinementResult | None] = []
    monkeypatch.setattr(cli.PromptRefinerProfile, "load", lambda path: object())
    monkeypatch.setattr(cli, "load_cached_refinement", lambda **kwargs: result)
    monkeypatch.setattr(
        cli,
        "_remote_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("A cache hit must not start a Pod")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_render_scene_effective",
        lambda args, scene_path, effective_scene, **kwargs: rendered.append(
            kwargs.get("refinement")
        ),
    )
    args = build_parser().parse_args(
        ["scene", str(manifest), "--plan", "--refine-prompts"]
    )

    cli.render_scene(args)

    assert rendered == [result]


def test_scene_refinement_reuses_same_remote_session_for_render(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Refined",
                "global_prompt": "Adult character",
                "shots": [
                    {
                        "name": "Opening",
                        "prompt": "Walks",
                        "generate_start_image": {"prompt": "Adult character"},
                    }
                ],
            }
        )
    )
    scene = cli.Scene.load(manifest)
    result = RefinementResult(
        scene=scene,
        document=json.loads(manifest.read_text()),
        manifest_path=tmp_path / "output/prompt-refinement/refined.json",
        provenance={"cache_key": "new"},
        cache_hit=False,
    )
    artifact = ModelFile("https://example.test/model", "models/model.gguf", 1)

    class RefinerProfile:
        artifacts = (artifact,)

    remote = object()
    remote_details = (remote, "pod-1", 1.0, {})
    events: list[object] = []

    @contextmanager
    def fake_remote_session(args, profile, *, models, prepare_comfy=True):
        assert models == (artifact,)
        assert prepare_comfy is False
        events.append("remote-enter")
        yield remote_details
        events.append("remote-exit")

    def fake_refine_with_remote(**kwargs):
        assert kwargs["remote"] is remote
        events.append("refine")
        return result

    def fake_render(*args, **kwargs):
        assert kwargs["remote_details"] is remote_details
        assert kwargs["refinement"] is result
        events.append("render")

    monkeypatch.setattr(cli.PromptRefinerProfile, "load", lambda path: RefinerProfile())
    monkeypatch.setattr(cli, "load_cached_refinement", lambda **kwargs: None)
    monkeypatch.setattr(cli.Profile, "load", lambda path: _profile())
    monkeypatch.setattr(cli, "_remote_session", fake_remote_session)
    monkeypatch.setattr(cli, "_refine_with_remote", fake_refine_with_remote)
    monkeypatch.setattr(cli, "_render_scene_effective", fake_render)
    args = build_parser().parse_args(
        ["scene", str(manifest), "--apply", "--refine-prompts"]
    )

    cli.render_scene(args)

    assert events == ["remote-enter", "refine", "render", "remote-exit"]


def test_scene_refinement_requires_restart_for_existing_pod(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Existing Pod",
                "global_prompt": "Adult character",
                "shots": [
                    {
                        "name": "Opening",
                        "prompt": "Walks",
                        "generate_start_image": {"prompt": "Adult character"},
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(cli.PromptRefinerProfile, "load", lambda path: object())
    monkeypatch.setattr(cli, "load_cached_refinement", lambda **kwargs: None)
    args = build_parser().parse_args(
        [
            "scene",
            str(manifest),
            "--apply",
            "--refine-prompts",
            "--pod-id",
            "pod-1",
        ]
    )

    with pytest.raises(ValueError, match="requires --restart"):
        cli.render_scene(args)


def test_scene_rejects_refiner_options_without_opt_in(tmp_path: Path) -> None:
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "No Refinement",
                "global_prompt": "Adult character",
                "shots": [
                    {
                        "name": "Opening",
                        "prompt": "Walks",
                        "generate_start_image": {"prompt": "Adult character"},
                    }
                ],
            }
        )
    )
    args = build_parser().parse_args(
        ["scene", str(manifest), "--plan", "--force"]
    )

    with pytest.raises(ValueError, match="require --refine-prompts"):
        cli.render_scene(args)


def test_setup_installs_selected_models_without_comfyui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_model = ModelFile("https://example.test/video", "models/video", 1)
    image_model = ModelFile("https://example.test/image", "models/image", 1)
    profile = _profile((video_model,), (image_model,))
    installed: list[tuple[ModelFile, ...]] = []

    @contextmanager
    def fake_remote_session(args, loaded_profile, *, models):
        assert loaded_profile is profile
        installed.append(models)
        yield object(), "pod-new", 1.0, {}

    monkeypatch.setattr(cli.Profile, "load", lambda path: profile)
    monkeypatch.setattr(cli, "_remote_session", fake_remote_session)
    args = build_parser().parse_args(
        ["setup", "--apply", "--model-group", "start-image", "--stop-pod"]
    )

    args.func(args)

    assert installed == [(image_model,)]


def test_setup_can_install_prompt_refiner_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ModelFile("https://example.test/runtime", "tools/runtime", 1)
    model = ModelFile("https://example.test/model", "models/model.gguf", 2)
    installed: list[tuple[ModelFile, ...]] = []

    class RefinerProfile:
        artifacts = (runtime, model)

    @contextmanager
    def fake_remote_session(args, profile, *, models):
        installed.append(models)
        yield object(), "pod-new", 1.0, {}

    monkeypatch.setattr(cli.Profile, "load", lambda path: _profile())
    monkeypatch.setattr(cli.PromptRefinerProfile, "load", lambda path: RefinerProfile())
    monkeypatch.setattr(cli, "_remote_session", fake_remote_session)
    args = build_parser().parse_args(["setup", "--apply", "--include-refiner"])

    args.func(args)

    assert installed == [(runtime, model)]


def test_scene_project_directory_uses_local_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scene_path = project / "scene.json"
    scene_path.write_text("{}")

    resolved = cli._scene_path(str(project))

    assert resolved == scene_path.resolve()
    assert cli._scene_output_root(resolved, None) == project.resolve() / "output"


def test_input_snapshot_is_content_addressed(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"image")

    snapshot = cli._snapshot_input(
        source,
        tmp_path / "output",
        index=2,
        role="end",
    )

    assert snapshot.parent.name == "000-inputs"
    assert snapshot.name.startswith("002-end-")
    assert snapshot.read_bytes() == b"image"


def test_metadata_backfill_reuses_existing_outputs_with_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Backfill Scene",
                "global_prompt": "Same fictional adult character",
                "shots": [
                    {
                        "name": "Opening",
                        "prompt": "Walks forward",
                        "generate_start_image": {
                            "prompt": "Adult character standing in a room"
                        },
                    },
                    {"name": "Follow Up", "prompt": "Turns around"},
                ],
            }
        )
    )
    output_root = tmp_path / "output"
    generated = output_root / "000-generated-start-image/001-opening_00001_.png"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"start")
    for index, name in ((1, "opening"), (2, "follow-up")):
        shot_dir = output_root / f"{index:03d}-{name}"
        shot_dir.mkdir()
        (shot_dir / f"{index:03d}-{name}_00001_.webm").write_bytes(b"video")
        (shot_dir / "continuation.png").write_bytes(f"frame-{index}".encode())

    profile = _profile()
    workflow = {"1": {"class_type": "Test", "inputs": {}}}
    monkeypatch.setattr(cli.Profile, "load", lambda path: profile)
    monkeypatch.setattr(cli, "load_workflow", lambda path: workflow)
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        cli,
        "_worker_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Metadata backfill and matching resume must not start a Pod")
        ),
    )
    monkeypatch.setattr(
        cli,
        "concatenate_webm",
        lambda videos, destination: destination.write_bytes(b"assembled"),
    )
    backfill_args = build_parser().parse_args(
        [
            "scene",
            str(manifest),
            "--backfill-metadata",
            "--output",
            str(output_root),
        ]
    )

    cli.render_scene(backfill_args)

    start_metadata = output_root / "000-generated-start-image/001-opening.metadata.json"
    first_metadata = output_root / "001-opening/metadata.json"
    second_metadata = output_root / "002-follow-up/metadata.json"
    assert start_metadata.is_file()
    assert first_metadata.is_file()
    assert second_metadata.is_file()
    assert json.loads(first_metadata.read_text())["render"]["backfilled"] is True

    legacy_partial = json.loads(second_metadata.read_text())
    legacy_partial["inputs"]["runtime"]["start_image_workflow"] = None
    legacy_partial["fingerprint"] = fingerprint(legacy_partial["inputs"])
    second_metadata.write_text(json.dumps(legacy_partial))

    selected_resume_args = build_parser().parse_args(
        [
            "scene",
            str(manifest),
            "--apply",
            "--resume",
            "--shot",
            "2",
            "--output",
            str(output_root),
        ]
    )
    cli.render_scene(selected_resume_args)

    resume_args = build_parser().parse_args(
        [
            "scene",
            str(manifest),
            "--apply",
            "--resume",
            "--output",
            str(output_root),
        ]
    )
    cli.render_scene(resume_args)

    assert (output_root / "backfill-scene.webm").is_file()


def test_metadata_backfill_rejects_ambiguous_video_files(tmp_path: Path) -> None:
    shot_dir = tmp_path / "001-shot"
    shot_dir.mkdir()
    (shot_dir / "one.webm").write_bytes(b"one")
    (shot_dir / "two.webm").write_bytes(b"two")

    with pytest.raises(ValueError, match="multiple matching files"):
        cli._single_existing_output(
            shot_dir,
            prefix="",
            suffixes={".webm"},
            label="video for shot 1",
        )


def test_metadata_backfill_generated_image_prefix_requires_delimiter(
    tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    (generated_dir / "001-opening_00001_.png").write_bytes(b"wrong")

    result = cli._single_existing_output(
        generated_dir,
        prefix="001-open_",
        suffixes={".png"},
        label="generated start image",
    )

    assert result is None


def test_metadata_backfill_validates_all_shots_before_writing_sidecars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    start = tmp_path / "start.png"
    start.write_bytes(b"start")
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Incomplete",
                "global_prompt": "Adult character",
                "shots": [
                    {"name": "One", "prompt": "A", "start_image": "start.png"},
                    {"name": "Two", "prompt": "B"},
                ],
            }
        )
    )
    output_root = tmp_path / "output"
    first = output_root / "001-one"
    second = output_root / "002-two"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "one.webm").write_bytes(b"one")
    (first / "continuation.png").write_bytes(b"frame")
    (second / "two.webm").write_bytes(b"two")
    monkeypatch.setattr(cli.Profile, "load", lambda path: _profile())
    monkeypatch.setattr(
        cli,
        "load_workflow",
        lambda path: {"1": {"class_type": "Test", "inputs": {}}},
    )
    args = build_parser().parse_args(
        [
            "scene",
            str(manifest),
            "--backfill-metadata",
            "--output",
            str(output_root),
        ]
    )

    with pytest.raises(ValueError, match="continuation image is missing"):
        cli.render_scene(args)

    assert not (first / "metadata.json").exists()
    assert not (output_root / "scene.snapshot.json").exists()


def test_scene_parser_accepts_existing_pod_restart() -> None:
    args = build_parser().parse_args(
        ["scene", "scene.json", "--apply", "--pod-id", "pod-1", "--restart"]
    )

    assert args.pod_id == "pod-1"
    assert args.restart is True


def test_scene_parser_accepts_start_image_only() -> None:
    args = build_parser().parse_args(
        ["scene", "scene.json", "--apply", "--start-image-only"]
    )

    assert args.start_image_only is True


@pytest.mark.parametrize("shot", ["0", "-1"])
def test_scene_rejects_non_positive_shot_numbers(tmp_path: Path, shot: str) -> None:
    start = tmp_path / "start.png"
    start.write_bytes(b"image")
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Invalid selection",
                "global_prompt": "Adult character",
                "shots": [
                    {"name": "Opening", "prompt": "A", "start_image": "start.png"}
                ],
            }
        )
    )
    args = build_parser().parse_args(
        ["scene", str(manifest), "--plan", f"--shot={shot}"]
    )

    with pytest.raises(ValueError, match="between 1 and 1"):
        cli.render_scene(args)


def test_partial_scene_plan_handles_unselected_generated_start_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Partial",
                "global_prompt": "Same adult character",
                "shots": [
                    {
                        "name": "Generated Opening",
                        "prompt": "A",
                        "generate_start_image": {"prompt": "Adult character"},
                    },
                    {"name": "Continuation", "prompt": "B"},
                ],
            }
        )
    )
    monkeypatch.setattr(cli.Profile, "load", lambda path: _profile())
    args = build_parser().parse_args(
        ["scene", str(manifest), "--plan", "--shot", "2"]
    )

    cli.render_scene(args)

    assert "generated start image (unselected shot)" in capsys.readouterr().out


def test_run_resolves_workflow_from_current_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow = tmp_path / "custom.json"
    workflow.write_text("{}")
    loaded: list[Path] = []

    class ExpectedStop(Exception):
        pass

    def fake_load(path: Path):
        loaded.append(path)
        raise ExpectedStop

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Profile, "load", lambda path: _profile())
    monkeypatch.setattr(cli, "load_workflow", fake_load)
    args = build_parser().parse_args(["run", "custom.json", "--apply"])

    with pytest.raises(ExpectedStop):
        cli.run(args)

    assert loaded == [workflow.resolve()]


def test_scene_parser_accepts_stop_pod_and_rejects_keep_pod_combination() -> None:
    args = build_parser().parse_args(
        ["scene", "scene.json", "--apply", "--stop-pod"]
    )

    assert args.stop_pod is True
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["scene", "scene.json", "--apply", "--stop-pod", "--keep-pod"]
        )


def test_scene_parser_accepts_single_shot() -> None:
    args = build_parser().parse_args(
        ["scene", "scene.json", "--apply", "--shot", "2"]
    )

    assert args.shot == 2


def test_scene_parser_accepts_shot_ranges() -> None:
    args = build_parser().parse_args(
        ["scene", "scene.json", "--apply", "--shots", "1,3-5,3"]
    )

    assert args.shots == (1, 3, 4, 5)


def test_retry_operation_retries_after_cleanup(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    def operation() -> str:
        events.append("run")
        if events.count("run") < 2:
            raise RuntimeError("temporary")
        return "ok"

    result = cli._retry_operation(
        "test operation",
        2,
        operation,
        before_retry=lambda: events.append("cleanup"),
    )

    assert result == "ok"
    assert events == ["run", "cleanup", "run"]


def test_cleanup_all_does_not_parse_runpod_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[str] = []

    class FakeRunPodClient:
        def __init__(self, api_key: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def list_pods(self):
            return [
                {
                    "id": "pod-1",
                    "name": "runpod-video-test",
                    "createdAt": "not-a-timestamp",
                }
            ]

        def terminate_pod(self, pod_id: str) -> None:
            terminated.append(pod_id)

    monkeypatch.setenv("RUNPOD_API_KEY", "key")
    monkeypatch.setattr(cli, "RunPodClient", FakeRunPodClient)

    cli.cleanup(Namespace(all=True, max_age_hours=2.0))

    assert terminated == ["pod-1"]


def test_cleanup_parses_runpod_utc_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[str] = []

    class FakeRunPodClient:
        def __init__(self, api_key: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def list_pods(self):
            return [
                {
                    "id": "pod-old",
                    "name": "runpod-video-test",
                    "createdAt": "2000-01-01 00:00:00.000 +0000 UTC",
                }
            ]

        def terminate_pod(self, pod_id: str) -> None:
            terminated.append(pod_id)

    monkeypatch.setenv("RUNPOD_API_KEY", "key")
    monkeypatch.setattr(cli, "RunPodClient", FakeRunPodClient)

    cli.cleanup(Namespace(all=False, max_age_hours=2.0))

    assert terminated == ["pod-old"]


def test_schedule_idle_stop_launches_detached_watchdog(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        pid = 1234

    def fake_popen(command: list[str], **kwargs: object):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    args = Namespace(idle_stop_minutes=5.0, start_timeout=900)

    cli._schedule_idle_stop("pod-1", tmp_path / "id_ed25519", args)

    command, kwargs = calls[0]
    assert "runpod_video_automation.idle_watchdog" in command
    assert command[command.index("--idle-minutes") + 1] == "5.0"
    assert kwargs["start_new_session"] is True


def test_single_dependent_shot_requires_existing_continuation(
    monkeypatch, tmp_path: Path
) -> None:
    start_image = tmp_path / "start.png"
    start_image.write_bytes(b"png")
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Test Scene",
                "global_prompt": "Same fictional adult character",
                "width": 576,
                "height": 800,
                "shots": [
                    {"name": "Opening", "start_image": "start.png", "prompt": "A"},
                    {"name": "Follow Up", "prompt": "B"},
                ],
            }
        )
    )
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cli.Profile, "load", lambda path: _profile())
    monkeypatch.setattr(
        cli,
        "_worker_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Pod setup must not begin")
        ),
    )
    args = Namespace(
        manifest=str(manifest),
        workflow="wan.json",
        start_image_workflow="start.json",
        profile=None,
        output=str(tmp_path / "output"),
        plan=False,
        apply=True,
        start_image_only=False,
        shot=2,
        restart=False,
        pod_id=None,
        keep_pod=False,
        stop_pod=False,
        ssh_key=None,
        start_timeout=30,
        workflow_timeout=60,
    )

    with pytest.raises(ValueError, match="continuation.png"):
        cli.render_scene(args)


def test_single_shot_uses_previous_and_writes_own_continuation(
    monkeypatch, tmp_path: Path
) -> None:
    start_image = tmp_path / "start.png"
    start_image.write_bytes(b"png")
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Test Scene",
                "global_prompt": "Same fictional adult character",
                "width": 576,
                "height": 800,
                "shots": [
                    {"name": "Opening", "start_image": "start.png", "prompt": "A"},
                    {"name": "Follow Up", "prompt": "B"},
                ],
            }
        )
    )
    output_root = tmp_path / "output"
    previous_continuation = output_root / "001-opening/continuation.png"
    previous_continuation.parent.mkdir(parents=True)
    previous_continuation.write_bytes(b"previous")
    profile = _profile()
    workflow = {
        node: {"class_type": "Test", "inputs": {}}
        for node in ("6", "7", "47", "50", "52", "57", "58")
    }
    uploads: list[Path] = []
    extracted: list[tuple[Path, Path]] = []

    class FakeComfyClient:
        def upload_image(self, path: Path, remote_name: str) -> str:
            uploads.append(path)
            return remote_name

        def queue_and_wait(self, value, **_: object):
            return "prompt", {"outputs": {}}

        def download_outputs(self, history, output_dir: Path):
            output = output_dir / "shot.webm"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"webm")
            return [output]

    @contextmanager
    def fake_worker_session(args, loaded_profile, *, models=None):
        yield FakeComfyClient()

    def fake_extract(source: Path, destination: Path) -> None:
        extracted.append((source, destination))
        destination.write_bytes(b"continuation")

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cli.Profile, "load", lambda path: profile)
    monkeypatch.setattr(cli, "load_workflow", lambda path: workflow)
    monkeypatch.setattr(cli, "_worker_session", fake_worker_session)
    monkeypatch.setattr(cli, "extract_last_frame", fake_extract)
    monkeypatch.setattr(
        cli,
        "concatenate_webm",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("A single selected shot must not assemble the whole scene")
        ),
    )
    args = Namespace(
        manifest=str(manifest),
        workflow="wan.json",
        start_image_workflow="start.json",
        profile=None,
        output=str(output_root),
        plan=False,
        apply=True,
        start_image_only=False,
        shot=2,
        restart=False,
        pod_id="pod-1",
        keep_pod=True,
        stop_pod=False,
        ssh_key=None,
        start_timeout=30,
        workflow_timeout=60,
    )

    cli.render_scene(args)

    assert uploads == [previous_continuation]
    assert extracted == [
        (
            output_root / "002-follow-up/shot.webm",
            output_root / "002-follow-up/continuation.png",
        )
    ]


def test_scene_start_image_only_skips_video_rendering(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Test Scene",
                "global_prompt": "Same fictional adult character",
                "width": 576,
                "height": 800,
                "shots": [
                    {
                        "name": "Opening",
                        "prompt": "Standing in a room",
                        "generate_start_image": {
                            "adapter": "z_image_turbo",
                            "prompt": "Adult woman standing in a room",
                            "width": 864,
                            "height": 1200,
                            "seed": 123,
                        },
                    }
                ],
            }
        )
    )
    wan_model = ModelFile("https://example.test/wan", "models/unet/wan", 1)
    start_model = ModelFile("https://example.test/image", "models/unet/image", 1)
    profile = _profile((wan_model,), (start_model,))
    workflow = {
        node: {"class_type": "Test", "inputs": {}}
        for node in ("3", "4", "5", "6", "7", "9")
    }
    loaded_workflows: list[Path] = []
    ensured_models: list[tuple[ModelFile, ...]] = []
    queued_workflows: list[dict[str, object]] = []

    class FakeComfyClient:
        def queue_and_wait(self, value, **_: object):
            queued_workflows.append(value)
            return "prompt", {"outputs": {}}

        def download_outputs(self, history, output_dir: Path):
            output = output_dir / "start.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"png")
            return [output]

        def upload_image(self, *_: object) -> str:
            raise AssertionError("Start-image-only must not queue a video")

    @contextmanager
    def fake_worker_session(args, loaded_profile, *, models=None):
        assert loaded_profile is profile
        ensured_models.append(models)
        yield FakeComfyClient()

    def fake_load_workflow(path: Path):
        loaded_workflows.append(path)
        return workflow

    monkeypatch.setattr(cli.Profile, "load", lambda path: profile)
    monkeypatch.setattr(cli, "load_workflow", fake_load_workflow)
    monkeypatch.setattr(cli, "_worker_session", fake_worker_session)
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    monkeypatch.setattr(
        cli,
        "concatenate_webm",
        lambda *_: (_ for _ in ()).throw(AssertionError("Video must not be assembled")),
    )
    args = Namespace(
        manifest=str(manifest),
        workflow="wan.json",
        start_image_workflow="start.json",
        profile=None,
        output=str(tmp_path / "output"),
        plan=False,
        apply=True,
        start_image_only=True,
        shot=None,
        restart=False,
        pod_id="pod-1",
        keep_pod=True,
        ssh_key=None,
        start_timeout=30,
        workflow_timeout=60,
    )

    cli.render_scene(args)

    assert loaded_workflows == [cli.PROJECT_ROOT / "start.json"]
    assert ensured_models == [(start_model,)]
    assert len(queued_workflows) == 1
    assert (tmp_path / "output/000-generated-start-image/start.png").is_file()
    assert (
        tmp_path / "output/000-generated-start-image/001-opening.metadata.json"
    ).is_file()
    assert (tmp_path / "output/render-manifest.json").is_file()


def test_resume_assembles_completed_shots_without_starting_pod(
    monkeypatch, tmp_path: Path
) -> None:
    start_image = tmp_path / "start.png"
    start_image.write_bytes(b"png")
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Resume Scene",
                "global_prompt": "Same fictional adult character",
                "width": 576,
                "height": 800,
                "shots": [
                    {"name": "Opening", "start_image": "start.png", "prompt": "A"}
                ],
            }
        )
    )
    output_root = tmp_path / "output"
    video = output_root / "001-opening/shot.webm"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"webm")
    continuation = video.parent / "continuation.png"
    continuation.write_bytes(b"continuation")
    profile = _profile()
    workflow = {"1": {"class_type": "Test", "inputs": {}}}
    loaded_scene = cli.Scene.load(manifest)
    start_snapshot = cli._snapshot_input(
        start_image,
        output_root,
        index=1,
        role="start",
    )
    metadata_inputs = build_shot_inputs(
        loaded_scene,
        loaded_scene.shots[0],
        index=1,
        start_image=start_snapshot,
        profile=profile,
        video_workflow=profile.select_workflow("video"),
        video_workflow_sha256=fingerprint(workflow),
        start_workflow_sha256=None,
    )
    write_shot_metadata(
        video.parent / "metadata.json",
        inputs=metadata_inputs,
        video=video,
        continuation=continuation,
        output_root=output_root,
        runtime={},
        elapsed_seconds=1,
    )
    continuation.unlink()
    extracted: list[tuple[Path, Path]] = []
    assembled: list[list[Path]] = []

    def fake_extract(source: Path, destination: Path) -> None:
        extracted.append((source, destination))
        destination.write_bytes(b"continuation")

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cli, "extract_last_frame", fake_extract)
    monkeypatch.setattr(cli.Profile, "load", lambda path: profile)
    monkeypatch.setattr(cli, "load_workflow", lambda path: workflow)
    monkeypatch.setattr(
        cli,
        "_worker_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Pod must not start")
        ),
    )
    monkeypatch.setattr(
        cli,
        "concatenate_webm",
        lambda videos, destination: assembled.append(videos),
    )
    args = Namespace(
        manifest=str(manifest),
        workflow="wan.json",
        start_image_workflow="start.json",
        profile=None,
        output=str(output_root),
        plan=False,
        apply=True,
        start_image_only=False,
        approve_start_images=False,
        resume=True,
        shot=None,
        shots=None,
        restart=False,
        pod_id=None,
        keep_pod=False,
        stop_pod=False,
        idle_stop_minutes=None,
        retries=2,
        ssh_key=None,
        start_timeout=30,
        workflow_timeout=60,
    )

    cli.render_scene(args)

    assert extracted == [(video, video.parent / "continuation.png")]
    assert assembled == [[video]]


def test_approved_start_image_skips_image_generation(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Approved Scene",
                "global_prompt": "Same fictional adult character",
                "width": 576,
                "height": 800,
                "shots": [
                    {
                        "name": "Opening",
                        "prompt": "A",
                        "generate_start_image": {
                            "adapter": "z_image_turbo",
                            "prompt": "Adult woman standing in a room",
                        },
                    }
                ],
            }
        )
    )
    output_root = tmp_path / "output"
    approved = output_root / "000-generated-start-image/001-opening_00001_.png"
    approved.parent.mkdir(parents=True)
    approved.write_bytes(b"png")
    wan_model = ModelFile("https://example.test/wan", "models/unet/wan", 1)
    start_model = ModelFile("https://example.test/start", "models/unet/start", 1)
    profile = _profile((wan_model,), (start_model,))
    workflow = {
        node: {"class_type": "Test", "inputs": {}}
        for node in ("6", "7", "47", "50", "52", "57", "58")
    }
    uploads: list[Path] = []
    ensured_models: list[tuple[ModelFile, ...]] = []
    loaded_scene = cli.Scene.load(manifest)
    generation = loaded_scene.shots[0].generate_start_image
    assert generation is not None
    start_selection = profile.select_workflow("start_image")
    start_inputs = build_start_image_inputs(
        loaded_scene.shots[0],
        index=1,
        profile=profile,
        generation=resolve_start_image_generation(
            generation, start_selection.adapter, start_selection.defaults
        ),
        start_workflow=start_selection,
        start_workflow_sha256=fingerprint(workflow),
    )
    write_start_image_metadata(
        output_root / "000-generated-start-image/001-opening.metadata.json",
        inputs=start_inputs,
        image=approved,
        output_root=output_root,
        runtime={},
        elapsed_seconds=1,
    )

    class FakeComfyClient:
        def upload_image(self, path: Path, remote_name: str) -> str:
            uploads.append(path)
            return remote_name

        def queue_and_wait(self, value, **_: object):
            return "prompt", {"outputs": {}}

        def download_outputs(self, history, output_dir: Path):
            output = output_dir / "shot.webm"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"webm")
            return [output]

    @contextmanager
    def fake_worker_session(args, loaded_profile, *, models=None):
        ensured_models.append(models)
        yield FakeComfyClient()

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cli.Profile, "load", lambda path: profile)
    monkeypatch.setattr(cli, "load_workflow", lambda path: workflow)
    monkeypatch.setattr(cli, "_worker_session", fake_worker_session)
    monkeypatch.setattr(
        cli,
        "extract_last_frame",
        lambda source, destination: destination.write_bytes(b"continuation"),
    )
    monkeypatch.setattr(cli, "concatenate_webm", lambda videos, destination: None)
    args = Namespace(
        manifest=str(manifest),
        workflow="wan.json",
        start_image_workflow="start.json",
        profile=None,
        output=str(output_root),
        plan=False,
        apply=True,
        start_image_only=False,
        approve_start_images=True,
        resume=False,
        shot=None,
        shots=None,
        restart=False,
        pod_id="pod-1",
        keep_pod=True,
        stop_pod=False,
        idle_stop_minutes=None,
        retries=0,
        ssh_key=None,
        start_timeout=30,
        workflow_timeout=60,
    )

    cli.render_scene(args)

    assert uploads == [approved]
    assert ensured_models == [(wan_model,)]
    assert (output_root / "001-opening/metadata.json").is_file()
    assert (output_root / "render-manifest.json").is_file()


def test_resume_rerenders_changed_shot_and_dependent_successor(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    start_image = tmp_path / "start.png"
    start_image.write_bytes(b"start")
    manifest = tmp_path / "scene.json"
    old_manifest = {
        "title": "Dependency Scene",
        "global_prompt": "Same fictional adult character",
        "width": 576,
        "height": 800,
        "shots": [
            {"name": "Opening", "start_image": "start.png", "prompt": "Walks"},
            {"name": "Follow Up", "prompt": "Turns"},
        ],
    }
    manifest.write_text(json.dumps(old_manifest))
    output_root = tmp_path / "output"
    profile = _profile()
    workflow = {
        node: {"class_type": "Test", "inputs": {}}
        for node in ("6", "7", "47", "50", "52", "57", "58")
    }
    old_scene = cli.Scene.load(manifest)
    previous_continuation: Path | None = None
    for index, shot in enumerate(old_scene.shots, start=1):
        shot_dir = output_root / f"{index:03d}-{cli.slugify(shot.name)}"
        video = shot_dir / "old.webm"
        continuation = shot_dir / "continuation.png"
        shot_dir.mkdir(parents=True)
        video.write_bytes(f"old-video-{index}".encode())
        continuation.write_bytes(f"old-continuation-{index}".encode())
        inputs = build_shot_inputs(
            old_scene,
            shot,
            index=index,
            start_image=shot.start_image or previous_continuation,
            profile=profile,
            video_workflow=profile.select_workflow("video"),
            video_workflow_sha256=fingerprint(workflow),
            start_workflow_sha256=None,
        )
        write_shot_metadata(
            shot_dir / "metadata.json",
            inputs=inputs,
            video=video,
            continuation=continuation,
            output_root=output_root,
            runtime={},
            elapsed_seconds=1,
        )
        previous_continuation = continuation
    old_manifest["shots"][0]["prompt"] = "Walks faster"
    manifest.write_text(json.dumps(old_manifest))
    queued: list[dict[str, object]] = []

    class FakeComfyClient:
        runtime_metadata: dict[str, object] = {}

        def upload_image(self, path: Path, remote_name: str) -> str:
            return remote_name

        def queue_and_wait(self, value, **_: object):
            queued.append(value)
            return "prompt", {"outputs": {}}

        def download_outputs(self, history, output_dir: Path):
            output = output_dir / "new.webm"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"new-video")
            return [output]

    @contextmanager
    def fake_worker_session(args, loaded_profile, *, models=None):
        yield FakeComfyClient()

    def fake_extract(source: Path, destination: Path) -> None:
        destination.write_bytes(b"new-continuation")

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    monkeypatch.setattr(cli.Profile, "load", lambda path: profile)
    monkeypatch.setattr(cli, "load_workflow", lambda path: workflow)
    monkeypatch.setattr(cli, "_worker_session", fake_worker_session)
    monkeypatch.setattr(cli, "extract_last_frame", fake_extract)
    monkeypatch.setattr(cli, "concatenate_webm", lambda videos, destination: None)
    args = Namespace(
        manifest=str(manifest),
        workflow="wan.json",
        start_image_workflow="start.json",
        profile=None,
        output=str(output_root),
        plan=False,
        apply=True,
        start_image_only=False,
        approve_start_images=False,
        resume=True,
        shot=None,
        shots=None,
        restart=False,
        pod_id="pod-1",
        keep_pod=True,
        stop_pod=False,
        idle_stop_minutes=None,
        retries=0,
        ssh_key=None,
        start_timeout=30,
        workflow_timeout=60,
    )

    cli.render_scene(args)

    output = capsys.readouterr().out
    assert len(queued) == 2
    assert "inputs.prompts.shot" in output
    assert "dependency: shot 1 is being rendered again" in output


@pytest.mark.parametrize(
    ("keep_pod", "stop_pod", "initial_status", "cleanup_event"),
    [
        (True, False, "RUNNING", None),
        (False, True, "RUNNING", "stop:pod-1"),
        (True, False, "EXITED", None),
    ],
)
def test_worker_session_reuses_and_restarts_existing_pod(
    monkeypatch,
    tmp_path: Path,
    keep_pod: bool,
    stop_pod: bool,
    initial_status: str,
    cleanup_event: str | None,
) -> None:
    events: list[str] = []
    ssh_key = tmp_path / "id_ed25519"
    ssh_key.write_text("private")
    ssh_key.with_suffix(".pub").write_text("public")

    class FakeRunPodClient:
        def __init__(self, api_key: str) -> None:
            assert api_key == "key"

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def wait_until_running(self, pod_id: str, *, timeout_seconds: int):
            events.append(f"reuse:{pod_id}")
            return {
                "id": pod_id,
                "publicIp": "192.0.2.1",
                "portMappings": {"22": 22022},
            }

        def get_pod(self, pod_id: str):
            return {"id": pod_id, "desiredStatus": initial_status}

        def start_pod(self, pod_id: str) -> None:
            events.append(f"start:{pod_id}")

        def create_pod(self, **_: object) -> None:
            raise AssertionError("Existing Pod must not create another Pod")

        def terminate_pod(self, pod_id: str) -> None:
            raise AssertionError(f"Reused Pod must remain running: {pod_id}")

        def stop_pod(self, pod_id: str) -> None:
            events.append(f"stop:{pod_id}")

    class FakeRemoteWorker:
        def __init__(self, *, host: str, port: int, ssh_key: Path) -> None:
            assert (host, port) == ("192.0.2.1", 22022)

        def wait_for_ssh(self) -> None:
            events.append("ssh")

        def ensure_models(self, models: tuple[object, ...], aliases: tuple = ()) -> None:
            assert models == ()
            assert aliases == ()
            events.append("models")

        @contextmanager
        def comfy_tunnel(self):
            yield "http://127.0.0.1:8188"

    class FakeComfyClient:
        def __init__(self, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:8188"

        def wait_until_ready(self):
            return {"devices": []}

        def interrupt_and_clear(self) -> None:
            events.append("restart")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setenv("RUNPOD_API_KEY", "key")
    monkeypatch.setattr(cli, "RunPodClient", FakeRunPodClient)
    monkeypatch.setattr(cli, "RemoteWorker", FakeRemoteWorker)
    monkeypatch.setattr(cli, "ComfyClient", FakeComfyClient)
    profile = _profile()
    args = Namespace(
        ssh_key=str(ssh_key),
        pod_id="pod-1",
        restart=True,
        start_timeout=30,
        keep_pod=keep_pod,
        stop_pod=stop_pod,
    )

    with cli._worker_session(args, profile):
        events.append("yield")

    expected = []
    if initial_status == "EXITED":
        expected.append("start:pod-1")
    expected.extend(["reuse:pod-1", "ssh", "models", "restart", "yield", "close"])
    if cleanup_event:
        expected.append(cleanup_event)
    assert events == expected
