from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from runpod_video_automation import cli
from runpod_video_automation.cli import build_parser
from runpod_video_automation.config import ModelFile, Profile, WorkflowPreset


WAN_MODEL = ModelFile("https://example.test/wan", "models/wan", 1)
START_MODEL = ModelFile("https://example.test/start", "models/start", 1)
QWEN_MODEL = ModelFile("https://example.test/qwen", "models/qwen", 1)


def _profile() -> Profile:
    return Profile(
        name="qwen-cli-test",
        image="worker:image",
        data_center_id="US-MO-1",
        volume_name="models",
        volume_size_gb=1,
        gpu_type_ids=("GPU",),
        min_ram_per_gpu=1,
        min_vcpu_per_gpu=1,
        container_disk_gb=1,
        max_hourly_cost=1,
        model_groups={
            "video": (WAN_MODEL,),
            "start": (START_MODEL,),
            "qwen": (QWEN_MODEL,),
        },
        workflows={
            "video": WorkflowPreset(Path("wan.json"), "wan22_i2v", ("video",)),
            "start_image": WorkflowPreset(
                Path("start.json"), "z_image_turbo", ("start",)
            ),
            "qwen": WorkflowPreset(
                Path("qwen.json"), "qwen_image_edit_2511", ("qwen",)
            ),
        },
    )


class _FakeComfy:
    runtime_metadata = {"pod_id": "pod-test"}

    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str]] = []
        self.queued: list[dict[str, object]] = []

    def upload_image(self, path: Path, remote_name: str) -> str:
        self.uploads.append((path, remote_name))
        return remote_name

    def queue_and_wait(self, workflow: dict[str, object], **_: object):
        self.queued.append(workflow)
        return "prompt", workflow

    def download_outputs(self, history: dict[str, object], output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)
        if history["kind"] == "image":
            output = output_dir / (
                f"generated-{history['shot_number']}-{history['role']}.png"
            )
            output.write_bytes(f"generated-{history['role']}".encode())
        else:
            output = output_dir / "shot.webm"
            output.write_bytes(b"video")
        return [output]


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    clients: list[_FakeComfy],
    installed_models: list[tuple[ModelFile, ...]],
) -> None:
    monkeypatch.setattr(cli.Profile, "load", lambda path: _profile())
    monkeypatch.setattr(cli, "load_workflow", lambda path: {})
    monkeypatch.setattr(
        cli,
        "build_image_workflow",
        lambda adapter, workflow, generation, **kwargs: {
            "kind": "image",
            "role": kwargs["role"],
            "adapter": adapter,
            "shot_number": kwargs["shot_number"],
            "reference_names": tuple(kwargs["reference_names"]),
        },
    )
    monkeypatch.setattr(
        cli,
        "build_shot_workflow",
        lambda adapter, workflow, scene, shot, **kwargs: {
            "kind": "video",
            "end_image_name": kwargs["end_image_name"],
        },
    )
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        cli,
        "extract_last_frame",
        lambda source, destination: destination.write_bytes(b"continuation"),
    )
    monkeypatch.setattr(
        cli,
        "concatenate_webm",
        lambda videos, destination: destination.write_bytes(b"assembled"),
    )

    @contextmanager
    def worker(args, profile, *, models=None):
        client = _FakeComfy()
        clients.append(client)
        installed_models.append(models)
        yield client

    monkeypatch.setattr(cli, "_worker_session", worker)


def _write_scene(tmp_path: Path, shots: list[dict[str, object]]) -> Path:
    manifest = tmp_path / "scene.json"
    manifest.write_text(
        json.dumps(
            {
                "title": "Qwen CLI",
                "global_prompt": "A fictional adult character",
                "shots": shots,
            }
        )
    )
    return manifest


def _qwen_end(references: list[dict[str, object]]) -> dict[str, object]:
    return {
        "workflow": "qwen",
        "adapter": "qwen_image_edit_2511",
        "prompt": "Preserve the subject and change the pose",
        "reference_images": references,
    }


def test_parser_accepts_generic_generated_image_flags() -> None:
    generated = build_parser().parse_args(
        ["scene", "scene.json", "--apply", "--generated-images-only"]
    )
    approved = build_parser().parse_args(
        ["scene", "scene.json", "--apply", "--approve-generated-images"]
    )
    preview = build_parser().parse_args(
        ["scene", "scene.json", "--apply", "--preview-generated-images"]
    )

    assert generated.generated_images_only is True
    assert approved.approve_generated_images is True
    assert preview.preview_generated_images is True


