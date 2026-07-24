from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any, Callable

from runpod_video_automation.scene import Scene, Shot, StartImageGeneration, slugify


@dataclass(frozen=True)
class ResolvedStartImageGeneration:
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

    def metadata(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StartImageAdapter:
    name: str
    checkpoint: str
    steps: int
    cfg: float
    sampler_name: str
    scheduler: str
    model_input: str
    supports_negative_prompt: bool


@dataclass(frozen=True)
class VideoAdapter:
    name: str
    build: Callable[..., dict[str, Any]]
    output_suffix: str = ".webm"


START_IMAGE_ADAPTERS = {
    "z_image_turbo": StartImageAdapter(
        name="z_image_turbo",
        checkpoint="cyberrealisticZImage_v50.safetensors",
        steps=8,
        cfg=1.0,
        sampler_name="res_multistep",
        scheduler="simple",
        model_input="unet_name",
        supports_negative_prompt=False,
    ),
    "sdxl": StartImageAdapter(
        name="sdxl",
        checkpoint="sd_xl_base_1.0.safetensors",
        steps=30,
        cfg=7.0,
        sampler_name="dpmpp_2m",
        scheduler="karras",
        model_input="ckpt_name",
        supports_negative_prompt=True,
    ),
}


def _node_inputs(workflow: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = workflow.get(node_id)
    inputs = node.get("inputs") if isinstance(node, dict) else None
    if not isinstance(inputs, dict):
        raise ValueError(f"Workflow adapter requires node {node_id}")
    return inputs


def resolve_start_image_generation(
    generation: StartImageGeneration,
    adapter_name: str,
    defaults: dict[str, Any] | None = None,
) -> ResolvedStartImageGeneration:
    requested_adapter = generation.adapter or adapter_name
    if requested_adapter != adapter_name:
        raise ValueError(
            f"Start image requests adapter {requested_adapter!r}, but the selected "
            f"workflow uses {adapter_name!r}"
        )
    try:
        adapter = START_IMAGE_ADAPTERS[adapter_name]
    except KeyError as error:
        raise ValueError(f"Unknown start image adapter: {adapter_name}") from error
    configured = defaults or {}

    def setting(name: str, explicit: object, fallback: object) -> object:
        return explicit if explicit is not None else configured.get(name, fallback)

    negative_prompt = generation.negative_prompt
    if negative_prompt and not adapter.supports_negative_prompt:
        raise ValueError(
            f"Start image adapter {adapter_name!r} does not support negative prompting"
        )
    checkpoint = setting("checkpoint", generation.checkpoint, adapter.checkpoint)
    steps = setting("steps", generation.steps, adapter.steps)
    cfg = setting("cfg", generation.cfg, adapter.cfg)
    sampler_name = setting(
        "sampler_name", generation.sampler_name, adapter.sampler_name
    )
    scheduler = setting("scheduler", generation.scheduler, adapter.scheduler)
    if not isinstance(checkpoint, str) or not checkpoint:
        raise ValueError("Resolved start image checkpoint must be a non-empty string")
    if not isinstance(sampler_name, str) or not sampler_name:
        raise ValueError("Resolved start image sampler_name must be a non-empty string")
    if not isinstance(scheduler, str) or not scheduler:
        raise ValueError("Resolved start image scheduler must be a non-empty string")
    if isinstance(steps, bool) or not isinstance(steps, (int, float)):
        raise ValueError("Resolved start image steps must be numeric")
    if isinstance(cfg, bool) or not isinstance(cfg, (int, float)):
        raise ValueError("Resolved start image CFG must be numeric")
    resolved = ResolvedStartImageGeneration(
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
    )
    if resolved.steps <= 0 or resolved.cfg <= 0:
        raise ValueError("Resolved start image steps and CFG must be positive")
    return resolved


def build_start_image_workflow(
    adapter_name: str,
    base_workflow: dict[str, Any],
    generation: ResolvedStartImageGeneration,
    *,
    shot_number: int,
    shot_name: str,
) -> dict[str, Any]:
    try:
        adapter = START_IMAGE_ADAPTERS[adapter_name]
    except KeyError as error:
        raise ValueError(f"Unknown start image adapter: {adapter_name}") from error
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
    _node_inputs(workflow, "9")["filename_prefix"] = (
        f"generated/{shot_number:03d}-{slugify(shot_name)}"
    )
    return workflow


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
