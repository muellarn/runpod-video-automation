import json
from pathlib import Path

import pytest

from runpod_video_automation.scene import (
    Scene,
    build_start_image_workflow,
    build_shot_workflow,
    concatenate_webm,
    duration_to_frames,
    extract_last_frame,
)
from runpod_video_automation.workflow import load_workflow


def _write_scene(tmp_path: Path, shots: list[dict[str, object]]) -> Path:
    manifest = {
        "title": "Test Scene",
        "global_prompt": "Same adult character, cinematic lighting",
        "negative_prompt": "flicker, identity drift",
        "width": 832,
        "height": 480,
        "fps": 16,
        "steps": 20,
        "transition_step": 10,
        "cfg": 3.5,
        "shots": shots,
    }
    path = tmp_path / "scene.json"
    path.write_text(json.dumps(manifest))
    return path


def test_scene_loads_keyframes_and_continuation(tmp_path: Path) -> None:
    start = tmp_path / "start.png"
    end = tmp_path / "end.png"
    start.write_bytes(b"start")
    end.write_bytes(b"end")
    path = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "start_image": "start.png",
                "end_image": "end.png",
                "duration_seconds": 5,
                "prompt": "Walks toward the window",
                "camera": "slow tracking shot",
                "seed": 123,
            },
            {
                "name": "Close-up",
                "duration_seconds": 3,
                "prompt": "Turns toward the camera",
            },
        ],
    )

    scene = Scene.load(path)

    assert scene.shots[0].start_image == start
    assert scene.shots[0].end_image == end
    assert scene.shots[0].frames == 81
    assert scene.shots[1].start_image is None
    assert scene.shots[1].frames == 49


def test_scene_requires_first_start_image(tmp_path: Path) -> None:
    path = _write_scene(
        tmp_path,
        [{"name": "Opening", "prompt": "Walks into the room"}],
    )

    with pytest.raises(ValueError, match="first scene shot requires"):
        Scene.load(path)


def test_scene_supports_generated_first_start_image(tmp_path: Path) -> None:
    path = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Sits on the bed",
                "generate_start_image": {
                    "model_type": "z_image_turbo",
                    "prompt": "Adult woman standing in a bedroom",
                    "width": 864,
                    "height": 1200,
                    "seed": 1234,
                },
            }
        ],
    )

    scene = Scene.load(path)

    generation = scene.shots[0].generate_start_image
    assert generation is not None
    assert generation.prompt == "Adult woman standing in a bedroom"
    assert generation.model_type == "z_image_turbo"
    assert generation.checkpoint == "cyberrealisticZImage_v50.safetensors"
    assert generation.width == 864
    assert generation.height == 1200
    assert generation.steps == 8
    assert generation.cfg == 1.0


def test_scene_rejects_file_and_generated_start_image(tmp_path: Path) -> None:
    start = tmp_path / "start.png"
    start.write_bytes(b"start")
    path = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Sits on the bed",
                "start_image": "start.png",
                "generate_start_image": {"prompt": "Adult woman in a bedroom"},
            }
        ],
    )

    with pytest.raises(ValueError, match="cannot set both"):
        Scene.load(path)


def test_build_start_image_workflow_applies_generation_settings(
    tmp_path: Path,
) -> None:
    scene = Scene.load(
        _write_scene(
            tmp_path,
            [
                {
                    "name": "Opening Portrait",
                    "prompt": "Sits on the bed",
                    "generate_start_image": {
                        "model_type": "z_image_turbo",
                        "prompt": "Adult woman standing in a bedroom",
                        "checkpoint": "cyberrealisticZImage_v50.safetensors",
                        "width": 864,
                        "height": 1200,
                        "seed": 1234,
                        "steps": 10,
                        "cfg": 1.0,
                    },
                }
            ],
        )
    )
    generation = scene.shots[0].generate_start_image
    assert generation is not None
    root = Path(__file__).resolve().parents[1]
    base = load_workflow(root / "workflows/z-image-turbo-start-image-api.json")

    workflow = build_start_image_workflow(
        base,
        generation,
        shot_number=1,
        shot_name=scene.shots[0].name,
    )

    assert workflow["4"]["inputs"]["unet_name"] == (
        "cyberrealisticZImage_v50.safetensors"
    )
    assert workflow["5"]["inputs"] == {
        "width": 864,
        "height": 1200,
        "batch_size": 1,
    }
    assert workflow["6"]["inputs"]["text"] == "Adult woman standing in a bedroom"
    assert workflow["7"]["class_type"] == "ConditioningZeroOut"
    assert workflow["3"]["inputs"]["seed"] == 1234
    assert workflow["3"]["inputs"]["steps"] == 10
    assert workflow["3"]["inputs"]["cfg"] == 1.0
    assert workflow["9"]["inputs"]["filename_prefix"] == (
        "generated/001-opening-portrait"
    )