def test_preview_images_use_prior_generated_end_in_isolated_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Sits",
                "generate_start_image": {"prompt": "Adult character standing"},
                "generate_end_image": _qwen_end([{"source": "current_start"}]),
            },
            {
                "name": "Follow Up",
                "prompt": "Turns",
                "generate_end_image": _qwen_end([{"source": "current_start"}]),
            },
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    monkeypatch.setattr(
        cli,
        "extract_last_frame",
        lambda *_: (_ for _ in ()).throw(AssertionError("video frame extracted")),
    )
    output = tmp_path / "out"

    cli.render_scene(
        build_parser().parse_args(
            [
                "scene",
                str(manifest),
                "--apply",
                "--preview-generated-images",
                "--shot",
                "2",
                "--output",
                str(output),
            ]
        )
    )

    preview = output / "000-image-preview"
    assert [workflow["kind"] for workflow in clients[0].queued] == [
        "image",
        "image",
        "image",
    ]
    assert models == [(START_MODEL, QWEN_MODEL)]
    assert WAN_MODEL not in models[0]
    opening = preview / "001-opening"
    follow_up = preview / "002-follow-up"
    assert (opening / "start.png").is_file()
    assert (opening / "start.metadata.json").is_file()
    assert (opening / "end.png").is_file()
    assert (opening / "end.metadata.json").is_file()
    assert (follow_up / "end.png").is_file()
    assert (follow_up / "end.metadata.json").is_file()
    assert clients[0].uploads[-1][0] == opening / "end.png"
    assert (preview / "render-manifest.json").is_file()
    render_manifest = json.loads((preview / "render-manifest.json").read_text())
    assert render_manifest["end_images"][0]["output"]["path"] == (
        "001-opening/end.png"
    )
    assert not (preview / "000-generated-start-image").exists()
    assert not (preview / "000-generated-end-image").exists()
    assert not (output / "000-generated-start-image").exists()
    assert not (output / "000-generated-end-image").exists()

    monkeypatch.setattr(
        cli,
        "_worker_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("matching preview resume must not start a worker")
        ),
    )
    cli.render_scene(
        build_parser().parse_args(
            [
                "scene",
                str(manifest),
                "--apply",
                "--preview-generated-images",
                "--resume",
                "--shot",
                "2",
                "--output",
                str(output),
            ]
        )
    )

    with pytest.raises(ValueError, match="does not match the current scene"):
        cli.render_scene(
            build_parser().parse_args(
                [
                    "scene",
                    str(manifest),
                    "--apply",
                    "--approve-generated-images",
                    "--shot",
                    "1",
                    "--output",
                    str(preview),
                ]
            )
        )


def test_preview_regeneration_prunes_old_shot_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Sits",
                "generate_start_image": {"prompt": "Adult character standing"},
                "generate_end_image": _qwen_end([{"source": "current_start"}]),
            }
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    output = tmp_path / "out"
    command = [
        "scene",
        str(manifest),
        "--apply",
        "--preview-generated-images",
        "--output",
        str(output),
    ]
    cli.render_scene(build_parser().parse_args(command))
    preview = output / "000-image-preview"
    assert (preview / "001-opening" / "start.png").is_file()
    assert (preview / "001-opening" / "end.png").is_file()

    changed = json.loads(manifest.read_text())
    changed["shots"][0]["name"] = "Renamed"
    manifest.write_text(json.dumps(changed))
    cli.render_scene(build_parser().parse_args(command))

    assert not (preview / "001-opening").exists()
    assert (preview / "001-renamed" / "start.png").is_file()
    assert (preview / "001-renamed" / "start.metadata.json").is_file()
    assert (preview / "001-renamed" / "end.png").is_file()
    assert (preview / "001-renamed" / "end.metadata.json").is_file()


