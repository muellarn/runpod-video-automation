from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any, Callable, Sequence

from runpod_video_automation.scene import Scene, Shot, slugify


@dataclass(frozen=True)
class ResolvedImageGeneration:
    adapter: str
    prompt: str
    negative_prompt: str
    checkpoint: str
    width: int
    height: int
    seed: int
    steps: int
    cfg: float
    sampler_name: str
    scheduler: str
    workflow: str | None = None
    dimensions_explicit: bool = False
    reference_count: int = 0

    def metadata(self) -> dict[str, Any]:
        return asdict(self)

    def legacy_metadata(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "checkpoint": self.checkpoint,
            "width": self.width,
            "height": self.height,
            "seed": self.seed,
            "steps": self.steps,
            "cfg": self.cfg,
            "sampler_name": self.sampler_name,
            "scheduler": self.scheduler,
        }


ResolvedStartImageGeneration = ResolvedImageGeneration


@dataclass(frozen=True)
class ImageAdapter:
    name: str
    checkpoint: str
    steps: int
    cfg: float
    sampler_name: str
    scheduler: str
    model_input: str
    supports_negative_prompt: bool
    build: Callable[..., dict[str, Any]]
    minimum_references: int = 0
    maximum_references: int = 0
    dimensions_from_reference: bool = False


StartImageAdapter = ImageAdapter


@dataclass(frozen=True)
class VideoAdapter:
    name: str
    build: Callable[..., dict[str, Any]]
    output_suffix: str = ".webm"


