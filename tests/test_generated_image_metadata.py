from pathlib import Path

import pytest

from runpod_video_automation.adapters import ResolvedImageGeneration
from runpod_video_automation.config import (
    ModelFile,
    ModelPathAlias,
    Profile,
    WorkflowPreset,
)
from runpod_video_automation.render_metadata import (
    SCHEMA_VERSION,
    build_generated_image_inputs,
    build_shot_inputs,
    build_start_image_inputs,
    fingerprint,
    read_metadata,
    sha256_file,
    validate_generated_image_metadata,
    write_generated_image_metadata,
    write_render_manifest,
)
from runpod_video_automation.scene import ImageGeneration, Scene, Shot


def _configured_generation(*, workflow: str = "image") -> ImageGeneration:
    return ImageGeneration(
        adapter="qwen_image_edit_2511",
        prompt="Preserve the subject",
        negative_prompt="distorted",
        checkpoint=None,
        width=768,
        height=768,
        seed=123,
        steps=None,
        cfg=None,
        sampler_name=None,
        scheduler=None,
        workflow=workflow,
    )


def _resolved_generation(*, prompt: str = "Preserve the subject") -> ResolvedImageGeneration:
    return ResolvedImageGeneration(
        adapter="qwen_image_edit_2511",
        prompt=prompt,
        negative_prompt="distorted",
        checkpoint="qwen.safetensors",
        width=768,
        height=768,
        seed=123,
        steps=40,
        cfg=4.0,
        sampler_name="euler",
        scheduler="simple",
        workflow="image",
        reference_count=2,
    )


def _values() -> tuple[Scene, Shot, Profile]:
    generation = _configured_generation()
    shot = Shot(
        name="Opening",
        prompt="The subject turns",
        camera="locked camera",
        negative_prompt="blur",
        start_image=None,
        generate_start_image=generation,
        end_image=None,
        generate_end_image=generation,
        duration_seconds=5,
        frames=81,
        seed=42,
        cfg=3.5,
    )
    scene = Scene(
        title="Metadata",
        global_prompt="Cinematic portrait",
        negative_prompt="flicker",
        width=768,
        height=768,
        fps=16,
        steps=20,
        transition_step=10,
        cfg=3.5,
        shots=(shot,),
    )
    model = ModelFile(
        "https://example.test/qwen",
        "models/unet/qwen.safetensors",
        1234,
        "a" * 64,
    )
    profile = Profile(
        name="test",
        image="worker:image",
        data_center_id="US-MO-1",
        volume_name="models",
        volume_size_gb=1,
        gpu_type_ids=("GPU",),
        min_ram_per_gpu=1,
        min_vcpu_per_gpu=1,
        container_disk_gb=1,
        max_hourly_cost=1,
        model_groups={"image": (model,), "video": (model,)},
        model_path_aliases=(
            ModelPathAlias("models/unet", "models/diffusion_models"),
        ),
        workflows={
            "image": WorkflowPreset(
                Path("image.json"),
                "qwen_image_edit_2511",
                ("image",),
                {"steps": 40},
            ),
            "video": WorkflowPreset(
                Path("video.json"), "wan22_i2v", ("video",)
            ),
        },
        comfy_args=("--enable-triton-backend",),
    )
    return scene, shot, profile


@pytest.mark.parametrize("role", ["start", "end"])
def test_generated_image_inputs_capture_role_and_exact_provenance(
    tmp_path: Path, role: str
) -> None:
    _, shot, profile = _values()
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    inputs = build_generated_image_inputs(
        shot,
        index=1,
        role=role,
        profile=profile,
        generation=_resolved_generation(),
        image_workflow=profile.select_workflow("image"),
        image_workflow_sha256="b" * 64,
        reference_images=(first, second),
    )

    assert inputs["role"] == role
    assert inputs["generation"] == _resolved_generation().metadata()
    assert inputs["references"] == [
        {"name": "first.png", "size": 5, "sha256": sha256_file(first)},
        {"name": "second.png", "size": 6, "sha256": sha256_file(second)},
    ]
    assert inputs["runtime"]["image_workflow"] == {
        "name": "image",
        "adapter": "qwen_image_edit_2511",
        "sha256": "b" * 64,
        "model_groups": ["image"],
        "models": [
            {
                "path": "models/unet/qwen.safetensors",
                "url": "https://example.test/qwen",
                "size": 1234,
                "sha256": "a" * 64,
            }
        ],
        "defaults": {"steps": 40},
    }
    assert inputs["runtime"]["model_path_aliases"] == [
        {
            "source": "models/unet",
            "target": "models/diffusion_models",
        }
    ]
    assert "prompt_refinement" not in inputs["runtime"]


def test_generated_image_inputs_capture_prompt_refinement(
    tmp_path: Path,
) -> None:
    _, shot, profile = _values()
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"reference")
    provenance = {"cache_key": "refined-scene"}

    inputs = build_generated_image_inputs(
        shot,
        index=1,
        role="end",
        profile=profile,
        generation=_resolved_generation(),
        image_workflow=profile.select_workflow("image"),
        image_workflow_sha256="b" * 64,
        reference_images=(reference,),
        prompt_refinement=provenance,
    )

    assert inputs["runtime"]["prompt_refinement"] == provenance