def test_preview_uses_generated_end_for_shot_continuation_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Sits",
                "generate_start_image": {"prompt": "Adult character standing"},
                "generate_end_image": _qwen_end([{"source": "current_start"}]),
            },
            {
                "name": "POV Reset",
                "prompt": "Looks up",
                "generate_start_image": {
                    "workflow": "qwen",
                    "adapter": "qwen_image_edit_2511",
                    "prompt": "Change the camera",
                    "reference_images": [
                        {"source": "shot_continuation", "shot": 1}
                    ],
                },
                "generate_end_image": _qwen_end([{"source": "current_start"}]),
            },
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    output = tmp_path / "out"

    cli.render_scene(
        build_parser().parse_args(
            [
                "scene",
                str(manifest),
                "--apply",
                "--preview-generated-images",
                "--shot",
                "2",
                "--output",
                str(output),
            ]
        )
    )

    preview = output / "000-image-preview"
    assert [
        (workflow["shot_number"], workflow["role"])
        for workflow in clients[0].queued
    ] == [(1, "start"), (1, "end"), (2, "start"), (2, "end")]
    assert any(
        path == preview / "001-opening" / "end.png"
        and "002-start-reference-001" in remote_name
        for path, remote_name in clients[0].uploads
    )
    assert (preview / "002-pov-reset" / "start.png").is_file()
    assert (preview / "002-pov-reset" / "end.png").is_file()


def test_qwen_generated_end_uses_exact_reference_order_and_feeds_wan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    start = tmp_path / "start.png"
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    for path, value in ((start, b"start"), (first, b"first"), (second, b"second")):
        path.write_bytes(value)
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Turns",
                "start_image": "start.png",
                "generate_end_image": _qwen_end(
                    [
                        {"path": "first.png"},
                        {"source": "current_start"},
                        {"path": "second.png"},
                    ]
                ),
            }
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)

    args = build_parser().parse_args(
        ["scene", str(manifest), "--apply", "--output", str(tmp_path / "out")]
    )
    cli.render_scene(args)

    client = clients[0]
    image_workflow, video_workflow = client.queued
    reference_uploads = client.uploads[:3]
    assert [path.read_bytes() for path, _ in reference_uploads] == [
        b"first",
        b"start",
        b"second",
    ]
    assert len({name for _, name in reference_uploads}) == 3
    assert image_workflow["reference_names"] == tuple(
        name for _, name in reference_uploads
    )
    generated_end = tmp_path / "out/000-generated-end-image/generated-1-end.png"
    assert client.uploads[-1][0] == generated_end
    assert video_workflow["end_image_name"] == client.uploads[-1][1]
    assert models == [(WAN_MODEL, QWEN_MODEL)]


def test_generated_images_only_uses_no_wan_models_or_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Turns",
                "generate_start_image": {
                    "prompt": "Adult character standing",
                },
                "generate_end_image": _qwen_end([{"source": "current_start"}]),
            }
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda command: (_ for _ in ()).throw(AssertionError("ffmpeg checked")),
    )
    monkeypatch.setattr(
        cli,
        "extract_last_frame",
        lambda *_: (_ for _ in ()).throw(AssertionError("ffmpeg used")),
    )

    args = build_parser().parse_args(
        [
            "scene",
            str(manifest),
            "--apply",
            "--generated-images-only",
            "--output",
            str(tmp_path / "out"),
        ]
    )
    cli.render_scene(args)

    assert [workflow["kind"] for workflow in clients[0].queued] == ["image", "image"]
    assert models == [(START_MODEL, QWEN_MODEL)]
    assert WAN_MODEL not in models[0]


def test_approved_qwen_image_does_not_provision_qwen_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    start = tmp_path / "start.png"
    reference = tmp_path / "reference.png"
    start.write_bytes(b"start")
    reference.write_bytes(b"reference")
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Turns",
                "start_image": "start.png",
                "generate_end_image": _qwen_end([{"path": "reference.png"}]),
            }
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    output = tmp_path / "out"
    cli.render_scene(
        build_parser().parse_args(
            [
                "scene",
                str(manifest),
                "--apply",
                "--generated-images-only",
                "--output",
                str(output),
            ]
        )
    )
    clients.clear()
    models.clear()

    cli.render_scene(
        build_parser().parse_args(
            [
                "scene",
                str(manifest),
                "--apply",
                "--approve-generated-images",
                "--output",
                str(output),
            ]
        )
    )

    assert models == [(WAN_MODEL,)]
    assert [workflow["kind"] for workflow in clients[0].queued] == ["video"]


