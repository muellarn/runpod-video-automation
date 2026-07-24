from argparse import Namespace
from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from runpod_video_automation import cli
from runpod_video_automation.cli import build_parser
from runpod_video_automation.config import ModelFile, Profile
from runpod_video_automation.render_metadata import (
    build_shot_inputs,
    build_start_image_inputs,
    fingerprint,
    write_shot_metadata,
    write_start_image_metadata,
)


def test_cli_parser_includes_scene_command() -> None:
    args = build_parser().parse_args(
        ["scene", "scene.json", "--plan"]
    )

    assert args.command == "scene"
    assert args.start_image_workflow == "workflows/z-image-turbo-start-image-api.json"


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
    monkeypatch.setattr(
        cli.Profile,
        "load",
        lambda path: (_ for _ in ()).throw(AssertionError("Pod setup must not begin")),
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
    profile = Profile(
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
        models=(),
        start_image_models=(),
    )
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
                            "model_type": "z_image_turbo",
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
    profile = Profile(
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
        models=(wan_model,),
        start_image_models=(start_model,),
    )
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
    profile = Profile(
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
        models=(),
        start_image_models=(),
    )
    workflow = {"1": {"class_type": "Test", "inputs": {}}}
    loaded_scene = cli.Scene.load(manifest)
    metadata_inputs = build_shot_inputs(
        loaded_scene,
        loaded_scene.shots[0],
        index=1,
        start_image=start_image,
        profile=profile,
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
                            "model_type": "z_image_turbo",
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
    profile = Profile(
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
        models=(wan_model,),
        start_image_models=(start_model,),
    )
    workflow = {
        node: {"class_type": "Test", "inputs": {}}
        for node in ("6", "7", "47", "50", "52", "57", "58")
    }
    uploads: list[Path] = []
    ensured_models: list[tuple[ModelFile, ...]] = []
    loaded_scene = cli.Scene.load(manifest)
    start_inputs = build_start_image_inputs(
        loaded_scene.shots[0],
        index=1,
        profile=profile,
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
    profile = Profile(
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
        models=(),
        start_image_models=(),
    )
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

        def ensure_models(self, models: tuple[object, ...]) -> None:
            assert models == ()
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
    profile = Profile(
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
        models=(),
        start_image_models=(),
    )
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
