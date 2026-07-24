import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from runpod_video_automation.adapters import (
    IMAGE_ADAPTERS,
    ResolvedImageGeneration,
    ResolvedStartImageGeneration,
    build_image_workflow,
    build_start_image_workflow,
    resolve_image_generation,
    resolve_start_image_generation,
)


ROOT = Path(__file__).resolve().parents[1]
QWEN_WORKFLOW = ROOT / "workflows/qwen-image-edit-2511-api.json"


def _generation(
    reference_count: int,
    *,
    adapter: str | None = "qwen_image_edit_2511",
    dimensions_explicit: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        adapter=adapter,
        workflow="qwen_image_edit",
        prompt="Preserve the subject and change the lighting",
        negative_prompt="blurry, distorted",
        checkpoint=None,
        width=1184,
        height=880,
        dimensions_explicit=dimensions_explicit,
        seed=123456,
        steps=None,
        cfg=None,
        sampler_name=None,
        scheduler=None,
        reference_images=tuple(
            Path(f"reference-{index}.png")
            for index in range(1, reference_count + 1)
        ),
    )


def _load_qwen_workflow() -> dict[str, object]:
    return json.loads(QWEN_WORKFLOW.read_text())


@pytest.mark.parametrize("reference_count", [1, 2, 3])
def test_qwen_builds_ordered_reference_variants(reference_count: int) -> None:
    base = _load_qwen_workflow()
    original = copy.deepcopy(base)
    resolved = resolve_image_generation(
        _generation(reference_count), "qwen_image_edit_2511"
    )
    reference_names = tuple(
        f"uploads/reference-{index}.png"
        for index in range(1, reference_count + 1)
    )
    role = "end" if reference_count == 3 else "start"

    workflow = build_image_workflow(
        "qwen_image_edit_2511",
        base,
        resolved,
        shot_number=7,
        shot_name="Window Portrait",
        role=role,
        reference_names=reference_names,
    )

    assert base == original
    assert workflow["41"]["inputs"]["image"] == reference_names[0]
    for index, node_id in enumerate(("41", "83", "84")):
        if index < reference_count:
            assert workflow[node_id]["inputs"]["image"] == reference_names[index]
        else:
            assert node_id not in workflow
    for node_id in ("151", "149"):
        inputs = workflow[node_id]["inputs"]
        assert inputs["image1"] == ["160", 0]
        assert ("image2" in inputs) is (reference_count >= 2)
        assert ("image3" in inputs) is (reference_count >= 3)
    assert workflow["161"]["inputs"]["unet_name"] == resolved.checkpoint
    assert workflow["151"]["inputs"]["prompt"] == resolved.prompt
    assert workflow["149"]["inputs"]["prompt"] == resolved.negative_prompt
    assert workflow["169"]["inputs"] == {
        **original["169"]["inputs"],
        "seed": 123456,
        "steps": 40,
        "cfg": 4.0,
        "sampler_name": "euler",
        "scheduler": "simple",
        "denoise": 1.0,
    }
    expected_suffix = "-end" if role == "end" else ""
    assert workflow["9"]["inputs"]["filename_prefix"] == (
        f"generated/007-window-portrait{expected_suffix}"
    )
    assert sum(
        node["class_type"] == "SaveImage" for node in workflow.values()
    ) == 1


@pytest.mark.parametrize("reference_count", [0, 4])
def test_qwen_rejects_invalid_reference_count(reference_count: int) -> None:
    with pytest.raises(ValueError, match="requires 1 to 3 reference images"):
        resolve_image_generation(
            _generation(reference_count), "qwen_image_edit_2511"
        )


def test_qwen_rejects_explicit_dimensions() -> None:
    with pytest.raises(ValueError, match="explicit dimensions are not supported"):
        resolve_image_generation(
            _generation(1, dimensions_explicit=True), "qwen_image_edit_2511"
        )


def test_qwen_defaults_and_backward_compatible_aliases() -> None:
    resolved = resolve_image_generation(_generation(1), "qwen_image_edit_2511")

    assert resolved.checkpoint == "qwen_image_edit_2511_fp8mixed.safetensors"
    assert (resolved.steps, resolved.cfg) == (40, 4.0)
    assert (resolved.sampler_name, resolved.scheduler) == ("euler", "simple")
    assert resolved.width == 1184
    assert resolved.height == 880
    assert resolved.reference_count == 1
    assert ResolvedStartImageGeneration is ResolvedImageGeneration
    assert IMAGE_ADAPTERS["qwen_image_edit_2511"].supports_negative_prompt is True


def test_legacy_z_image_wrapper_is_unchanged() -> None:
    generation = SimpleNamespace(
        adapter="z_image_turbo",
        prompt="A full-length portrait",
        negative_prompt="",
        checkpoint=None,
        width=864,
        height=1200,
        seed=99,
        steps=None,
        cfg=None,
        sampler_name=None,
        scheduler=None,
    )
    resolved = resolve_start_image_generation(generation, "z_image_turbo")
    base = json.loads(
        (ROOT / "workflows/z-image-turbo-start-image-api.json").read_text()
    )
    original = copy.deepcopy(base)

    workflow = build_start_image_workflow(
        "z_image_turbo",
        base,
        resolved,
        shot_number=1,
        shot_name="Opening Portrait",
    )

    assert base == original
    assert workflow["4"]["inputs"]["unet_name"] == (
        "cyberrealisticZImage_v50.safetensors"
    )
    assert workflow["5"]["inputs"] == {
        "width": 864,
        "height": 1200,
        "batch_size": 1,
    }
    assert workflow["6"]["inputs"]["text"] == "A full-length portrait"
    assert workflow["7"] == original["7"]
    assert workflow["3"]["inputs"] == {
        **original["3"]["inputs"],
        "seed": 99,
        "steps": 8,
        "cfg": 1.0,
        "sampler_name": "res_multistep",
        "scheduler": "simple",
        "denoise": 1.0,
    }
    assert workflow["9"]["inputs"]["filename_prefix"] == (
        "generated/001-opening-portrait"
    )


def test_qwen_workflow_is_a_valid_flattened_api_graph() -> None:
    workflow = _load_qwen_workflow()
    expected_types = {
        "FluxKontextImageScale",
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
        "ModelSamplingAuraFlow",
        "CFGNorm",
        "TextEncodeQwenImageEditPlus",
        "FluxKontextMultiReferenceLatentMethod",
        "VAEEncode",
        "KSampler",
        "VAEDecode",
        "LoadImage",
        "SaveImage",
    }

    assert all(
        isinstance(node_id, str)
        and isinstance(node, dict)
        and isinstance(node.get("inputs"), dict)
        for node_id, node in workflow.items()
    )
    assert {node["class_type"] for node in workflow.values()} == expected_types
    assert sum(
        node["class_type"] == "TextEncodeQwenImageEditPlus"
        for node in workflow.values()
    ) == 2
    assert sum(
        node["class_type"] == "FluxKontextMultiReferenceLatentMethod"
        for node in workflow.values()
    ) == 2
    for node in workflow.values():
        for value in node["inputs"].values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and isinstance(value[1], int)
            ):
                assert value[0] in workflow