def test_missing_unselected_prior_continuation_fails_before_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    start = tmp_path / "start.png"
    start.write_bytes(b"start")
    manifest = _write_scene(
        tmp_path,
        [
            {"name": "Opening", "prompt": "Walks", "start_image": "start.png"},
            {
                "name": "Second",
                "prompt": "Turns",
                "generate_end_image": _qwen_end(
                    [{"source": "shot_continuation", "shot": 1}]
                ),
            },
        ],
    )
    monkeypatch.setattr(cli.Profile, "load", lambda path: _profile())
    monkeypatch.setattr(cli, "load_workflow", lambda path: {})
    monkeypatch.setattr(
        cli,
        "_worker_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker must not start")
        ),
    )
    args = build_parser().parse_args(
        [
            "scene",
            str(manifest),
            "--apply",
            "--generated-images-only",
            "--shot",
            "2",
            "--output",
            str(tmp_path / "out"),
        ]
    )

    with pytest.raises(ValueError, match="continuation.*unavailable"):
        cli.render_scene(args)


@pytest.mark.parametrize("backfill", [False, True])
def test_partial_shot_start_reference_uses_inherited_predecessor_continuation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backfill: bool
) -> None:
    start = tmp_path / "start.png"
    start.write_bytes(b"start")
    manifest = _write_scene(
        tmp_path,
        [
            {"name": "Opening", "prompt": "Walks", "start_image": "start.png"},
            {"name": "Inherited Start", "prompt": "Turns"},
            {
                "name": "Consumer",
                "prompt": "Stops",
                "generate_end_image": _qwen_end(
                    [{"source": "shot_start", "shot": 2}]
                ),
            },
        ],
    )
    output = tmp_path / "out"
    predecessor_continuation = output / "001-opening/continuation.png"
    predecessor_continuation.parent.mkdir(parents=True)
    predecessor_continuation.write_bytes(b"inherited-start")
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    command = [
        "scene",
        str(manifest),
        "--shot",
        "3",
        "--output",
        str(output),
    ]
    if backfill:
        generated = output / "000-generated-end-image/003-consumer-end_00001_.png"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"generated-end")
        command.append("--backfill-metadata")
    else:
        command.extend(["--apply", "--generated-images-only"])

    cli.render_scene(build_parser().parse_args(command))

    if backfill:
        metadata = json.loads(
            (output / "000-generated-end-image/003-consumer.metadata.json").read_text()
        )
        assert metadata["inputs"]["references"][0]["sha256"] == cli.sha256_file(
            predecessor_continuation
        )
        assert not clients
    else:
        assert clients[0].uploads[0][0] == predecessor_continuation


def test_full_matching_qwen_resume_does_not_start_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    start = tmp_path / "start.png"
    reference = tmp_path / "reference.png"
    start.write_bytes(b"start")
    reference.write_bytes(b"reference")
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Turns",
                "start_image": "start.png",
                "generate_end_image": _qwen_end([{"path": "reference.png"}]),
            }
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    output = tmp_path / "out"
    base_args = ["scene", str(manifest), "--apply", "--output", str(output)]
    cli.render_scene(build_parser().parse_args(base_args))
    monkeypatch.setattr(
        cli,
        "_worker_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("matching resume must not start a worker")
        ),
    )

    cli.render_scene(build_parser().parse_args([*base_args, "--resume"]))


def test_changed_qwen_reference_invalidates_generated_image_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    start = tmp_path / "start.png"
    reference = tmp_path / "reference.png"
    start.write_bytes(b"start")
    reference.write_bytes(b"first")
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Turns",
                "start_image": "start.png",
                "generate_end_image": _qwen_end([{"path": "reference.png"}]),
            }
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    output = tmp_path / "out"
    base_args = [
        "scene",
        str(manifest),
        "--apply",
        "--generated-images-only",
        "--output",
        str(output),
    ]
    cli.render_scene(build_parser().parse_args(base_args))
    clients.clear()
    models.clear()
    reference.write_bytes(b"changed")

    cli.render_scene(build_parser().parse_args([*base_args, "--resume"]))

    assert len(clients) == 1
    assert [workflow["kind"] for workflow in clients[0].queued] == ["image"]
    assert models == [(QWEN_MODEL,)]


