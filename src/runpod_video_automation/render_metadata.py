from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runpod_video_automation.adapters import (
    ResolvedImageGeneration,
    ResolvedStartImageGeneration,
)
from runpod_video_automation.config import ModelFile, Profile, WorkflowSelection
from runpod_video_automation.scene import Scene, Shot, slugify


SCHEMA_VERSION = 2


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _model(model: ModelFile) -> dict[str, Any]:
    return {
        "path": model.path,
        "url": model.url,
        "size": model.size,
        "sha256": model.sha256,
    }


def _workflow(
    selection: WorkflowSelection,
    workflow_sha256: str,
    *,
    output_suffix: str | None = None,
) -> dict[str, Any]:
    value = {
        "name": selection.name,
        "adapter": selection.adapter,
        "sha256": workflow_sha256,
        "model_groups": list(selection.model_groups),
        "models": [_model(model) for model in selection.models],
        "defaults": selection.defaults,
    }
    if output_suffix is not None:
        value["output_suffix"] = output_suffix
    return value


def fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_shot_inputs(
    scene: Scene,
    shot: Shot,
    *,
    index: int,
    start_image: Path | None,
    profile: Profile,
    video_workflow: WorkflowSelection,
    video_workflow_sha256: str,
    video_output_suffix: str = ".webm",
    start_workflow: WorkflowSelection | None = None,
    start_workflow_sha256: str | None = None,
    generation: ResolvedStartImageGeneration | None = None,
    starting_state: str = "",
    end_image: Path | None = None,
    end_generation: ResolvedImageGeneration | None = None,
    end_workflow: WorkflowSelection | None = None,
    end_workflow_sha256: str | None = None,
    start_generation_fingerprint: str | None = None,
    end_generation_fingerprint: str | None = None,
    prompt_refinement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    positive_parts = [scene.global_prompt]
    if starting_state:
        positive_parts.append(f"Starting state: {starting_state}")
    if shot.prompt:
        positive_parts.append(f"Current action: {shot.prompt}")
    if shot.camera:
        positive_parts.append(f"Camera: {shot.camera}")
    positive_prompt = ", ".join(part for part in positive_parts if part)
    negative_prompt = ", ".join(
        part for part in (scene.negative_prompt, shot.negative_prompt) if part
    )
    if shot.start_image is not None:
        start_source = "start_image"
    elif shot.generate_start_image is not None:
        start_source = "generate_start_image"
    else:
        start_source = "previous_continuation"
    generation_metadata = None
    if generation is not None:
        generation_metadata = (
            generation.legacy_metadata()
            if start_generation_fingerprint is None
            else generation.metadata()
        )
    conditioning = {
        "start_source": start_source,
        "start_image": _asset(start_image),
        "end_image": _asset(end_image if end_image is not None else shot.end_image),
        "generation": generation_metadata,
    }
    if end_generation is not None:
        conditioning["end_generation"] = end_generation.metadata()
    if start_generation_fingerprint is not None:
        conditioning["start_generation_fingerprint"] = start_generation_fingerprint
    if end_generation_fingerprint is not None:
        conditioning["end_generation_fingerprint"] = end_generation_fingerprint

    runtime = {
        "container_image": profile.image,
        "comfy_args": list(profile.comfy_args),
        "video_workflow": _workflow(
            video_workflow,
            video_workflow_sha256,
            output_suffix=video_output_suffix,
        ),
        "start_image_workflow": (
            _workflow(start_workflow, start_workflow_sha256)
            if start_workflow is not None and start_workflow_sha256 is not None
            else None
        ),
        "model_path_aliases": [
            {"source": alias.source, "target": alias.target}
            for alias in profile.model_path_aliases
        ],
    }
    if end_workflow is not None and end_workflow_sha256 is not None:
        runtime["end_image_workflow"] = _workflow(
            end_workflow, end_workflow_sha256
        )
    if prompt_refinement is not None:
        runtime["prompt_refinement"] = prompt_refinement

    return {
        "shot": {"index": index, "name": shot.name},
        "prompts": {
            "global": scene.global_prompt,
            "shot": shot.prompt,
            "camera": shot.camera,
            "starting_state": starting_state,
            "end_state": shot.end_state,
            "positive_effective": positive_prompt,
            "negative_effective": negative_prompt,
        },
        "sampling": {
            "seed": shot.seed,
            "cfg": shot.cfg,
            "steps": scene.steps,
            "transition_step": scene.transition_step,
        },
        "format": {
            "width": scene.width,
            "height": scene.height,
            "fps": scene.fps,
            "frames": shot.frames,
            "duration_seconds": shot.duration_seconds,
        },
        "conditioning": conditioning,
        "runtime": runtime,
    }


def build_generated_image_inputs(
    shot: Shot,
    *,
    index: int,
    role: str,
    profile: Profile,
    generation: ResolvedImageGeneration,
    image_workflow: WorkflowSelection,
    image_workflow_sha256: str,
    reference_images: tuple[Path, ...],
    prompt_refinement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in {"start", "end"}:
        raise ValueError("Generated image role must be 'start' or 'end'")
    configured_generation = (
        shot.generate_start_image if role == "start" else shot.generate_end_image
    )
    if configured_generation is None:
        raise ValueError(f"Shot {index} does not configure {role} image generation")

    references = []
    for path in reference_images:
        asset = _asset(path)
        if asset is None:
            raise ValueError(f"Reference image not found: {path}")
        references.append(asset)

    return {
        "shot": {"index": index, "name": shot.name},
        "role": role,
        "generation": generation.metadata(),
        "references": references,
        "runtime": {
            "container_image": profile.image,
            "comfy_args": list(profile.comfy_args),
            "image_workflow": _workflow(
                image_workflow, image_workflow_sha256
            ),
            "model_path_aliases": [
                {"source": alias.source, "target": alias.target}
                for alias in profile.model_path_aliases
            ],
            **(
                {"prompt_refinement": prompt_refinement}
                if prompt_refinement is not None
                else {}
            ),
        },
    }


def build_start_image_inputs(
    shot: Shot,
    *,
    index: int,
    profile: Profile,
    generation: ResolvedStartImageGeneration,
    start_workflow: WorkflowSelection,
    start_workflow_sha256: str,
    prompt_refinement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if shot.generate_start_image is None:
        raise ValueError(f"Shot {index} does not configure start image generation")
    return {
        "shot": {"index": index, "name": shot.name},
        "generation": generation.legacy_metadata(),
        "runtime": {
            "container_image": profile.image,
            "comfy_args": list(profile.comfy_args),
            "start_image_workflow": _workflow(
                start_workflow, start_workflow_sha256
            ),
            "model_path_aliases": [
                {"source": alias.source, "target": alias.target}
                for alias in profile.model_path_aliases
            ],
            **(
                {"prompt_refinement": prompt_refinement}
                if prompt_refinement is not None
                else {}
            ),
        },
    }


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def write_shot_metadata(
    path: Path,
    *,
    inputs: dict[str, Any],
    video: Path,
    continuation: Path,
    output_root: Path,
    runtime: dict[str, Any],
    elapsed_seconds: float,
) -> dict[str, Any]:
    value = {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": fingerprint(inputs),
        "inputs": inputs,
        "outputs": {
            "video": {
                "path": str(video.relative_to(output_root)),
                **(_asset(video) or {}),
            },
            "continuation": {
                "path": str(continuation.relative_to(output_root)),
                **(_asset(continuation) or {}),
            },
        },
        "render": {
            "completed_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 3),
            **runtime,
        },
    }
    _atomic_write_json(path, value)
    return value


def write_generated_image_metadata(
    path: Path,
    *,
    inputs: dict[str, Any],
    image: Path,
    output_root: Path,
    runtime: dict[str, Any],
    elapsed_seconds: float,
) -> dict[str, Any]:
    value = {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": fingerprint(inputs),
        "inputs": inputs,
        "output": {
            "path": str(image.relative_to(output_root)),
            **(_asset(image) or {}),
        },
        "render": {
            "completed_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 3),
            **runtime,
        },
    }
    _atomic_write_json(path, value)
    return value


def write_start_image_metadata(
    path: Path,
    *,
    inputs: dict[str, Any],
    image: Path,
    output_root: Path,
    runtime: dict[str, Any],
    elapsed_seconds: float,
) -> dict[str, Any]:
    return write_generated_image_metadata(
        path,
        inputs=inputs,
        image=image,
        output_root=output_root,
        runtime=runtime,
        elapsed_seconds=elapsed_seconds,
    )


def read_metadata(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def validate_shot_metadata(
    metadata: dict[str, Any] | None,
    expected_inputs: dict[str, Any],
    output_root: Path,
) -> tuple[Path | None, list[str]]:
    if metadata is None:
        return None, ["metadata: missing or invalid"]
    differences = diff_values(metadata.get("inputs"), expected_inputs, "inputs")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        differences.append(
            f"schema_version: {metadata.get('schema_version')!r} -> {SCHEMA_VERSION!r}"
        )
    if metadata.get("fingerprint") != fingerprint(expected_inputs):
        if not differences:
            differences.append("fingerprint: does not match effective inputs")
    outputs = metadata.get("outputs")
    if not isinstance(outputs, dict):
        return None, [*differences, "outputs: missing"]
    video = _validate_output(outputs.get("video"), output_root, "video", differences)
    _validate_output(outputs.get("continuation"), output_root, "continuation", differences)
    return video, differences


def validate_generated_image_metadata(
    metadata: dict[str, Any] | None,
    expected_inputs: dict[str, Any],
    output_root: Path,
) -> tuple[Path | None, list[str]]:
    if metadata is None:
        return None, ["generated image metadata: missing or invalid"]
    differences = diff_values(metadata.get("inputs"), expected_inputs, "inputs")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        differences.append(
            f"schema_version: {metadata.get('schema_version')!r} -> {SCHEMA_VERSION!r}"
        )
    if metadata.get("fingerprint") != fingerprint(expected_inputs):
        if not differences:
            differences.append("fingerprint: does not match generated image inputs")
    role = expected_inputs.get("role")
    output_label = f"{role}_image" if role in {"start", "end"} else "generated_image"
    image = _validate_output(
        metadata.get("output"), output_root, output_label, differences
    )
    return image, differences


def validate_start_image_metadata(
    metadata: dict[str, Any] | None,
    expected_inputs: dict[str, Any],
    output_root: Path,
) -> tuple[Path | None, list[str]]:
    if metadata is None:
        return None, ["start image metadata: missing or invalid"]
    differences = diff_values(metadata.get("inputs"), expected_inputs, "inputs")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        differences.append(
            f"schema_version: {metadata.get('schema_version')!r} -> {SCHEMA_VERSION!r}"
        )
    if metadata.get("fingerprint") != fingerprint(expected_inputs):
        if not differences:
            differences.append("fingerprint: does not match start image inputs")
    image = _validate_output(
        metadata.get("output"), output_root, "start_image", differences
    )
    return image, differences


def _validate_output(
    value: object,
    output_root: Path,
    label: str,
    differences: list[str],
) -> Path | None:
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        differences.append(f"outputs.{label}: missing metadata")
        return None
    relative_path = Path(value["path"])
    if relative_path.is_absolute():
        differences.append(
            f"outputs.{label}.path: absolute paths are not allowed: "
            f"{value['path']!r}"
        )
        return None

    depth = 0
    for part in relative_path.parts:
        if part == "..":
            if depth == 0:
                differences.append(
                    f"outputs.{label}.path: traversal escapes output_root: "
                    f"{value['path']!r}"
                )
                return None
            depth -= 1
        elif part != ".":
            depth += 1

    path = output_root / relative_path
    resolved_root = output_root.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        differences.append(
            f"outputs.{label}.path: resolves outside output_root: "
            f"{value['path']!r}"
        )
        return None
    if not path.is_file():
        differences.append(f"outputs.{label}: missing file {path}")
        return None
    expected_hash = value.get("sha256")
    actual_hash = sha256_file(path)
    if expected_hash != actual_hash:
        differences.append(
            f"outputs.{label}.sha256: {expected_hash!r} -> {actual_hash!r}"
        )
    return path


def diff_values(old: object, new: object, prefix: str = "") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        differences: list[str] = []
        for key in sorted(set(old) | set(new)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in old:
                differences.append(f"{path}: missing -> {_short(new[key])}")
            elif key not in new:
                differences.append(f"{path}: {_short(old[key])} -> removed")
            else:
                differences.extend(diff_values(old[key], new[key], path))
        return differences
    if old != new:
        return [f"{prefix}: {_short(old)} -> {_short(new)}"]
    return []


def _short(value: object, limit: int = 180) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def write_render_manifest(
    output_root: Path,
    scene_path: Path,
    scene: Scene,
    *,
    selected_shots: list[int],
    final_video: Path | None = None,
    provenance: str | None = None,
    prompt_refinement: dict[str, Any] | None = None,
) -> None:
    shots = []
    for index, shot in enumerate(scene.shots, start=1):
        metadata_path = (
            output_root / f"{index:03d}-{slugify(shot.name)}" / "metadata.json"
        )
        metadata = read_metadata(metadata_path)
        if metadata is not None:
            shots.append(metadata)
    generated_images: dict[str, list[dict[str, Any]]] = {
        "start": [],
        "end": [],
    }
    for index, shot in enumerate(scene.shots, start=1):
        for role, configured_generation in (
            ("start", shot.generate_start_image),
            ("end", shot.generate_end_image),
        ):
            if configured_generation is None:
                continue
            metadata_path = (
                output_root
                / f"000-generated-{role}-image"
                / f"{index:03d}-{slugify(shot.name)}.metadata.json"
            )
            metadata = read_metadata(metadata_path)
            inputs = metadata.get("inputs") if metadata is not None else None
            metadata_shot = inputs.get("shot") if isinstance(inputs, dict) else None
            metadata_role = inputs.get("role") if isinstance(inputs, dict) else None
            if (
                metadata is not None
                and metadata_shot == {"index": index, "name": shot.name}
                and (
                    metadata_role == role
                    or (role == "start" and metadata_role is None)
                )
            ):
                generated_images[role].append(metadata)
    value = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(UTC).isoformat(),
        "provenance": provenance,
        **(
            {"prompt_refinement": prompt_refinement}
            if prompt_refinement is not None
            else {}
        ),
        "scene": {
            "title": scene.title,
            "source": str(scene_path.resolve()),
            "sha256": sha256_file(scene_path.resolve()),
            "snapshot": (
                {
                    "path": "scene.snapshot.json",
                    **(_asset(output_root / "scene.snapshot.json") or {}),
                }
                if (output_root / "scene.snapshot.json").is_file()
                else None
            ),
        },
        "selected_shots": selected_shots,
        "start_images": generated_images["start"],
        "end_images": generated_images["end"],
        "shots": shots,
        "final_video": (
            {
                "path": str(final_video.relative_to(output_root)),
                **(_asset(final_video) or {}),
                "provenance": provenance,
            }
            if final_video is not None and final_video.is_file()
            else None
        ),
    }
    _atomic_write_json(output_root / "render-manifest.json", value)