def test_reference_order_and_hash_affect_generated_image_fingerprint(
    tmp_path: Path,
) -> None:
    _, shot, profile = _values()
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    def build(references: tuple[Path, ...]) -> dict[str, object]:
        return build_generated_image_inputs(
            shot,
            index=1,
            role="end",
            profile=profile,
            generation=_resolved_generation(),
            image_workflow=profile.select_workflow("image"),
            image_workflow_sha256="b" * 64,
            reference_images=references,
        )

    original = fingerprint(build((first, second)))
    assert fingerprint(build((second, first))) != original

    first.write_bytes(b"changed")
    assert fingerprint(build((first, second))) != original


def test_end_generation_fields_change_shot_fingerprint_only_when_provided(
    tmp_path: Path,
) -> None:
    scene, shot, profile = _values()
    common = {
        "index": 1,
        "start_image": None,
        "profile": profile,
        "video_workflow": profile.select_workflow("video"),
        "video_workflow_sha256": "c" * 64,
    }
    legacy = build_shot_inputs(scene, shot, **common)
    assert "end_generation" not in legacy["conditioning"]
    assert "start_generation_fingerprint" not in legacy["conditioning"]
    assert "end_generation_fingerprint" not in legacy["conditioning"]
    assert "end_image_workflow" not in legacy["runtime"]

    extended = build_shot_inputs(
        scene,
        shot,
        **common,
        end_generation=_resolved_generation(prompt="Ending frame"),
        end_workflow=profile.select_workflow("image"),
        end_workflow_sha256="d" * 64,
        start_generation_fingerprint="start-fingerprint",
        end_generation_fingerprint="end-fingerprint",
    )

    assert fingerprint(extended) != fingerprint(legacy)
    assert extended["conditioning"]["end_generation"]["prompt"] == "Ending frame"
    assert extended["conditioning"]["start_generation_fingerprint"] == (
        "start-fingerprint"
    )
    assert extended["conditioning"]["end_generation_fingerprint"] == (
        "end-fingerprint"
    )
    assert extended["runtime"]["end_image_workflow"]["sha256"] == "d" * 64


def test_generated_image_metadata_detects_tampered_output(tmp_path: Path) -> None:
    _, shot, profile = _values()
    output_root = tmp_path / "output"
    image = output_root / "000-generated-end-image/001-opening.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    inputs = build_generated_image_inputs(
        shot,
        index=1,
        role="end",
        profile=profile,
        generation=_resolved_generation(),
        image_workflow=profile.select_workflow("image"),
        image_workflow_sha256="b" * 64,
        reference_images=(),
    )
    metadata = write_generated_image_metadata(
        image.with_suffix(".metadata.json"),
        inputs=inputs,
        image=image,
        output_root=output_root,
        runtime={"pod_id": "pod-1"},
        elapsed_seconds=1.25,
    )
    image.write_bytes(b"tampered")

    reused, differences = validate_generated_image_metadata(
        metadata, inputs, output_root
    )

    assert reused == image
    assert any("outputs.end_image.sha256" in value for value in differences)


def test_legacy_start_image_inputs_shape_is_unchanged() -> None:
    _, shot, profile = _values()
    generation = _resolved_generation()

    inputs = build_start_image_inputs(
        shot,
        index=1,
        profile=profile,
        generation=generation,
        start_workflow=profile.select_workflow("image"),
        start_workflow_sha256="b" * 64,
    )

    assert inputs == {
        "shot": {"index": 1, "name": "Opening"},
        "generation": generation.legacy_metadata(),
        "runtime": {
            "container_image": "worker:image",
            "comfy_args": ["--enable-triton-backend"],
            "start_image_workflow": {
                "name": "image",
                "adapter": "qwen_image_edit_2511",
                "sha256": "b" * 64,
                "model_groups": ["image"],
                "models": [
                    {
                        "path": "models/unet/qwen.safetensors",
                        "url": "https://example.test/qwen",
                        "size": 1234,
                        "sha256": "a" * 64,
                    }
                ],
                "defaults": {"steps": 40},
            },
            "model_path_aliases": [
                {
                    "source": "models/unet",
                    "target": "models/diffusion_models",
                }
            ],
        },
    }


def test_render_manifest_indexes_generated_end_images(tmp_path: Path) -> None:
    scene, shot, profile = _values()
    output_root = tmp_path / "output"
    image = output_root / "000-generated-end-image/001-opening.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    inputs = build_generated_image_inputs(
        shot,
        index=1,
        role="end",
        profile=profile,
        generation=_resolved_generation(),
        image_workflow=profile.select_workflow("image"),
        image_workflow_sha256="b" * 64,
        reference_images=(),
    )
    metadata = write_generated_image_metadata(
        image.parent / "001-opening.metadata.json",
        inputs=inputs,
        image=image,
        output_root=output_root,
        runtime={},
        elapsed_seconds=1,
    )
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
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["start_images"] == []
    assert manifest["end_images"] == [metadata]
