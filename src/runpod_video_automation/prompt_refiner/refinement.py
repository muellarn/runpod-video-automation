from __future__ import annotations

import copy
import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from runpod_video_automation.prompt_refiner.client import KoboldClient
from runpod_video_automation.prompt_refiner.config import PromptRefinerProfile
from runpod_video_automation.render_metadata import fingerprint, sha256_file
from runpod_video_automation.scene import Scene


REFINEMENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RefinementResult:
    scene: Scene
    document: dict[str, Any]
    manifest_path: Path
    provenance: dict[str, Any]
    cache_hit: bool


def _artifact(value: object) -> dict[str, Any]:
    return {
        "path": value.path,
        "url": value.url,
        "size": value.size,
        "sha256": value.sha256,
    }


def _refinement_inputs(
    source_document: dict[str, Any],
    profile: PromptRefinerProfile,
) -> dict[str, Any]:
    system_prompt = profile.system_prompt()
    return {
        "schema_version": REFINEMENT_SCHEMA_VERSION,
        "source_document_sha256": fingerprint(source_document),
        "profile": profile.name,
        "runtime": _artifact(profile.runtime),
        "model": _artifact(profile.model),
        "system_prompt_sha256": fingerprint({"text": system_prompt}),
        "reference_document_sha256": (
            sha256_file(profile.reference_document_path)
            if profile.reference_document_path is not None
            else None
        ),
        "generation": profile.generation_settings(),
    }


def _cache_paths(
    output_root: Path,
    inputs: dict[str, Any],
) -> tuple[Path, Path, Path, str]:
    cache_key = fingerprint(inputs)
    cache_dir = output_root / "prompt-refinement" / cache_key
    return (
        cache_dir / "scene.refined.json",
        cache_dir / "provenance.json",
        output_root / "prompt-refinement" / f"{cache_key}.lock",
        cache_key,
    )


def _read_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_cached(
    *,
    source_path: Path,
    source_document: dict[str, Any],
    output_root: Path,
    profile: PromptRefinerProfile,
) -> RefinementResult | None:
    inputs = _refinement_inputs(source_document, profile)
    manifest_path, provenance_path, _, cache_key = _cache_paths(output_root, inputs)
    provenance = _read_object(provenance_path)
    document = _read_object(manifest_path)
    if provenance is None or document is None:
        return None
    if provenance.get("schema_version") != REFINEMENT_SCHEMA_VERSION:
        return None
    if provenance.get("cache_key") != cache_key or provenance.get("inputs") != inputs:
        return None
    if provenance.get("refined_manifest_sha256") != sha256_file(manifest_path):
        return None
    try:
        scene = Scene.from_dict(document, base_dir=source_path.parent)
    except (TypeError, ValueError):
        return None
    return RefinementResult(
        scene=scene,
        document=document,
        manifest_path=manifest_path,
        provenance=provenance,
        cache_hit=True,
    )


def load_cached_refinement(
    *,
    source_path: Path,
    output_root: Path,
    profile: PromptRefinerProfile,
) -> RefinementResult | None:
    source_document = _read_object(source_path)
    if source_document is None:
        raise ValueError(f"Scene manifest is not a JSON object: {source_path}")
    return _load_cached(
        source_path=source_path,
        source_document=source_document,
        output_root=output_root,
        profile=profile,
    )


def _prompt_payload(source_document: dict[str, Any]) -> dict[str, Any]:
    raw_shots = source_document.get("shots")
    if not isinstance(raw_shots, list):
        raise ValueError("Scene field 'shots' must be an array")
    shots: list[dict[str, Any]] = []
    for index, shot in enumerate(raw_shots, start=1):
        if not isinstance(shot, dict):
            raise ValueError(f"Scene shot {index} must be an object")
        generation = shot.get("generate_start_image")
        generation_prompt: str | None = None
        generation_negative_prompt: str | None = None
        if isinstance(generation, dict):
            raw_prompt = generation.get("prompt")
            generation_prompt = raw_prompt if isinstance(raw_prompt, str) else None
            raw_negative_prompt = generation.get("negative_prompt", "")
            generation_negative_prompt = (
                raw_negative_prompt
                if isinstance(raw_negative_prompt, str)
                else None
            )
        shots.append(
            {
                "name": shot.get("name"),
                "prompt": shot.get("prompt", ""),
                "camera": shot.get("camera", ""),
                "negative_prompt": shot.get("negative_prompt", ""),
                "end_state": shot.get("end_state", ""),
                "generate_start_image_prompt": generation_prompt,
                "generate_start_image_negative_prompt": generation_negative_prompt,
            }
        )
    return {
        "global_prompt": source_document.get("global_prompt", ""),
        "negative_prompt": source_document.get("negative_prompt", ""),
        "shots": shots,
    }