def _node_inputs(workflow: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = workflow.get(node_id)
    inputs = node.get("inputs") if isinstance(node, dict) else None
    if not isinstance(inputs, dict):
        raise ValueError(f"Workflow adapter requires node {node_id}")
    return inputs


def _output_prefix(shot_number: int, shot_name: str, role: str) -> str:
    if role not in {"start", "end"}:
        raise ValueError("Image generation role must be 'start' or 'end'")
    suffix = "" if role == "start" else "-end"
    return f"generated/{shot_number:03d}-{slugify(shot_name)}{suffix}"


def _build_text_to_image_workflow(
    adapter: ImageAdapter,
    base_workflow: dict[str, Any],
    generation: ResolvedImageGeneration,
    *,
    shot_number: int,
    shot_name: str,
    role: str,
    reference_names: Sequence[str],
) -> dict[str, Any]:
    if reference_names:
        raise ValueError(f"Image adapter {adapter.name!r} does not accept references")
    workflow = copy.deepcopy(base_workflow)
    _node_inputs(workflow, "4")[adapter.model_input] = generation.checkpoint
    _node_inputs(workflow, "5").update(
        {"width": generation.width, "height": generation.height, "batch_size": 1}
    )
    _node_inputs(workflow, "6")["text"] = generation.prompt
    if adapter.supports_negative_prompt:
        _node_inputs(workflow, "7")["text"] = generation.negative_prompt
    _node_inputs(workflow, "3").update(
        {
            "seed": generation.seed,
            "steps": generation.steps,
            "cfg": generation.cfg,
            "sampler_name": generation.sampler_name,
            "scheduler": generation.scheduler,
            "denoise": 1.0,
        }
    )
    _node_inputs(workflow, "9")["filename_prefix"] = _output_prefix(
        shot_number, shot_name, role
    )
    return workflow


def _build_qwen_image_edit_2511_workflow(
    adapter: ImageAdapter,
    base_workflow: dict[str, Any],
    generation: ResolvedImageGeneration,
    *,
    shot_number: int,
    shot_name: str,
    role: str,
    reference_names: Sequence[str],
) -> dict[str, Any]:
    names = tuple(reference_names)
    if not adapter.minimum_references <= len(names) <= adapter.maximum_references:
        raise ValueError(
            f"Image adapter {adapter.name!r} requires 1 to 3 reference images"
        )
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError("Uploaded reference names must be non-empty strings")

    workflow = copy.deepcopy(base_workflow)
    image_nodes = ("41", "83", "84")
    for index, node_id in enumerate(image_nodes):
        if index < len(names):
            _node_inputs(workflow, node_id)["image"] = names[index]
        else:
            workflow.pop(node_id, None)

    image_links = {
        "image1": ["160", 0],
        "image2": ["83", 0],
        "image3": ["84", 0],
    }
    for node_id in ("151", "149"):
        inputs = _node_inputs(workflow, node_id)
        for index, input_name in enumerate(("image1", "image2", "image3")):
            if index < len(names):
                inputs[input_name] = image_links[input_name]
            else:
                inputs.pop(input_name, None)

    _node_inputs(workflow, "161")[adapter.model_input] = generation.checkpoint
    _node_inputs(workflow, "151")["prompt"] = generation.prompt
    _node_inputs(workflow, "149")["prompt"] = generation.negative_prompt
    _node_inputs(workflow, "169").update(
        {
            "seed": generation.seed,
            "steps": generation.steps,
            "cfg": generation.cfg,
            "sampler_name": generation.sampler_name,
            "scheduler": generation.scheduler,
            "denoise": 1.0,
        }
    )
    _node_inputs(workflow, "9")["filename_prefix"] = _output_prefix(
        shot_number, shot_name, role
    )
    return workflow


IMAGE_ADAPTERS = {
    "z_image_turbo": ImageAdapter(
        name="z_image_turbo",
        checkpoint="cyberrealisticZImage_v50.safetensors",
        steps=8,
        cfg=1.0,
        sampler_name="res_multistep",
        scheduler="simple",
        model_input="unet_name",
        supports_negative_prompt=False,
        build=_build_text_to_image_workflow,
    ),
    "sdxl": ImageAdapter(
        name="sdxl",
        checkpoint="sd_xl_base_1.0.safetensors",
        steps=30,
        cfg=7.0,
        sampler_name="dpmpp_2m",
        scheduler="karras",
        model_input="ckpt_name",
        supports_negative_prompt=True,
        build=_build_text_to_image_workflow,
    ),
    "qwen_image_edit_2511": ImageAdapter(
        name="qwen_image_edit_2511",
        checkpoint="qwen_image_edit_2511_fp8mixed.safetensors",
        steps=40,
        cfg=4.0,
        sampler_name="euler",
        scheduler="simple",
        model_input="unet_name",
        supports_negative_prompt=True,
        build=_build_qwen_image_edit_2511_workflow,
        minimum_references=1,
        maximum_references=3,
        dimensions_from_reference=True,
    ),
}

START_IMAGE_ADAPTERS = IMAGE_ADAPTERS


def _generation_reference_count(generation: object) -> int:
    references = getattr(generation, "reference_images", None)
    if references is None:
        references = getattr(generation, "references", ())
    return len(tuple(references))


def _resolve_image_generation(
    generation: object,
    adapter_name: str,
    defaults: dict[str, Any] | None = None,
    *,
    description: str,
) -> ResolvedImageGeneration:
    requested_adapter = getattr(generation, "adapter", None) or adapter_name
    if requested_adapter != adapter_name:
        raise ValueError(
            f"{description.capitalize()} requests adapter {requested_adapter!r}, but the selected "
            f"workflow uses {adapter_name!r}"
        )
    try:
        adapter = IMAGE_ADAPTERS[adapter_name]
    except KeyError as error:
        raise ValueError(f"Unknown {description} adapter: {adapter_name}") from error
    configured = defaults or {}

    def setting(name: str, explicit: object, fallback: object) -> object:
        return explicit if explicit is not None else configured.get(name, fallback)

    negative_prompt = generation.negative_prompt
    if negative_prompt and not adapter.supports_negative_prompt:
        raise ValueError(
            f"{description.capitalize()} adapter {adapter_name!r} does not support "
            "negative prompting"
        )
    reference_count = _generation_reference_count(generation)
    if not adapter.minimum_references <= reference_count <= adapter.maximum_references:
        if adapter.maximum_references == 0:
            raise ValueError(f"{description.capitalize()} adapter {adapter_name!r} does not accept references")
        raise ValueError(
            f"{description.capitalize()} adapter {adapter_name!r} requires 1 to 3 "
            "reference images"
        )
    dimensions_explicit = bool(
        getattr(generation, "dimensions_explicit", False)
    )
    if adapter.dimensions_from_reference and dimensions_explicit:
        raise ValueError(
            f"{description.capitalize()} adapter {adapter_name!r} derives dimensions "
            "from the primary reference; explicit dimensions are not supported"
        )
    checkpoint = setting("checkpoint", generation.checkpoint, adapter.checkpoint)
    steps = setting("steps", generation.steps, adapter.steps)
    cfg = setting("cfg", generation.cfg, adapter.cfg)
    sampler_name = setting(
        "sampler_name", generation.sampler_name, adapter.sampler_name
    )
    scheduler = setting("scheduler", generation.scheduler, adapter.scheduler)
    if not isinstance(checkpoint, str) or not checkpoint:
        raise ValueError(f"Resolved {description} checkpoint must be a non-empty string")
    if not isinstance(sampler_name, str) or not sampler_name:
        raise ValueError(f"Resolved {description} sampler_name must be a non-empty string")
    if not isinstance(scheduler, str) or not scheduler:
        raise ValueError(f"Resolved {description} scheduler must be a non-empty string")
    if isinstance(steps, bool) or not isinstance(steps, (int, float)):
        raise ValueError(f"Resolved {description} steps must be numeric")
    if isinstance(cfg, bool) or not isinstance(cfg, (int, float)):
        raise ValueError(f"Resolved {description} CFG must be numeric")
    workflow = getattr(generation, "workflow", None)
    if workflow is not None and (not isinstance(workflow, str) or not workflow):
        raise ValueError(f"Resolved {description} workflow must be a non-empty string")
    resolved = ResolvedImageGeneration(
        adapter=adapter_name,
        prompt=generation.prompt,
        negative_prompt=negative_prompt,
        checkpoint=checkpoint,
        width=generation.width,
        height=generation.height,
        seed=generation.seed,
        steps=int(steps),
        cfg=float(cfg),
        sampler_name=sampler_name,
        scheduler=scheduler,
        workflow=workflow,
        dimensions_explicit=dimensions_explicit,
        reference_count=reference_count,
    )
    if resolved.steps <= 0 or resolved.cfg <= 0:
        raise ValueError(f"Resolved {description} steps and CFG must be positive")
    return resolved


def resolve_image_generation(
    generation: object,
    adapter_name: str,
    defaults: dict[str, Any] | None = None,
) -> ResolvedImageGeneration:
    return _resolve_image_generation(
        generation, adapter_name, defaults, description="image"
    )


def resolve_start_image_generation(
    generation: object,
    adapter_name: str,
    defaults: dict[str, Any] | None = None,
) -> ResolvedStartImageGeneration:
    return _resolve_image_generation(
        generation, adapter_name, defaults, description="start image"
    )


def build_image_workflow(
    adapter_name: str,
    base_workflow: dict[str, Any],
    generation: ResolvedImageGeneration,
    *,
    shot_number: int,
    shot_name: str,
    role: str,
    reference_names: Sequence[str] = (),
) -> dict[str, Any]:
    try:
        adapter = IMAGE_ADAPTERS[adapter_name]
    except KeyError as error:
        raise ValueError(f"Unknown image adapter: {adapter_name}") from error
    return adapter.build(
        adapter,
        base_workflow,
        generation,
        shot_number=shot_number,
        shot_name=shot_name,
        role=role,
        reference_names=reference_names,
    )


def build_start_image_workflow(
    adapter_name: str,
    base_workflow: dict[str, Any],
    generation: ResolvedStartImageGeneration,
    *,
    shot_number: int,
    shot_name: str,
) -> dict[str, Any]:
    try:
        return build_image_workflow(
            adapter_name,
            base_workflow,
            generation,
            shot_number=shot_number,
            shot_name=shot_name,
            role="start",
        )
    except ValueError as error:
        if str(error) == f"Unknown image adapter: {adapter_name}":
            raise ValueError(f"Unknown start image adapter: {adapter_name}") from error
        raise


def _build_wan22_i2v_workflow(
    base_workflow: dict[str, Any],
    scene: Scene,
    shot: Shot,
    *,
    shot_number: int,
    start_image_name: str,
    end_image_name: str | None = None,
    starting_state: str = "",
) -> dict[str, Any]:
    workflow = copy.deepcopy(base_workflow)
    prompt_parts = [scene.global_prompt]
    if starting_state:
        prompt_parts.append(f"Starting state: {starting_state}")
    if shot.prompt:
        prompt_parts.append(f"Current action: {shot.prompt}")
    if shot.camera:
        prompt_parts.append(f"Camera: {shot.camera}")
    _node_inputs(workflow, "6")["text"] = ", ".join(
        part for part in prompt_parts if part
    )
    _node_inputs(workflow, "7")["text"] = ", ".join(
        part for part in (scene.negative_prompt, shot.negative_prompt) if part
    )
    _node_inputs(workflow, "52")["image"] = start_image_name
    conditioning = _node_inputs(workflow, "50")
    conditioning.update(
        {
            "width": scene.width,
            "height": scene.height,
            "length": shot.frames,
            "batch_size": 1,
            "start_image": ["52", 0],
        }
    )
    if end_image_name:
        workflow["50"]["class_type"] = "WanFirstLastFrameToVideo"
        conditioning["end_image"] = ["53", 0]
        workflow["53"] = {
            "class_type": "LoadImage",
            "inputs": {"image": end_image_name},
        }
    else:
        workflow["50"]["class_type"] = "WanImageToVideo"
        conditioning.pop("end_image", None)
        workflow.pop("53", None)
    _node_inputs(workflow, "57").update(
        {
            "noise_seed": shot.seed,
            "steps": scene.steps,
            "cfg": shot.cfg,
            "start_at_step": 0,
            "end_at_step": scene.transition_step,
        }
    )
    _node_inputs(workflow, "58").update(
        {
            "steps": scene.steps,
            "cfg": shot.cfg,
            "start_at_step": scene.transition_step,
            "end_at_step": 10000,
        }
    )
    _node_inputs(workflow, "47").update(
        {
            "filename_prefix": f"scene/{shot_number:03d}-{slugify(shot.name)}",
            "fps": scene.fps,
        }
    )
    return workflow


VIDEO_ADAPTERS = {
    "wan22_i2v": VideoAdapter(name="wan22_i2v", build=_build_wan22_i2v_workflow)
}


def get_video_adapter(name: str) -> VideoAdapter:
    try:
        return VIDEO_ADAPTERS[name]
    except KeyError as error:
        raise ValueError(f"Unknown video adapter: {name}") from error


def build_shot_workflow(
    adapter_name: str,
    base_workflow: dict[str, Any],
    scene: Scene,
    shot: Shot,
    **kwargs: Any,
) -> dict[str, Any]:
    return get_video_adapter(adapter_name).build(base_workflow, scene, shot, **kwargs)
