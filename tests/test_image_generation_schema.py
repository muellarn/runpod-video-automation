import json
from pathlib import Path

import pytest

from runpod_video_automation.scene import (
    ImageGeneration,
    ImageReference,
    Scene,
    StartImageGeneration,
)


def _write_scene(tmp_path: Path, shots: list[dict[str, object]]) -> Path:
    path = tmp_path / "scene.json"
    path.write_text(
        json.dumps(
            {
                "title": "Image generation schema",
                "global_prompt": "Cinematic scene",
                "shots": shots,
            }
        )
    )
    return path


def _start_shot(**values: object) -> dict[str, object]:
    shot: dict[str, object] = {
        "name": "Opening",
        "prompt": "The subject enters",
        "generate_start_image": {"prompt": "Opening frame"},
    }
    shot.update(values)
    return shot


def test_image_generation_preserves_start_image_import_and_defaults(
    tmp_path: Path,
) -> None:
    scene = Scene.load(_write_scene(tmp_path, [_start_shot()]))

    generation = scene.shots[0].generate_start_image
    assert StartImageGeneration is ImageGeneration
    assert isinstance(generation, ImageGeneration)
    assert generation is not None
    assert generation.workflow == "start_image"
    assert generation.reference_images == ()
    assert generation.dimensions_explicit is False
    assert generation.width == 768
    assert generation.height == 768


def test_generation_tracks_explicit_dimensions(tmp_path: Path) -> None:
    scene = Scene.load(
        _write_scene(
            tmp_path,
            [
                _start_shot(
                    generate_start_image={
                        "prompt": "Opening frame",
                        "width": 1024,
                        "height": 768,
                    }
                )
            ],
        )
    )

    generation = scene.shots[0].generate_start_image
    assert generation is not None
    assert generation.width == 1024
    assert generation.height == 768
    assert generation.dimensions_explicit is True


def test_end_generation_defaults_to_image_edit_workflow(tmp_path: Path) -> None:
    scene = Scene.load(
        _write_scene(
            tmp_path,
            [
                _start_shot(
                    generate_end_image={
                        "prompt": "Ending frame",
                        "reference_images": [{"source": "current_start"}],
                    }
                )
            ],
        )
    )

    generation = scene.shots[0].generate_end_image
    assert generation is not None
    assert generation.workflow == "image_edit"


def test_generation_preserves_ordered_reference_descriptors(tmp_path: Path) -> None:
    identity = tmp_path / "identity.png"
    identity.write_bytes(b"identity")
    scene = Scene.load(
        _write_scene(
            tmp_path,
            [
                _start_shot(end_image="identity.png"),
                {
                    "name": "Follow-up",
                    "prompt": "The subject sits",
                    "generate_end_image": {
                        "workflow": "qwen_image_edit",
                        "prompt": "Compose the ending frame",
                        "reference_images": [
                            {"source": "current_start"},
                            {"path": "identity.png"},
                            {"source": "shot_start", "shot": 1},
                            {"source": "shot_end", "shot": 1},
                            {"source": "shot_continuation", "shot": 1},
                        ],
                    },
                },
            ],
        )
    )

    generation = scene.shots[1].generate_end_image
    assert generation is not None
    assert generation.workflow == "qwen_image_edit"
    assert generation.reference_images == (
        ImageReference(source="current_start"),
        ImageReference(path=identity.resolve()),
        ImageReference(source="shot_start", shot=1),
        ImageReference(source="shot_end", shot=1),
        ImageReference(source="shot_continuation", shot=1),
    )


@pytest.mark.parametrize(
    "descriptor",
    [
        "identity.png",
        {},
        {"path": None},
        {"path": "identity.png", "extra": True},
        {"path": "identity.png", "source": "current_start"},
        {"source": "shot_start"},
        {"source": "current_start", "shot": 1},
        {"source": "unknown"},
        {"source": "shot_start", "shot": 0},
        {"source": "shot_start", "shot": "1"},
    ],
)
def test_generation_rejects_malformed_reference_descriptors(
    tmp_path: Path, descriptor: object
) -> None:
    (tmp_path / "identity.png").write_bytes(b"identity")
    path = _write_scene(
        tmp_path,
        [
            _start_shot(),
            {
                "name": "Follow-up",
                "prompt": "The subject sits",
                "generate_end_image": {
                    "prompt": "Ending frame",
                    "reference_images": [descriptor],
                },
            },
        ],
    )

    with pytest.raises(ValueError, match="reference"):
        Scene.load(path)


def test_generation_rejects_missing_reference_file(tmp_path: Path) -> None:
    path = _write_scene(
        tmp_path,
        [
            _start_shot(
                generate_start_image={
                    "prompt": "Opening frame",
                    "reference_images": [{"path": "missing.png"}],
                }
            )
        ],
    )

    with pytest.raises(ValueError, match="not found"):
        Scene.load(path)


@pytest.mark.parametrize("shot_number", [2, 3])
def test_generation_rejects_current_or_future_shot_reference(
    tmp_path: Path, shot_number: int
) -> None:
    path = _write_scene(
        tmp_path,
        [
            _start_shot(),
            {
                "name": "Follow-up",
                "prompt": "The subject sits",
                "generate_end_image": {
                    "prompt": "Ending frame",
                    "reference_images": [
                        {"source": "shot_continuation", "shot": shot_number}
                    ],
                },
            },
        ],
    )

    with pytest.raises(ValueError, match="prior shot"):
        Scene.load(path)


def test_start_generation_rejects_current_start_reference(tmp_path: Path) -> None:
    path = _write_scene(
        tmp_path,
        [
            _start_shot(
                generate_start_image={
                    "prompt": "Opening frame",
                    "reference_images": [{"source": "current_start"}],
                }
            )
        ],
    )

    with pytest.raises(ValueError, match="current_start.*generate_start_image"):
        Scene.load(path)


def test_scene_rejects_file_and_generated_end_image(tmp_path: Path) -> None:
    end = tmp_path / "end.png"
    end.write_bytes(b"end")
    path = _write_scene(
        tmp_path,
        [
            _start_shot(
                end_image="end.png",
                generate_end_image={"prompt": "Ending frame"},
            )
        ],
    )

    with pytest.raises(ValueError, match="cannot set both end_image and"):
        Scene.load(path)


@pytest.mark.parametrize(
    "first_end",
    [
        None,
        {"generate_end_image": {"prompt": "Generated ending frame"}},
    ],
)
def test_shot_end_reference_requires_available_prior_end_image(
    tmp_path: Path, first_end: dict[str, object] | None
) -> None:
    first = _start_shot(**(first_end or {}))
    path = _write_scene(
        tmp_path,
        [
            first,
            {
                "name": "Follow-up",
                "prompt": "The subject sits",
                "generate_end_image": {
                    "prompt": "Ending frame",
                    "reference_images": [{"source": "shot_end", "shot": 1}],
                },
            },
        ],
    )

    if first_end is None:
        with pytest.raises(ValueError, match="shot_end.*no end image"):
            Scene.load(path)
    else:
        scene = Scene.load(path)
        assert scene.shots[1].generate_end_image is not None