def _required_text(value: object, field: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Refiner output field {field!r} must be a string")
    value = value.strip()
    if not allow_empty and not value:
        raise ValueError(f"Refiner output field {field!r} must not be empty")
    return value


def _apply_overlay(
    source_document: dict[str, Any],
    overlay: object,
) -> dict[str, Any]:
    if not isinstance(overlay, dict):
        raise ValueError("Prompt refiner output must be a JSON object")
    required_root = {"global_prompt", "negative_prompt", "shots"}
    if set(overlay) != required_root:
        raise ValueError("Prompt refiner output has unexpected top-level fields")
    raw_shots = source_document.get("shots")
    refined_shots = overlay.get("shots")
    if not isinstance(raw_shots, list) or not isinstance(refined_shots, list):
        raise ValueError("Prompt refiner output shots must be an array")
    if len(raw_shots) != len(refined_shots):
        raise ValueError("Prompt refiner changed the shot count")

    document = copy.deepcopy(source_document)
    document["global_prompt"] = _required_text(
        overlay.get("global_prompt"), "global_prompt"
    )
    document["negative_prompt"] = _required_text(
        overlay.get("negative_prompt"), "negative_prompt"
    )
    output_shots = document["shots"]
    required_shot = {
        "name",
        "prompt",
        "camera",
        "negative_prompt",
        "end_state",
        "generate_start_image_prompt",
        "generate_start_image_negative_prompt",
    }
    for index, (source, refined) in enumerate(
        zip(raw_shots, refined_shots, strict=True), start=1
    ):
        if not isinstance(source, dict) or not isinstance(refined, dict):
            raise ValueError(f"Prompt refiner shot {index} must be an object")
        if set(refined) != required_shot:
            raise ValueError(f"Prompt refiner shot {index} has unexpected fields")
        if refined.get("name") != source.get("name"):
            raise ValueError(f"Prompt refiner changed shot {index} name")
        target = output_shots[index - 1]
        for field in ("prompt", "camera", "negative_prompt", "end_state"):
            target[field] = _required_text(
                refined.get(field), f"shots[{index}].{field}"
            )
        generated_prompt = refined.get("generate_start_image_prompt")
        generated_negative_prompt = refined.get(
            "generate_start_image_negative_prompt"
        )
        source_generation = source.get("generate_start_image")
        if source_generation is None:
            if generated_prompt is not None or generated_negative_prompt is not None:
                raise ValueError(
                    f"Prompt refiner added a generated start image to shot {index}"
                )
        else:
            if not isinstance(source_generation, dict):
                raise ValueError(f"Scene shot {index} generation must be an object")
            target_generation = target["generate_start_image"]
            target_generation["prompt"] = _required_text(
                generated_prompt,
                f"shots[{index}].generate_start_image.prompt",
                allow_empty=False,
            )
            target_generation["negative_prompt"] = _required_text(
                generated_negative_prompt,
                f"shots[{index}].generate_start_image.negative_prompt",
            )
    return document


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def refine_scene(
    *,
    client: KoboldClient,
    source_path: Path,
    output_root: Path,
    profile: PromptRefinerProfile,
    force: bool = False,
) -> RefinementResult:
    source_document = _read_object(source_path)
    if source_document is None:
        raise ValueError(f"Scene manifest is not a JSON object: {source_path}")
    Scene.from_dict(source_document, base_dir=source_path.parent)
    inputs = _refinement_inputs(source_document, profile)
    manifest_path, provenance_path, lock_path, cache_key = _cache_paths(
        output_root, inputs
    )
    with _exclusive_lock(lock_path):
        if not force:
            cached = _load_cached(
                source_path=source_path,
                source_document=source_document,
                output_root=output_root,
                profile=profile,
            )
            if cached is not None:
                return cached
        payload = _prompt_payload(source_document)
        content = client.chat_completion(
            system_prompt=profile.system_prompt(),
            user_prompt=json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            profile=profile,
        )
        try:
            overlay = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError("Prompt refiner did not return strict JSON") from error
        document = _apply_overlay(source_document, overlay)
        scene = Scene.from_dict(document, base_dir=source_path.parent)
        _atomic_write(manifest_path, document)
        provenance = {
            "schema_version": REFINEMENT_SCHEMA_VERSION,
            "cache_key": cache_key,
            "inputs": inputs,
            "refined_manifest": str(manifest_path.relative_to(output_root)),
            "refined_manifest_sha256": sha256_file(manifest_path),
        }
        _atomic_write(provenance_path, provenance)
        return RefinementResult(
            scene=scene,
            document=document,
            manifest_path=manifest_path,
            provenance=provenance,
            cache_hit=False,
        )