def test_scene_rejects_negative_prompt_for_z_image(tmp_path: Path) -> None:
    path = _write_scene(
        tmp_path,
        [
            {
                "name": "Opening",
                "prompt": "Sits on the bed",
                "generate_start_image": {
                    "model_type": "z_image_turbo",
                    "prompt": "Adult woman standing in a bedroom",
                    "negative_prompt": "blurry",
                },
            }
        ],
    )

    with pytest.raises(ValueError, match="does not support negative prompting"):
        Scene.load(path)


def test_build_shot_workflow_applies_scene_direction(tmp_path: Path) -> None:
    start = tmp_path / "start.png"
    end = tmp_path / "end.png"
    start.write_bytes(b"start")
    end.write_bytes(b"end")
    scene = Scene.load(
        _write_scene(
            tmp_path,
            [
                {
                    "name": "Window Turn",
                    "start_image": "start.png",
                    "end_image": "end.png",
                    "duration_seconds": 5,
                    "prompt": "Turns toward the camera",
                    "camera": "slow dolly-in",
                    "negative_prompt": "camera shake",
                    "seed": 987,
                    "cfg": 4.0,
                }
            ],
        )
    )
    root = Path(__file__).resolve().parents[1]
    base = load_workflow(root / "workflows/wan22-i2v-14b-api.json")

    workflow = build_shot_workflow(
        base,
        scene,
        scene.shots[0],
        shot_number=1,
        start_image_name="start-upload.png",
        end_image_name="end-upload.png",
    )

    assert workflow["6"]["inputs"]["text"] == (
        "Same adult character, cinematic lighting, Current action: Turns toward "
        "the camera, Camera: slow dolly-in"
    )
    assert workflow["7"]["inputs"]["text"] == (
        "flicker, identity drift, camera shake"
    )
    assert workflow["50"]["class_type"] == "WanFirstLastFrameToVideo"
    assert workflow["50"]["inputs"]["end_image"] == ["53", 0]
    assert workflow["53"]["inputs"]["image"] == "end-upload.png"
    assert workflow["57"]["inputs"]["noise_seed"] == 987
    assert workflow["57"]["inputs"]["cfg"] == 4.0
    assert workflow["47"]["inputs"]["filename_prefix"] == "scene/001-window-turn"


def test_previous_end_state_is_labeled_in_next_shot_prompt(tmp_path: Path) -> None:
    start = tmp_path / "start.png"
    start.write_bytes(b"start")
    scene = Scene.load(
        _write_scene(
            tmp_path,
            [
                {
                    "name": "Opening",
                    "start_image": "start.png",
                    "prompt": "Walks to the chair",
                    "end_state": "She is seated on the chair",
                },
                {
                    "name": "Follow Up",
                    "prompt": "She looks toward the window",
                },
            ],
        )
    )
    root = Path(__file__).resolve().parents[1]
    base = load_workflow(root / "workflows/wan22-i2v-14b-api.json")

    workflow = build_shot_workflow(
        base,
        scene,
        scene.shots[1],
        shot_number=2,
        start_image_name="continuation.png",
        starting_state=scene.shots[0].end_state,
    )

    assert scene.shots[0].end_state == "She is seated on the chair"
    assert "Starting state: She is seated on the chair" in workflow["6"]["inputs"][
        "text"
    ]
    assert "Current action: She looks toward the window" in workflow["6"]["inputs"][
        "text"
    ]


def test_duration_to_frames_uses_wan_frame_interval() -> None:
    assert duration_to_frames(5, 16) == 81
    assert duration_to_frames(3, 16) == 49


def test_extract_last_frame_decodes_through_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        "runpod_video_automation.scene.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    video = tmp_path / "shot.webm"
    destination = tmp_path / "frames" / "continuation.png"

    extract_last_frame(video, destination)

    command, kwargs = calls[0]
    assert command[command.index("-sseof") + 1] == "-1"
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert command[command.index("-update") + 1] == "1"
    assert "-frames:v" not in command
    assert command[-1] == str(destination)
    assert kwargs["check"] is True


def test_concatenate_webm_uses_ffmpeg_concat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object], str]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        manifest_path = Path(command[command.index("-i") + 1])
        calls.append((command, kwargs, manifest_path.read_text()))

    monkeypatch.setattr("runpod_video_automation.scene.subprocess.run", fake_run)
    destination = tmp_path / "final" / "scene.webm"

    concatenate_webm([tmp_path / "one.webm", tmp_path / "two.webm"], destination)

    command, kwargs, manifest = calls[0]
    assert command[-3:] == ["-c", "copy", str(destination)]
    assert "pipe:0" not in command
    assert "one.webm" in manifest
    assert "two.webm" in manifest
    assert kwargs["check"] is True