def _dependency_scene(tmp_path: Path) -> tuple[Path, Path]:
    start = tmp_path / "start.png"
    reference = tmp_path / "dependency-reference.png"
    start.write_bytes(b"start")
    reference.write_bytes(b"dependency-reference")
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Dependency",
                "prompt": "Walks",
                "start_image": "start.png",
                "generate_end_image": _qwen_end(
                    [{"path": "dependency-reference.png"}]
                ),
            },
            {
                "name": "Consumer",
                "prompt": "Turns",
                "generate_end_image": _qwen_end(
                    [{"source": "shot_end", "shot": 1}]
                ),
            },
        ],
    )
    return manifest, reference


def test_stale_unselected_generated_dependency_regenerates_before_consumer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, reference = _dependency_scene(tmp_path)
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    output = tmp_path / "out"
    common = [
        "scene",
        str(manifest),
        "--apply",
        "--generated-images-only",
        "--output",
        str(output),
    ]
    cli.render_scene(build_parser().parse_args(common))
    clients.clear()
    models.clear()
    reference.write_bytes(b"changed-dependency-reference")
    changed_scene = json.loads(manifest.read_text())
    changed_scene["shots"][0]["generate_end_image"]["prompt"] = (
        "Changed dependency prompt"
    )
    manifest.write_text(json.dumps(changed_scene))

    cli.render_scene(
        build_parser().parse_args([*common, "--resume", "--shot", "2"])
    )

    assert [
        (workflow["shot_number"], workflow["role"])
        for workflow in clients[0].queued
    ] == [(1, "end"), (2, "end")]
    assert models == [(QWEN_MODEL,)]


def test_matching_unselected_generated_dependency_reuses_without_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest, _ = _dependency_scene(tmp_path)
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    output = tmp_path / "out"
    common = [
        "scene",
        str(manifest),
        "--apply",
        "--generated-images-only",
        "--output",
        str(output),
    ]
    cli.render_scene(build_parser().parse_args(common))
    monkeypatch.setattr(
        cli,
        "_worker_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("matching dependency closure must not start a worker")
        ),
    )

    cli.render_scene(
        build_parser().parse_args([*common, "--resume", "--shot", "2"])
    )


def test_rerendered_predecessor_invalidates_inherited_current_start_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    start = tmp_path / "start.png"
    start.write_bytes(b"start")
    manifest = _write_scene(
        tmp_path,
        [
            {"name": "Opening", "prompt": "Walks", "start_image": "start.png"},
            {
                "name": "Consumer",
                "prompt": "Turns",
                "generate_end_image": _qwen_end([{"source": "current_start"}]),
            },
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    output = tmp_path / "out"
    common = ["scene", str(manifest), "--apply", "--output", str(output)]
    cli.render_scene(build_parser().parse_args(common))
    clients.clear()
    models.clear()
    changed_scene = json.loads(manifest.read_text())
    changed_scene["shots"][0]["prompt"] = "Runs"
    manifest.write_text(json.dumps(changed_scene))

    cli.render_scene(build_parser().parse_args([*common, "--resume"]))

    assert [workflow["kind"] for workflow in clients[0].queued] == [
        "video",
        "image",
        "video",
    ]
    assert models == [(WAN_MODEL, QWEN_MODEL)]


def test_partial_selection_resolves_unselected_inherited_shot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first_start = tmp_path / "first-start.png"
    third_start = tmp_path / "third-start.png"
    first_start.write_bytes(b"first-start")
    third_start.write_bytes(b"third-start")
    manifest = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Walks",
                "start_image": "first-start.png",
            },
            {"name": "Inherited", "prompt": "Pauses"},
            {
                "name": "Consumer",
                "prompt": "Turns",
                "start_image": "third-start.png",
                "generate_end_image": _qwen_end(
                    [{"source": "shot_start", "shot": 2}]
                ),
            },
        ],
    )
    clients: list[_FakeComfy] = []
    models: list[tuple[ModelFile, ...]] = []
    _install_fakes(monkeypatch, clients, models)
    output = tmp_path / "out"

    cli.render_scene(
        build_parser().parse_args(
            [
                "scene",
                str(manifest),
                "--apply",
                "--shots",
                "1,3",
                "--output",
                str(output),
            ]
        )
    )

    continuation = output / "001-opening/continuation.png"
    assert any(
        path == continuation and "end-reference-001" in remote_name
        for path, remote_name in clients[0].uploads
    )
