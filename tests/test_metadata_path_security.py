import json
from pathlib import Path
from typing import Callable

import pytest

from runpod_video_automation.render_metadata import (
    SCHEMA_VERSION,
    fingerprint,
    read_metadata,
    sha256_file,
    validate_generated_image_metadata,
    validate_shot_metadata,
    write_render_manifest,
)
from runpod_video_automation.scene import ImageGeneration, Scene, Shot


Validator = Callable[
    [dict[str, object], dict[str, object], Path],
    tuple[Path | None, list[str]],
]


def _generated_validator(
    metadata: dict[str, object],
    inputs: dict[str, object],
    output_root: Path,
) -> tuple[Path | None, list[str]]:
    return validate_generated_image_metadata(metadata, inputs, output_root)


def _shot_validator(
    metadata: dict[str, object],
    inputs: dict[str, object],
    output_root: Path,
) -> tuple[Path | None, list[str]]:
    return validate_shot_metadata(metadata, inputs, output_root)


def _metadata(
    validator: Validator,
    inputs: dict[str, object],
    output: dict[str, object],
    continuation: dict[str, object],
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": fingerprint(inputs),
        "inputs": inputs,
    }
    if validator is _shot_validator:
        value["outputs"] = {"video": output, "continuation": continuation}
    else:
        value["output"] = output
    return value


@pytest.mark.parametrize("validator", [_generated_validator, _shot_validator])
@pytest.mark.parametrize(
    ("path_kind", "difference"),
    [
        ("absolute", "absolute paths are not allowed"),
        ("traversal", "traversal escapes output_root"),
        ("symlink", "resolves outside output_root"),
    ],
)
def test_metadata_output_paths_cannot_escape_output_root(
    tmp_path: Path,
    validator: Validator,
    path_kind: str,
    difference: str,
) -> None:
    output_root = tmp_path / "output"
    output_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / "external.bin"
    external_file.write_bytes(b"external")
    if path_kind == "absolute":
        metadata_path = str(external_file)
    elif path_kind == "traversal":
        metadata_path = "../outside/external.bin"
    else:
        (output_root / "external-link").symlink_to(outside, target_is_directory=True)
        metadata_path = "external-link/external.bin"

    continuation = output_root / "nested" / "continuation.png"
    continuation.parent.mkdir()
    continuation.write_bytes(b"continuation")
    continuation_output = {
        "path": "nested/continuation.png",
        "sha256": sha256_file(continuation),
    }
    inputs: dict[str, object] = {"role": "start"}
    output = {
        "path": metadata_path,
        "sha256": sha256_file(external_file),
    }

    reused, differences = validator(
        _metadata(validator, inputs, output, continuation_output),
        inputs,
        output_root,
    )

    assert reused is None
    assert any(difference in value for value in differences)


@pytest.mark.parametrize("validator", [_generated_validator, _shot_validator])
def test_metadata_output_paths_preserve_relative_nested_paths(
    tmp_path: Path, validator: Validator
) -> None:
    output_root = tmp_path / "output"
    output = output_root / "nested" / "deeper" / "result.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"result")
    continuation = output_root / "nested" / "continuation.png"
    continuation.write_bytes(b"continuation")
    inputs: dict[str, object] = {"role": "start"}

    reused, differences = validator(
        _metadata(
            validator,
            inputs,
            {
                "path": "nested/deeper/result.bin",
                "sha256": sha256_file(output),
            },
            {
                "path": "nested/continuation.png",
                "sha256": sha256_file(continuation),
            },
        ),
        inputs,
        output_root,
    )

    assert reused == output
    assert differences == []


def _generation() -> ImageGeneration:
    return ImageGeneration(
        adapter=None,
        prompt="Frame",
        negative_prompt="",
        checkpoint=None,
        width=768,
        height=768,
        seed=1,
        steps=None,
        cfg=None,
        sampler_name=None,
        scheduler=None,
    )


def _shot(
    name: str,
    *,
    generate_start: bool = False,
    generate_end: bool = False,
) -> Shot:
    return Shot(
        name=name,
        prompt="Action",
        camera="",
        negative_prompt="",
        start_image=None,
        generate_start_image=_generation() if generate_start else None,
        end_image=None,
        generate_end_image=_generation() if generate_end else None,
        duration_seconds=1,
        frames=17,
        seed=1,
        cfg=1,
    )


def _scene(*shots: Shot) -> Scene:
    return Scene(
        title="Scene",
        global_prompt="",
        negative_prompt="",
        width=768,
        height=768,
        fps=16,
        steps=20,
        transition_step=10,
        cfg=1,
        shots=shots,
    )


def _write_sidecar(
    path: Path,
    *,
    index: int,
    name: str,
    role: str | None,
) -> dict[str, object]:
    inputs: dict[str, object] = {"shot": {"index": index, "name": name}}
    if role is not None:
        inputs["role"] = role
    metadata: dict[str, object] = {"inputs": inputs}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata))
    return metadata


def test_render_manifest_ignores_orphaned_and_unconfigured_sidecars(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    scene = _scene(
        _shot("Current", generate_start=True, generate_end=True),
        _shot("No Generation"),
    )
    start_dir = output_root / "000-generated-start-image"
    end_dir = output_root / "000-generated-end-image"
    current_start = _write_sidecar(
        start_dir / "001-current.metadata.json",
        index=1,
        name="Current",
        role=None,
    )
    current_end = _write_sidecar(
        end_dir / "001-current.metadata.json",
        index=1,
        name="Current",
        role="end",
    )
    orphan_paths = [
        start_dir / "001-old-name.metadata.json",
        end_dir / "003-removed.metadata.json",
        start_dir / "002-no-generation.metadata.json",
        end_dir / "002-no-generation.metadata.json",
    ]
    _write_sidecar(orphan_paths[0], index=1, name="Old Name", role="start")
    _write_sidecar(orphan_paths[1], index=3, name="Removed", role="end")
    _write_sidecar(orphan_paths[2], index=2, name="No Generation", role="start")
    _write_sidecar(orphan_paths[3], index=2, name="No Generation", role="end")
    scene_path = tmp_path / "scene.json"
    scene_path.write_text("{}")

    write_render_manifest(
        output_root,
        scene_path,
        scene,
        selected_shots=[1],
    )

    manifest = read_metadata(output_root / "render-manifest.json")
    assert manifest is not None
    assert manifest["start_images"] == [current_start]
    assert manifest["end_images"] == [current_end]
    assert all(path.is_file() for path in orphan_paths)


def test_render_manifest_rejects_sidecar_with_wrong_role(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    scene = _scene(_shot("Opening", generate_start=True))
    sidecar = output_root / "000-generated-start-image/001-opening.metadata.json"
    _write_sidecar(sidecar, index=1, name="Opening", role="end")
    scene_path = tmp_path / "scene.json"
    scene_path.write_text("{}")

    write_render_manifest(
        output_root,
        scene_path,
        scene,
        selected_shots=[1],
    )

    manifest = read_metadata(output_root / "render-manifest.json")
    assert manifest is not None
    assert manifest["start_images"] == []
    assert sidecar.is_file()
