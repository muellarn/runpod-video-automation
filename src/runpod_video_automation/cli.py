from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from runpod_video_automation.adapters import (
    ResolvedImageGeneration,
    build_image_workflow,
    build_shot_workflow,
    get_video_adapter,
    resolve_image_generation,
)
from runpod_video_automation.comfy_client import ComfyClient
from runpod_video_automation.config import ModelFile, Profile, WorkflowSelection
from runpod_video_automation.remote import RemoteWorker
from runpod_video_automation.render_metadata import (
    build_generated_image_inputs,
    build_shot_inputs,
    build_start_image_inputs,
    fingerprint,
    read_metadata,
    sha256_file,
    validate_generated_image_metadata,
    validate_shot_metadata,
    validate_start_image_metadata,
    write_generated_image_metadata,
    write_render_manifest,
    write_shot_metadata,
    write_start_image_metadata,
)
from runpod_video_automation.runpod_client import RunPodClient
from runpod_video_automation.scene import (
    ImageGeneration,
    Scene,
    Shot,
    concatenate_webm,
    extract_last_frame,
    slugify,
)
from runpod_video_automation.workflow import apply_overrides, load_workflow


PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass
class _ImageGenerationPlan:
    index: int
    shot: Shot
    role: str
    raw: ImageGeneration
    selection: WorkflowSelection
    generation: ResolvedImageGeneration
    workflow: dict[str, Any] | None = None
    workflow_sha256: str | None = None
    reference_paths: tuple[Path, ...] | None = None
    inputs: dict[str, Any] | None = None
    image: Path | None = None
    selected: bool = False
    required_by_start_approval: bool = False

    @property
    def legacy_start(self) -> bool:
        return (
            self.role == "start"
            and self.raw.workflow == "start_image"
            and not self.raw.reference_images
        )


def _profile_path(value: str | None) -> Path:
    path = Path(value or os.environ.get("RUNPOD_VIDEO_PROFILE", "profiles/wan22-i2v-fp8.json"))
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _scene_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_dir():
        path = path / "scene.json"
    if not path.is_file():
        raise ValueError(f"Scene manifest not found: {path}")
    return path.resolve()


def _scene_output_root(scene_path: Path, output: str | None) -> Path:
    return Path(output) if output else scene_path.parent / "output"


def _project_path(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _workflow_selection(
    profile: Profile,
    name: str,
    *,
    path: str | None,
    adapter: str | None,
    model_groups: list[str] | None,
) -> WorkflowSelection:
    return profile.select_workflow(
        name,
        path=_project_path(path),
        adapter=adapter,
        model_groups=tuple(model_groups) if model_groups is not None else None,
    )


def _api_key() -> str:
    value = os.environ.get("RUNPOD_API_KEY")
    if not value:
        raise RuntimeError("RUNPOD_API_KEY is not set")
    return value


def _ssh_key(value: str | None) -> Path:
    path = Path(value or os.environ.get("RUNPOD_VIDEO_SSH_KEY", "~/.ssh/id_ed25519"))
    path = path.expanduser()
    if not path.is_file() or not path.with_suffix(path.suffix + ".pub").is_file():
        raise RuntimeError(f"SSH key pair not found at {path}")
    return path


def inventory(args: argparse.Namespace) -> None:
    with RunPodClient(_api_key()) as client:
        data = {
            "pods": [
                {
                    "id": pod.get("id"),
                    "name": pod.get("name"),
                    "status": pod.get("desiredStatus") or pod.get("status"),
                    "gpu": (pod.get("gpu") or {}).get("displayName"),
                    "cost_per_hour": pod.get("costPerHr") or pod.get("adjustedCostPerHr"),
                }
                for pod in client.list_pods()
            ],
            "network_volumes": [
                {
                    "id": volume.get("id"),
                    "name": volume.get("name"),
                    "size": volume.get("size"),
                    "data_center_id": volume.get("dataCenterId"),
                }
                for volume in client.list_network_volumes()
            ],
        }
    print(json.dumps(data, indent=2))


def plan(args: argparse.Namespace) -> None:
    profile = Profile.load(_profile_path(args.profile))
    print(f"Profile: {profile.name}")
    print(f"Image: {profile.image}")
    print(f"Data center: {profile.data_center_id}")
    print(f"Persistent volume: {profile.volume_name} ({profile.volume_size_gb} GB)")
    print(f"GPU fallback order: {', '.join(profile.gpu_type_ids)}")
    for name, models in profile.model_groups.items():
        print(f"Model group {name}: {len(models)} file(s)")
    for name, workflow in profile.workflows.items():
        print(
            f"Workflow {name}: {workflow.path} via {workflow.adapter} "
            f"[{', '.join(workflow.model_groups)}]"
        )
    print("Lifecycle: create/reuse volume -> create pod -> SSH -> models -> workflow -> download -> terminate")


def _parse_image(value: str) -> tuple[Path, str | None]:
    local, separator, remote = value.partition(":")
    path = Path(local)
    if not path.is_file():
        raise ValueError(f"Input image not found: {path}")
    return path, remote if separator and remote else None


def _parse_shots(value: str) -> tuple[int, ...]:
    selected: set[int] = set()
    try:
        for part in value.split(","):
            part = part.strip()
            if not part:
                raise ValueError
            if "-" in part:
                start_text, separator, end_text = part.partition("-")
                if not separator:
                    raise ValueError
                start = int(start_text)
                end = int(end_text)
                if start <= 0 or end < start:
                    raise ValueError
                selected.update(range(start, end + 1))
            else:
                number = int(part)
                if number <= 0:
                    raise ValueError
                selected.add(number)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "shots must use positive numbers and ranges such as 1,3-5"
        ) from error
    return tuple(sorted(selected))


def _validate_execution_args(args: argparse.Namespace) -> None:
    if getattr(args, "restart", False) and not args.pod_id:
        raise ValueError("--restart requires --pod-id")
    if getattr(args, "retries", 2) < 0:
        raise ValueError("--retries cannot be negative")
    idle_minutes = getattr(args, "idle_stop_minutes", None)
    if idle_minutes is not None:
        if idle_minutes <= 0:
            raise ValueError("--idle-stop-minutes must be positive")
        if not args.keep_pod:
            raise ValueError("--idle-stop-minutes requires --keep-pod")


def _retry_operation(
    label: str,
    retries: int,
    operation: Callable[[], Any],
    *,
    before_retry: Callable[[], None] | None = None,
) -> Any:
    for attempt in range(retries + 1):
        try:
            return operation()
        except Exception as error:
            if attempt == retries:
                raise
            print(
                f"{label} failed ({error}); retrying {attempt + 1}/{retries}",
                flush=True,
            )
            if before_retry:
                try:
                    before_retry()
                except Exception as cleanup_error:
                    print(f"Retry cleanup failed: {cleanup_error}", flush=True)
            time.sleep(min(2**attempt, 10))
    raise RuntimeError(f"{label} did not run")


def _queue_with_retries(
    comfy: ComfyClient,
    workflow: dict[str, Any],
    args: argparse.Namespace,
    status_callback: Callable[[str], None],
) -> tuple[str, dict[str, Any]]:
    retries = getattr(args, "retries", 2)
    return _retry_operation(
        "ComfyUI workflow",
        retries,
        lambda: comfy.queue_and_wait(
            workflow,
            timeout_seconds=args.workflow_timeout,
            status_callback=status_callback,
        ),
        before_retry=getattr(comfy, "interrupt_and_clear", None),
    )


def _schedule_idle_stop(
    pod_id: str,
    ssh_key: Path,
    args: argparse.Namespace,
) -> None:
    idle_minutes = getattr(args, "idle_stop_minutes", None)
    if idle_minutes is None:
        return
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "runpod_video_automation.idle_watchdog",
            pod_id,
            "--ssh-key",
            str(ssh_key),
            "--idle-minutes",
            str(idle_minutes),
            "--start-timeout",
            str(args.start_timeout),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(
        f"Idle-stop watchdog {process.pid} will stop pod {pod_id} after "
        f"{idle_minutes:g} idle minute(s)"
    )


@contextmanager
def _remote_session(
    args: argparse.Namespace,
    profile: Profile,
    *,
    models: tuple[ModelFile, ...],
) -> Iterator[tuple[RemoteWorker, str, float | None, dict[str, Any]]]:
    ssh_key = _ssh_key(args.ssh_key)
    requested_pod_id: str | None = args.pod_id
    pod_id: str | None = requested_pod_id
    with RunPodClient(_api_key()) as runpod:
        try:
            if requested_pod_id:
                print(f"Reusing pod {requested_pod_id}")
                existing_pod = runpod.get_pod(requested_pod_id)
                existing_status = existing_pod.get("desiredStatus") or existing_pod.get(
                    "status"
                )
                if existing_status in {"EXITED", "STOPPED"}:
                    print(f"Starting stopped pod {requested_pod_id}")
                    runpod.start_pod(requested_pod_id)
            else:
                public_key = ssh_key.with_suffix(ssh_key.suffix + ".pub").read_text().strip()
                volume, created = runpod.find_or_create_volume(
                    name=profile.volume_name,
                    size=profile.volume_size_gb,
                    data_center_id=profile.data_center_id,
                )
                print(
                    f"Network volume: {volume['id']} "
                    f"({'created' if created else 'reused'})"
                )
                pod = runpod.create_pod(
                    name=f"runpod-video-{profile.name}",
                    image=profile.image,
                    gpu_type_ids=profile.gpu_type_ids,
                    network_volume_id=volume["id"],
                    public_key=public_key,
                    container_disk_gb=profile.container_disk_gb,
                    min_ram_per_gpu=profile.min_ram_per_gpu,
                    min_vcpu_per_gpu=profile.min_vcpu_per_gpu,
                )
                pod_id = pod["id"]
                print(f"Pod {pod_id} created")
            if pod_id is None:
                raise RuntimeError("No Pod is available")
            pod = runpod.wait_until_running(pod_id, timeout_seconds=args.start_timeout)
            raw_hourly_cost = pod.get("costPerHr") or pod.get("adjustedCostPerHr")
            hourly_cost = float(raw_hourly_cost) if raw_hourly_cost is not None else None
            if hourly_cost is not None and hourly_cost > profile.max_hourly_cost:
                raise RuntimeError(
                    f"Pod costs ${hourly_cost:.2f}/h, above profile limit "
                    f"${profile.max_hourly_cost:.2f}/h"
                )
            mappings = pod.get("portMappings") or {}
            ssh_port = int(mappings.get("22") or mappings[22])
            remote = RemoteWorker(
                host=str(pod["publicIp"]), port=ssh_port, ssh_key=ssh_key
            )
            remote.wait_for_ssh()
            remote.ensure_models(models, profile.model_path_aliases)
            if profile.comfy_args:
                remote.ensure_comfy_args(
                    profile.comfy_args,
                    system_packages=profile.system_packages,
                )
            elif profile.system_packages:
                remote.ensure_system_packages(profile.system_packages)
            yield remote, pod_id, hourly_cost, pod
        finally:
            if pod_id and args.stop_pod:
                print(f"Stopping pod {pod_id}")
                runpod.stop_pod(pod_id)
            elif pod_id and not args.keep_pod:
                print(f"Terminating pod {pod_id}")
                runpod.terminate_pod(pod_id)
            elif pod_id:
                print(f"Leaving pod {pod_id} running")
                _schedule_idle_stop(pod_id, ssh_key, args)


@contextmanager
def _worker_session(
    args: argparse.Namespace,
    profile: Profile,
    *,
    models: tuple[ModelFile, ...] | None = None,
) -> Iterator[ComfyClient]:
    selected_models = (
        models
        if models is not None
        else profile.models_for_groups(profile.default_model_groups)
    )
    with _remote_session(args, profile, models=selected_models) as remote_details:
        remote, pod_id, hourly_cost, pod = remote_details
        with remote.comfy_tunnel() as base_url:
            comfy = ComfyClient(base_url)
            try:
                stats = comfy.wait_until_ready()
                devices = stats.get("devices", [])
                if devices:
                    print(f"ComfyUI device: {devices[0].get('name', 'unknown')}")
                comfy.runtime_metadata = {
                    "pod_id": pod_id,
                    "gpu": (
                        devices[0].get("name", "unknown")
                        if devices
                        else (pod.get("gpu") or {}).get("displayName", "unknown")
                    ),
                    "cost_per_hour": hourly_cost,
                }
                if getattr(args, "restart", False):
                    print("Interrupting active ComfyUI execution and clearing queue")
                    comfy.interrupt_and_clear()
                yield comfy
            finally:
                comfy.close()


def setup(args: argparse.Namespace) -> None:
    if not args.apply:
        raise RuntimeError("Refusing to create billable resources without --apply")
    _validate_execution_args(args)
    profile = Profile.load(_profile_path(args.profile))
    groups = tuple(args.model_group or profile.default_model_groups)
    models = profile.models_for_groups(groups)
    print(f"Model groups: {', '.join(groups) if groups else '(none)'}")
    print(f"Model files: {len(models)}")
    with _remote_session(args, profile, models=models):
        print("Model setup complete")


def run(args: argparse.Namespace) -> None:
    if not args.apply:
        raise RuntimeError("Refusing to create billable resources without --apply")
    _validate_execution_args(args)
    profile = Profile.load(_profile_path(args.profile))
    default_groups = (
        profile.workflows["video"].model_groups
        if "video" in profile.workflows
        else profile.default_model_groups
    )
    groups = tuple(args.model_group or default_groups)
    models = profile.models_for_groups(groups)
    workflow_path = Path(args.workflow).expanduser().resolve()
    workflow = load_workflow(workflow_path)
    apply_overrides(workflow, args.set or [])
    with _worker_session(args, profile, models=models) as comfy:
        for image_arg in args.image or []:
            local_path, remote_name = _parse_image(image_arg)
            uploaded_name = _retry_operation(
                f"Upload {local_path}",
                getattr(args, "retries", 2),
                lambda: comfy.upload_image(local_path, remote_name),
            )
            print(f"Uploaded input image: {uploaded_name}")
        _, history = _queue_with_retries(
            comfy,
            workflow,
            args,
            lambda message: print(message, flush=True),
        )
        outputs = _retry_operation(
            "Output download",
            getattr(args, "retries", 2),
            lambda: comfy.download_outputs(history, Path(args.output)),
        )
        if not outputs:
            raise RuntimeError("The workflow completed without downloadable outputs")
        for output in outputs:
            print(f"Downloaded: {output}")


def _scene_plan(
    scene: Scene,
    image_plans: dict[tuple[int, str], _ImageGenerationPlan],
) -> None:
    print(f"Scene: {scene.title}")
    print(f"Format: {scene.width}x{scene.height} at {scene.fps:g} FPS")
    print(f"Sampling: {scene.steps} steps, transition at {scene.transition_step}")
    print(f"Shots: {len(scene.shots)}, approximately {scene.duration_seconds:.1f} seconds")
    print(f"Global prompt: {scene.global_prompt or '(none)'}")
    print(f"Negative prompt: {scene.negative_prompt or '(none)'}")
    for index, shot in enumerate(scene.shots, start=1):
        if shot.start_image:
            source = str(shot.start_image)
        elif shot.generate_start_image:
            image_plan = image_plans.get((index, "start"))
            if image_plan is None:
                source = "generated start image (unselected shot)"
            else:
                generation = image_plan.generation
                source = (
                    f"generated with {generation.adapter} ({generation.checkpoint}) "
                    f"at {generation.width}x{generation.height}, "
                    f"{generation.reference_count} reference(s)"
                )
        else:
            source = "previous shot's last frame"
        if shot.end_image:
            end = f", end keyframe {shot.end_image}"
        elif shot.generate_end_image:
            end_plan = image_plans.get((index, "end"))
            if end_plan is None:
                end = ", generated end image (unselected shot)"
            else:
                end = (
                    f", generated end image with {end_plan.generation.adapter} "
                    f"({end_plan.generation.reference_count} reference(s))"
                )
        else:
            end = ""
        print(
            f"  {index:03d} {shot.name}: {shot.frames} frames, "
            f"seed {shot.seed}, start {source}{end}"
        )
        print(f"      Action: {shot.prompt or '(global prompt only)'}")
        print(f"      End state: {shot.end_state or '(unspecified)'}")
        print(f"      Camera: {shot.camera or '(unspecified)'}")


def _shot_dir(output_root: Path, index: int, name: str) -> Path:
    return output_root / f"{index:03d}-{slugify(name)}"


def _continuation_path(output_root: Path, index: int, name: str) -> Path:
    return _shot_dir(output_root, index, name) / "continuation.png"


def _snapshot_scene(scene_path: Path, output_root: Path) -> Path:
    destination = output_root / "scene.snapshot.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    shutil.copy2(scene_path, temporary)
    temporary.replace(destination)
    return destination


def _snapshot_input(
    source: Path,
    output_root: Path,
    *,
    index: int,
    role: str,
) -> Path:
    digest = sha256_file(source)
    suffix = source.suffix.lower() or ".img"
    destination = (
        output_root
        / "000-inputs"
        / f"{index:03d}-{role}-{digest[:12]}{suffix}"
    )
    if not destination.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    return destination


def _start_image_metadata_path(output_root: Path, index: int, name: str) -> Path:
    return (
        output_root
        / "000-generated-start-image"
        / f"{index:03d}-{slugify(name)}.metadata.json"
    )


def _generated_image_dir(output_root: Path, role: str) -> Path:
    return output_root / f"000-generated-{role}-image"


def _generated_image_metadata_path(
    output_root: Path, index: int, name: str, role: str
) -> Path:
    return _generated_image_dir(output_root, role) / (
        f"{index:03d}-{slugify(name)}.metadata.json"
    )


def _build_image_generation_plans(
    profile: Profile,
    scene: Scene,
    shot_entries: list[tuple[int, Shot]],
    args: argparse.Namespace,
) -> tuple[dict[tuple[int, str], _ImageGenerationPlan], dict[str, WorkflowSelection]]:
    selections: dict[str, WorkflowSelection] = {}
    plans: dict[tuple[int, str], _ImageGenerationPlan] = {}
    roots: list[tuple[tuple[int, str], bool]] = []
    for index, shot in shot_entries:
        for role, raw in (
            ("start", shot.generate_start_image),
            ("end", shot.generate_end_image),
        ):
            if raw is None:
                continue
            if role == "end" and getattr(args, "start_image_only", False):
                continue
            roots.append(((index, role), role == "start"))

    expanded: set[tuple[tuple[int, str], bool]] = set()
    pending = [(key, True, approve_for_start) for key, approve_for_start in roots]
    while pending:
        key, selected, required_by_start_approval = pending.pop()
        index, role = key
        shot = scene.shots[index - 1]
        raw = (
            shot.generate_start_image
            if role == "start"
            else shot.generate_end_image
        )
        if raw is None:
            raise RuntimeError(
                f"Shot {index} has no configured generated {role} image"
            )
        plan = plans.get(key)
        if plan is None:
            selection = selections.get(raw.workflow)
            if selection is None:
                if raw.workflow == "start_image":
                    selection = _workflow_selection(
                        profile,
                        raw.workflow,
                        path=getattr(args, "start_image_workflow", None),
                        adapter=getattr(args, "start_image_adapter", None),
                        model_groups=getattr(args, "start_image_model_group", None),
                    )
                else:
                    selection = profile.select_workflow(raw.workflow)
                selections[raw.workflow] = selection
            plan = _ImageGenerationPlan(
                index=index,
                shot=shot,
                role=role,
                raw=raw,
                selection=selection,
                generation=resolve_image_generation(
                    raw, selection.adapter, selection.defaults
                ),
            )
            plans[key] = plan
        plan.selected = plan.selected or selected
        plan.required_by_start_approval = (
            plan.required_by_start_approval or required_by_start_approval
        )

        expansion = (key, required_by_start_approval)
        if expansion in expanded:
            continue
        expanded.add(expansion)
        for reference in raw.reference_images:
            dependency: tuple[int, str] | None = None
            if reference.source == "current_start" and shot.generate_start_image:
                dependency = (index, "start")
            elif reference.source == "shot_start":
                referenced_index = int(reference.shot)
                if scene.shots[referenced_index - 1].generate_start_image:
                    dependency = (referenced_index, "start")
            elif reference.source == "shot_end":
                referenced_index = int(reference.shot)
                if scene.shots[referenced_index - 1].generate_end_image:
                    dependency = (referenced_index, "end")
            if dependency is not None:
                pending.append((dependency, False, required_by_start_approval))

    ordered = dict(
        sorted(plans.items(), key=lambda item: (item[0][0], item[0][1] == "end"))
    )
    return ordered, selections


def _load_image_plan_workflows(
    plans: dict[tuple[int, str], _ImageGenerationPlan],
) -> None:
    loaded: dict[str, tuple[dict[str, Any], str]] = {}
    for plan in plans.values():
        cached = loaded.get(plan.selection.name)
        if cached is None:
            workflow = load_workflow(plan.selection.path)
            cached = (workflow, fingerprint(workflow))
            loaded[plan.selection.name] = cached
        plan.workflow, plan.workflow_sha256 = cached


def _metadata_output_path(metadata_path: Path, output_root: Path) -> Path | None:
    metadata = read_metadata(metadata_path)
    output = metadata.get("output") if metadata is not None else None
    if not isinstance(output, dict) or not isinstance(output.get("path"), str):
        return None
    relative_path = Path(output["path"])
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    resolved_root = output_root.resolve()
    path = (resolved_root / relative_path).resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError:
        return None
    expected_hash = output.get("sha256")
    if not path.is_file() or not isinstance(expected_hash, str):
        return None
    return path if sha256_file(path) == expected_hash else None


def _build_generation_inputs(
    plan: _ImageGenerationPlan,
    profile: Profile,
    references: tuple[Path, ...],
) -> dict[str, Any]:
    if plan.workflow_sha256 is None:
        raise RuntimeError("Image workflow fingerprint is unavailable")
    if plan.legacy_start:
        return build_start_image_inputs(
            plan.shot,
            index=plan.index,
            profile=profile,
            generation=plan.generation,
            start_workflow=plan.selection,
            start_workflow_sha256=plan.workflow_sha256,
        )
    return build_generated_image_inputs(
        plan.shot,
        index=plan.index,
        role=plan.role,
        profile=profile,
        generation=plan.generation,
        image_workflow=plan.selection,
        image_workflow_sha256=plan.workflow_sha256,
        reference_images=references,
    )


def _validate_generation_metadata(
    plan: _ImageGenerationPlan,
    inputs: dict[str, Any],
    output_root: Path,
) -> tuple[Path | None, list[str]]:
    metadata_path = _generated_image_metadata_path(
        output_root, plan.index, plan.shot.name, plan.role
    )
    metadata = read_metadata(metadata_path)
    if plan.legacy_start:
        return validate_start_image_metadata(metadata, inputs, output_root)
    return validate_generated_image_metadata(metadata, inputs, output_root)


def _resolve_generation_references(
    plan: _ImageGenerationPlan,
    *,
    scene: Scene,
    paths: dict[tuple[int, str], Path],
    explicit_references: dict[tuple[int, str, int], Path],
    pending_images: set[tuple[int, str]],
    pending_videos: set[int],
) -> tuple[Path, ...] | None:
    resolved: list[Path] = []
    for position, reference in enumerate(plan.raw.reference_images, start=1):
        dependency: tuple[int, str] | None = None
        if reference.path is not None:
            path = explicit_references[(plan.index, plan.role, position)]
        elif reference.source == "current_start":
            dependency = (plan.index, "start")
            if dependency in pending_images:
                return None
            if (
                plan.shot.start_image is None
                and plan.shot.generate_start_image is None
            ):
                if plan.index - 1 in pending_videos:
                    return None
                path = paths.get((plan.index - 1, "continuation"))
            else:
                path = paths.get(dependency)
        elif reference.source in {"shot_start", "shot_end"}:
            dependency = (
                int(reference.shot),
                "start" if reference.source == "shot_start" else "end",
            )
            if dependency in pending_images:
                return None
            referenced_shot = scene.shots[dependency[0] - 1]
            if (
                dependency[1] == "start"
                and referenced_shot.start_image is None
                and referenced_shot.generate_start_image is None
            ):
                if dependency[0] - 1 in pending_videos:
                    return None
                path = paths.get((dependency[0] - 1, "continuation"))
            else:
                path = paths.get(dependency)
        elif reference.source == "shot_continuation":
            referenced_index = int(reference.shot)
            if referenced_index in pending_videos:
                return None
            path = paths.get((referenced_index, "continuation"))
            if path is not None and not path.is_file():
                path = None
            if path is None:
                raise ValueError(
                    f"Shot {plan.index} {plan.role} image reference {position} "
                    f"requires shot {referenced_index} continuation, but it is "
                    "unavailable"
                )
            resolved.append(path)
            continue
        else:
            raise RuntimeError("Unknown image reference descriptor")

        if path is None:
            source = reference.source or str(reference.path)
            raise ValueError(
                f"Shot {plan.index} {plan.role} image reference {position} "
                f"({source}) is unavailable"
            )
        resolved.append(path)
    return tuple(resolved)


def _print_resume_differences(index: int, differences: list[str]) -> None:
    print(f"Resume: shot {index} must be rendered again:")
    for difference in differences:
        print(f"  - {difference}")


def _prune_old_shot_videos(shot_dir: Path, keep: Path, suffix: str) -> None:
    for path in shot_dir.glob(f"*{suffix}"):
        if path != keep:
            path.unlink()


def _single_existing_output(
    directory: Path,
    *,
    prefix: str,
    suffixes: set[str],
    label: str,
) -> Path | None:
    if not directory.is_dir():
        return None
    matches = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.suffix.lower() in suffixes
    )
    if len(matches) > 1:
        raise ValueError(
            f"Cannot backfill {label}: found multiple matching files: "
            f"{', '.join(str(path) for path in matches)}"
        )
    return matches[0] if matches else None


def _backfill_scene_metadata(
    *,
    scene_path: Path,
    scene: Scene,
    shot_entries: list[tuple[int, Shot]],
    output_root: Path,
    profile: Profile,
    video_selection: WorkflowSelection,
    video_output_suffix: str,
    image_plans: dict[tuple[int, str], _ImageGenerationPlan],
    legacy_start_selection: WorkflowSelection | None,
) -> None:
    if not output_root.is_dir():
        raise ValueError(f"Scene output directory not found: {output_root}")
    video_workflow_sha256 = fingerprint(load_workflow(video_selection.path))
    _load_image_plan_workflows(image_plans)
    legacy_start_workflow_sha256 = (
        fingerprint(load_workflow(legacy_start_selection.path))
        if legacy_start_selection is not None
        and not any(
            plan.selection.name == legacy_start_selection.name
            for plan in image_plans.values()
        )
        else next(
            (
                plan.workflow_sha256
                for plan in image_plans.values()
                if plan.selection.name == "start_image"
            ),
            None,
        )
    )

    paths: dict[tuple[int, str], Path] = {}
    explicit_references: dict[tuple[int, str, int], Path] = {}
    generated_image_sidecars: list[
        tuple[_ImageGenerationPlan, Path, dict[str, Any], Path]
    ] = []
    shot_plans: list[tuple[Path, dict[str, Any], Path, Path]] = []
    runtime = {
        "backfilled": True,
        "provenance": "inferred_from_existing_outputs",
        "historical_render_time_unknown": True,
    }

    for index, shot in enumerate(scene.shots, start=1):
        if shot.start_image is not None:
            paths[(index, "start")] = _snapshot_input(
                shot.start_image,
                output_root,
                index=index,
                role="start",
            )
        if shot.end_image is not None:
            paths[(index, "end")] = _snapshot_input(
                shot.end_image,
                output_root,
                index=index,
                role="end",
            )
        continuation = _continuation_path(output_root, index, shot.name)
        if continuation.is_file():
            paths[(index, "continuation")] = continuation
        if shot.start_image is None and shot.generate_start_image is None:
            previous = paths.get((index - 1, "continuation"))
            if previous is not None:
                paths[(index, "start")] = previous
        for role, generation in (
            ("start", shot.generate_start_image),
            ("end", shot.generate_end_image),
        ):
            if generation is None or (index, role) in image_plans:
                continue
            existing = _metadata_output_path(
                _generated_image_metadata_path(output_root, index, shot.name, role),
                output_root,
            )
            if existing is not None:
                paths[(index, role)] = existing

    for plan in image_plans.values():
        for position, reference in enumerate(plan.raw.reference_images, start=1):
            if reference.path is not None:
                explicit_references[
                    (plan.index, plan.role, position)
                ] = _snapshot_input(
                    reference.path,
                    output_root,
                    index=plan.index,
                    role=f"{plan.role}-reference-{position}",
                )

    # Validate the complete adoption set before writing any metadata sidecars.
    generation_indices = sorted({index for index, _ in image_plans})
    for index in generation_indices:
        shot = scene.shots[index - 1]
        if shot.generate_start_image is None and shot.start_image is None:
            previous = paths.get((index - 1, "continuation"))
            if previous is not None:
                paths[(index, "start")] = previous
        for role in ("start", "end"):
            plan = image_plans.get((index, role))
            if plan is None:
                continue
            references = _resolve_generation_references(
                plan,
                scene=scene,
                paths=paths,
                explicit_references=explicit_references,
                pending_images=set(),
                pending_videos=set(),
            )
            if references is None:
                raise ValueError(
                    f"Cannot backfill shot {index}: {role} image references are "
                    "unavailable"
                )
            plan.reference_paths = references
            inputs = _build_generation_inputs(plan, profile, references)
            plan.inputs = inputs
            metadata_path = _generated_image_metadata_path(
                output_root, index, shot.name, role
            )
            if metadata_path.is_file():
                existing_image, differences = _validate_generation_metadata(
                    plan, inputs, output_root
                )
                if existing_image is None or differences:
                    raise ValueError(
                        f"Cannot backfill shot {index}: existing {role} image metadata "
                        "does not match the current scene:\n  - "
                        + "\n  - ".join(differences)
                    )
                plan.image = existing_image
                paths[(index, role)] = existing_image
                print(f"Metadata already exists: {metadata_path}")
                continue
            output_prefix = f"{index:03d}-{slugify(shot.name)}"
            if role == "end":
                output_prefix += "-end"
            image = _single_existing_output(
                _generated_image_dir(output_root, role),
                prefix=output_prefix + "_",
                suffixes=IMAGE_SUFFIXES,
                label=f"generated {role} image for shot {index}",
            )
            if image is None:
                continue
            plan.image = image
            paths[(index, role)] = image
            generated_image_sidecars.append((plan, metadata_path, inputs, image))

    for index, shot in shot_entries:
        shot_dir = _shot_dir(output_root, index, shot.name)
        metadata_path = shot_dir / "metadata.json"
        metadata_exists = metadata_path.is_file()
        continuation = _continuation_path(output_root, index, shot.name)
        video: Path | None = None
        if not metadata_exists:
            video = _single_existing_output(
                shot_dir,
                prefix="",
                suffixes={video_output_suffix},
                label=f"video for shot {index}",
            )
            if video is None and not continuation.is_file():
                print(f"Backfill: no existing output for shot {index}; skipping")
                continue
            if video is None:
                raise ValueError(f"Cannot backfill shot {index}: video is missing")
            if not continuation.is_file():
                raise ValueError(
                    f"Cannot backfill shot {index}: continuation image is missing"
                )
        if shot.start_image is not None or shot.generate_start_image is not None:
            start_image = paths.get((index, "start"))
            if start_image is None:
                raise ValueError(
                    f"Cannot backfill shot {index}: generated start image is missing"
                )
        else:
            start_image = paths.get((index - 1, "continuation"))
            if start_image is None:
                raise ValueError(
                    f"Cannot backfill shot {index}: previous continuation is missing"
                )
        start_plan = image_plans.get((index, "start"))
        end_plan = image_plans.get((index, "end"))
        end_image = paths.get((index, "end"))
        if shot.generate_end_image is not None and end_image is None:
            raise ValueError(
                f"Cannot backfill shot {index}: generated end image is missing"
            )
        inputs = build_shot_inputs(
            scene,
            shot,
            index=index,
            start_image=start_image,
            profile=profile,
            video_workflow=video_selection,
            video_workflow_sha256=video_workflow_sha256,
            video_output_suffix=video_output_suffix,
            start_workflow=(
                start_plan.selection
                if start_plan is not None
                else legacy_start_selection
            ),
            start_workflow_sha256=(
                start_plan.workflow_sha256
                if start_plan is not None
                else legacy_start_workflow_sha256
            ),
            generation=start_plan.generation if start_plan is not None else None,
            starting_state=(scene.shots[index - 2].end_state if index > 1 else ""),
            end_image=end_image,
            end_generation=end_plan.generation if end_plan is not None else None,
            end_workflow=end_plan.selection if end_plan is not None else None,
            end_workflow_sha256=(
                end_plan.workflow_sha256 if end_plan is not None else None
            ),
            start_generation_fingerprint=(
                fingerprint(start_plan.inputs)
                if start_plan is not None
                and not start_plan.legacy_start
                and start_plan.inputs is not None
                else None
            ),
            end_generation_fingerprint=(
                fingerprint(end_plan.inputs)
                if end_plan is not None and end_plan.inputs is not None
                else None
            ),
        )
        if metadata_exists:
            existing_video, differences = validate_shot_metadata(
                read_metadata(metadata_path), inputs, output_root
            )
            if existing_video is None or differences:
                raise ValueError(
                    f"Cannot backfill shot {index}: existing shot metadata does "
                    "not match the current scene or outputs:\n  - "
                    + "\n  - ".join(differences)
                )
            print(f"Metadata already exists: {metadata_path}")
            continue
        if video is None:
            raise RuntimeError("Backfill video validation did not run")
        shot_plans.append((metadata_path, inputs, video, continuation))

    _snapshot_scene(scene_path, output_root)
    for plan, metadata_path, inputs, image in generated_image_sidecars:
        writer = (
            write_start_image_metadata
            if plan.legacy_start
            else write_generated_image_metadata
        )
        writer(
            metadata_path,
            inputs=inputs,
            image=image,
            output_root=output_root,
            runtime=runtime,
            elapsed_seconds=0,
        )
        print(f"Backfilled {plan.role} image metadata: {metadata_path}")
    for metadata_path, inputs, video, continuation in shot_plans:
        write_shot_metadata(
            metadata_path,
            inputs=inputs,
            video=video,
            continuation=continuation,
            output_root=output_root,
            runtime=runtime,
            elapsed_seconds=0,
        )
        print(f"Backfilled shot metadata: {metadata_path}")

    final_video = output_root / f"{slugify(scene.title)}{video_output_suffix}"
    write_render_manifest(
        output_root,
        scene_path,
        scene,
        selected_shots=[index for index, _ in shot_entries],
        final_video=final_video if final_video.is_file() else None,
        provenance="inferred_from_existing_outputs",
    )
    print(
        f"Metadata backfill complete: {len(shot_plans)} shot(s), "
        f"{len(generated_image_sidecars)} generated image(s)"
    )


def _selected_shot_entries(
    scene: Scene, args: argparse.Namespace
) -> list[tuple[int, Shot]]:
    shot_number = getattr(args, "shot", None)
    shot_numbers = getattr(args, "shots", None)
    selected = shot_numbers or ((shot_number,) if shot_number is not None else None)
    if selected is None:
        return list(enumerate(scene.shots, start=1))
    invalid = [
        number for number in selected if number < 1 or number > len(scene.shots)
    ]
    if invalid:
        raise ValueError(
            f"Selected shots must be between 1 and {len(scene.shots)}, got "
            f"{', '.join(map(str, invalid))}"
        )
    return [(number, scene.shots[number - 1]) for number in selected]


def render_scene(args: argparse.Namespace) -> None:
    scene_path = _scene_path(args.manifest)
    scene = Scene.load(scene_path)
    shot_entries = _selected_shot_entries(scene, args)
    selected_numbers = {index for index, _ in shot_entries}
    all_shots_selected = len(selected_numbers) == len(scene.shots)
    profile = Profile.load(_profile_path(args.profile))
    start_image_only = bool(getattr(args, "start_image_only", False))
    generated_images_only = bool(getattr(args, "generated_images_only", False))
    approve_start_images = bool(getattr(args, "approve_start_images", False))
    approve_generated_images = bool(
        getattr(args, "approve_generated_images", False)
    )
    images_only = start_image_only or generated_images_only
    if start_image_only and generated_images_only:
        raise ValueError(
            "Use either --start-image-only or --generated-images-only, not both"
        )
    if start_image_only and (approve_start_images or approve_generated_images):
        raise ValueError(
            "--start-image-only cannot be combined with image approval flags"
        )
    if generated_images_only and (approve_start_images or approve_generated_images):
        raise ValueError(
            "--generated-images-only cannot be combined with image approval flags"
        )
    if approve_start_images and approve_generated_images:
        raise ValueError(
            "Use either --approve-start-images or --approve-generated-images"
        )

    image_plans, image_selections = _build_image_generation_plans(
        profile, scene, shot_entries, args
    )
    if "start_image" not in image_selections and any(
        shot.generate_start_image is not None
        and shot.generate_start_image.workflow == "start_image"
        for shot in scene.shots
    ):
        image_selections["start_image"] = _workflow_selection(
            profile,
            "start_image",
            path=getattr(args, "start_image_workflow", None),
            adapter=getattr(args, "start_image_adapter", None),
            model_groups=getattr(args, "start_image_model_group", None),
        )
    video_selection: WorkflowSelection | None = None
    video_output_suffix = ".webm"
    if not images_only:
        video_selection = _workflow_selection(
            profile,
            "video",
            path=getattr(args, "workflow", None),
            adapter=getattr(args, "video_adapter", None),
            model_groups=getattr(args, "video_model_group", None),
        )
        video_output_suffix = get_video_adapter(
            video_selection.adapter
        ).output_suffix.lower()
    _scene_plan(scene, image_plans)
    if not all_shots_selected:
        print(f"Selected shots: {', '.join(map(str, sorted(selected_numbers)))}")
    if getattr(args, "backfill_metadata", False):
        conflicting = [
            flag
            for enabled, flag in (
                (args.plan, "--plan"),
                (args.apply, "--apply"),
                (start_image_only, "--start-image-only"),
                (generated_images_only, "--generated-images-only"),
                (approve_start_images, "--approve-start-images"),
                (approve_generated_images, "--approve-generated-images"),
                (getattr(args, "resume", False), "--resume"),
                (getattr(args, "restart", False), "--restart"),
                (bool(getattr(args, "pod_id", None)), "--pod-id"),
                (getattr(args, "keep_pod", False), "--keep-pod"),
                (getattr(args, "stop_pod", False), "--stop-pod"),
                (
                    getattr(args, "idle_stop_minutes", None) is not None,
                    "--idle-stop-minutes",
                ),
            )
            if enabled
        ]
        if conflicting:
            raise ValueError(
                "--backfill-metadata cannot be combined with "
                + ", ".join(conflicting)
            )
        if video_selection is None:
            raise RuntimeError("Video workflow selection is unavailable")
        _backfill_scene_metadata(
            scene_path=scene_path,
            scene=scene,
            shot_entries=shot_entries,
            output_root=_scene_output_root(scene_path, args.output),
            profile=profile,
            video_selection=video_selection,
            video_output_suffix=video_output_suffix,
            image_plans=image_plans,
            legacy_start_selection=image_selections.get("start_image"),
        )
        return
    if args.plan:
        if args.apply:
            raise ValueError("Use either --plan or --apply, not both")
        return
    if not args.apply:
        raise RuntimeError("Refusing to create billable resources without --apply")
    _validate_execution_args(args)
    output_root = _scene_output_root(scene_path, args.output)
    if not images_only and shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to assemble scene outputs")
    scene_snapshot = _snapshot_scene(scene_path, output_root)
    print(f"Scene snapshot: {scene_snapshot}")
    paths: dict[tuple[int, str], Path] = {}
    for index, shot in enumerate(scene.shots, start=1):
        if shot.start_image is not None:
            paths[(index, "start")] = _snapshot_input(
                shot.start_image,
                output_root,
                index=index,
                role="start",
            )
        if shot.end_image is not None:
            paths[(index, "end")] = _snapshot_input(
                shot.end_image,
                output_root,
                index=index,
                role="end",
            )
        continuation = _continuation_path(output_root, index, shot.name)
        if continuation.is_file():
            paths[(index, "continuation")] = continuation
        if shot.start_image is None and shot.generate_start_image is None:
            previous = paths.get((index - 1, "continuation"))
            if previous is not None:
                paths[(index, "start")] = previous

    explicit_references: dict[tuple[int, str, int], Path] = {}
    for plan in image_plans.values():
        for position, reference in enumerate(plan.raw.reference_images, start=1):
            if reference.path is None:
                continue
            explicit_references[(plan.index, plan.role, position)] = _snapshot_input(
                reference.path,
                output_root,
                index=plan.index,
                role=f"{plan.role}-reference-{position}",
            )

    # Unselected generated outputs may satisfy selected dynamic references, but
    # selected outputs are admitted only after current metadata validation.
    for index, shot in enumerate(scene.shots, start=1):
        for role, generation in (
            ("start", shot.generate_start_image),
            ("end", shot.generate_end_image),
        ):
            if generation is None or (index, role) in image_plans:
                continue
            existing = _metadata_output_path(
                _generated_image_metadata_path(output_root, index, shot.name, role),
                output_root,
            )
            if existing is not None:
                paths[(index, role)] = existing

    for index, shot in shot_entries:
        if (
            images_only
            or shot.start_image is not None
            or shot.generate_start_image is not None
        ):
            continue
        previous_index = index - 1
        if previous_index in selected_numbers:
            continue
        continuation = _continuation_path(
            output_root, previous_index, scene.shots[previous_index - 1].name
        )
        if not continuation.is_file():
            raise ValueError(
                f"Shot {index} requires the previous continuation image, but it "
                f"does not exist: {continuation}"
            )

    if start_image_only and not image_plans:
        raise ValueError(
            "--start-image-only requires at least one generate_start_image shot"
        )
    if generated_images_only and not image_plans:
        raise ValueError(
            "--generated-images-only requires at least one configured image generation"
        )
    base_workflow: dict[str, Any] | None = None
    video_workflow_sha256 = ""
    if not images_only:
        if video_selection is None:
            raise RuntimeError("Video workflow selection is unavailable")
        base_workflow = load_workflow(video_selection.path)
        video_workflow_sha256 = fingerprint(base_workflow)
    _load_image_plan_workflows(image_plans)
    legacy_start_selection = image_selections.get("start_image")
    legacy_start_workflow_sha256 = next(
        (
            plan.workflow_sha256
            for plan in image_plans.values()
            if plan.selection.name == "start_image"
        ),
        None,
    )
    if (
        legacy_start_selection is not None
        and legacy_start_workflow_sha256 is None
        and not images_only
    ):
        legacy_start_workflow_sha256 = fingerprint(
            load_workflow(legacy_start_selection.path)
        )

    pending_images: set[tuple[int, str]] = set()
    pending_videos: set[int] = set()
    resumed_videos: dict[int, Path] = {}
    resume = bool(getattr(args, "resume", False))

    def shot_inputs(index: int, shot: Shot) -> dict[str, Any]:
        start_plan = image_plans.get((index, "start"))
        end_plan = image_plans.get((index, "end"))
        return build_shot_inputs(
            scene,
            shot,
            index=index,
            start_image=paths.get((index, "start")),
            profile=profile,
            video_workflow=video_selection,
            video_workflow_sha256=video_workflow_sha256,
            video_output_suffix=video_output_suffix,
            start_workflow=(
                start_plan.selection
                if start_plan is not None
                else legacy_start_selection
            ),
            start_workflow_sha256=(
                start_plan.workflow_sha256
                if start_plan is not None
                else legacy_start_workflow_sha256
            ),
            generation=start_plan.generation if start_plan is not None else None,
            starting_state=(scene.shots[index - 2].end_state if index > 1 else ""),
            end_image=paths.get((index, "end")),
            end_generation=end_plan.generation if end_plan is not None else None,
            end_workflow=end_plan.selection if end_plan is not None else None,
            end_workflow_sha256=(
                end_plan.workflow_sha256 if end_plan is not None else None
            ),
            start_generation_fingerprint=(
                fingerprint(start_plan.inputs)
                if start_plan is not None
                and not start_plan.legacy_start
                and start_plan.inputs is not None
                else None
            ),
            end_generation_fingerprint=(
                fingerprint(end_plan.inputs)
                if end_plan is not None and end_plan.inputs is not None
                else None
            ),
        )

    operation_indices = sorted(
        selected_numbers | {index for index, _ in image_plans}
    )
    for index in operation_indices:
        shot = scene.shots[index - 1]
        if shot.generate_start_image is None and shot.start_image is None:
            previous = paths.get((index - 1, "continuation"))
            if previous is not None:
                paths[(index, "start")] = previous
        for role in ("start", "end"):
            plan = image_plans.get((index, role))
            if plan is None:
                continue
            references = _resolve_generation_references(
                plan,
                scene=scene,
                paths=paths,
                explicit_references=explicit_references,
                pending_images=pending_images,
                pending_videos=pending_videos,
            )
            must_approve = approve_generated_images or (
                approve_start_images and plan.required_by_start_approval
            )
            if references is None:
                if must_approve:
                    raise ValueError(
                        f"Shot {index} {role} image cannot be approved because a "
                        "reference-producing operation is pending"
                    )
                pending_images.add((index, role))
                continue
            plan.reference_paths = references
            plan.inputs = _build_generation_inputs(plan, profile, references)
            existing_image, differences = _validate_generation_metadata(
                plan, plan.inputs, output_root
            )
            if must_approve:
                if existing_image is None or differences:
                    print(f"Generated image approval rejected for shot {index} {role}:")
                    for difference in differences:
                        print(f"  - {difference}")
                    raise ValueError(
                        f"Shot {index} {role} image does not match the current scene"
                    )
                plan.image = existing_image
                paths[(index, role)] = existing_image
            elif resume and existing_image is not None and not differences:
                plan.image = existing_image
                paths[(index, role)] = existing_image
                print(f"Resume: reusing generated {role} image {existing_image}")
            else:
                if resume and differences:
                    print(f"Resume: {role} image {index} must be generated again:")
                    for difference in differences:
                        print(f"  - {difference}")
                pending_images.add((index, role))

        if images_only or index not in selected_numbers:
            continue
        image_dependency_pending = any(
            (index, role) in pending_images for role in ("start", "end")
        )
        previous_dependency_pending = (
            shot.start_image is None
            and shot.generate_start_image is None
            and index - 1 in pending_videos
        )
        if not resume or image_dependency_pending or previous_dependency_pending:
            if resume and previous_dependency_pending:
                _print_resume_differences(
                    index,
                    [f"dependency: shot {index - 1} is being rendered again"],
                )
            paths.pop((index, "continuation"), None)
            pending_videos.add(index)
            continue
        expected_inputs = shot_inputs(index, shot)
        metadata_path = _shot_dir(output_root, index, shot.name) / "metadata.json"
        metadata = read_metadata(metadata_path)
        if shot.generate_start_image is None and metadata is not None:
            saved_inputs = metadata.get("inputs")
            saved_runtime = (
                saved_inputs.get("runtime") if isinstance(saved_inputs, dict) else None
            )
            expected_runtime = expected_inputs.get("runtime")
            if isinstance(saved_runtime, dict) and isinstance(expected_runtime, dict):
                expected_runtime["start_image_workflow"] = saved_runtime.get(
                    "start_image_workflow"
                )
        existing_video, differences = validate_shot_metadata(
            metadata, expected_inputs, output_root
        )
        if (
            existing_video is not None
            and differences
            and all(
                difference.startswith("outputs.continuation: missing file")
                for difference in differences
            )
        ):
            continuation = _continuation_path(output_root, index, shot.name)
            extract_last_frame(existing_video, continuation)
            print(f"Recovered continuation frame: {continuation}")
            existing_video, differences = validate_shot_metadata(
                metadata, expected_inputs, output_root
            )
        if existing_video is None or differences:
            _print_resume_differences(index, differences)
            paths.pop((index, "continuation"), None)
            pending_videos.add(index)
            continue
        resumed_videos[index] = existing_video
        continuation = _continuation_path(output_root, index, shot.name)
        paths[(index, "continuation")] = continuation
        print(f"Resume: shot {index}/{len(scene.shots)} metadata matches")

    if approve_generated_images:
        missing_approvals = [
            key for key in image_plans if image_plans[key].image is None
        ]
        if missing_approvals:
            index, role = missing_approvals[0]
            raise ValueError(f"Shot {index} {role} image is not approved")

    if not pending_images and not pending_videos:
        if images_only:
            print(f"Resume complete: {len(image_plans)} generated image(s) ready")
            write_render_manifest(
                output_root,
                scene_path,
                scene,
                selected_shots=sorted(selected_numbers),
            )
            return
        ordered_videos = [resumed_videos[index] for index, _ in shot_entries]
        if all_shots_selected:
            final_video = output_root / f"{slugify(scene.title)}{video_output_suffix}"
            concatenate_webm(ordered_videos, final_video)
            write_render_manifest(
                output_root,
                scene_path,
                scene,
                selected_shots=sorted(selected_numbers),
                final_video=final_video,
            )
            print(f"Scene assembled: {final_video}")
        else:
            write_render_manifest(
                output_root,
                scene_path,
                scene,
                selected_shots=sorted(selected_numbers),
            )
            print(f"Resume complete: {len(ordered_videos)} selected shot(s) ready")
        return

    required_groups: list[str] = []
    if pending_videos:
        if video_selection is None:
            raise RuntimeError("Video workflow selection is unavailable")
        required_groups.extend(video_selection.model_groups)
    for key, plan in image_plans.items():
        if key in pending_images:
            required_groups.extend(plan.selection.model_groups)
    required_models = profile.models_for_groups(required_groups)
    rendered_videos = dict(resumed_videos)
    generated_image_count = 0

    with _worker_session(args, profile, models=required_models) as comfy:
        for index in operation_indices:
            shot = scene.shots[index - 1]
            shot_started = time.monotonic()
            if shot.generate_start_image is None and shot.start_image is None:
                previous = paths.get((index - 1, "continuation"))
                if previous is not None:
                    paths[(index, "start")] = previous
            for role in ("start", "end"):
                key = (index, role)
                plan = image_plans.get(key)
                if plan is None or key not in pending_images:
                    continue
                references = _resolve_generation_references(
                    plan,
                    scene=scene,
                    paths=paths,
                    explicit_references=explicit_references,
                    pending_images=set(),
                    pending_videos=set(),
                )
                if references is None:
                    raise RuntimeError(
                        f"Shot {index} {role} image references are still pending"
                    )
                plan.reference_paths = references
                plan.inputs = _build_generation_inputs(plan, profile, references)
                reference_names: list[str] = []
                for position, reference_path in enumerate(references, start=1):
                    remote_name = (
                        f"scene-{index:03d}-{role}-reference-{position:03d}"
                        f"{reference_path.suffix.lower() or '.png'}"
                    )
                    uploaded = _retry_operation(
                        f"{role.capitalize()} image reference {position} upload "
                        f"for shot {index}",
                        getattr(args, "retries", 2),
                        lambda path=reference_path, name=remote_name: (
                            comfy.upload_image(path, name)
                        ),
                    )
                    reference_names.append(uploaded)
                if plan.workflow is None:
                    raise RuntimeError("Image workflow was not loaded")
                print(f"Generating {role} keyframe for shot {index}: {shot.name}")
                generation_started = time.monotonic()
                generation_workflow = build_image_workflow(
                    plan.selection.adapter,
                    plan.workflow,
                    plan.generation,
                    shot_number=index,
                    shot_name=shot.name,
                    role=role,
                    reference_names=reference_names,
                )
                _, generation_history = _queue_with_retries(
                    comfy,
                    generation_workflow,
                    args,
                    lambda message, shot_index=index, image_role=role: print(
                        f"[{image_role} image {shot_index}/{len(scene.shots)}] "
                        f"{message}",
                        flush=True,
                    ),
                )
                generated_dir = _generated_image_dir(output_root, role)
                generated_outputs = _retry_operation(
                    f"{role.capitalize()} image download",
                    getattr(args, "retries", 2),
                    lambda: comfy.download_outputs(
                        generation_history,
                        generated_dir,
                    ),
                )
                generated_images = [
                    path
                    for path in generated_outputs
                    if path.suffix.lower() in IMAGE_SUFFIXES
                ]
                if len(generated_images) != 1:
                    raise RuntimeError(
                        f"{role.capitalize()} image generation for {shot.name!r} "
                        "produced "
                        f"{len(generated_images)} images; expected 1"
                    )
                plan.image = generated_images[0]
                paths[key] = plan.image
                generated_image_count += 1
                print(f"Generated {role} keyframe: {plan.image}")
                metadata_path = _generated_image_metadata_path(
                    output_root, index, shot.name, role
                )
                writer = (
                    write_start_image_metadata
                    if plan.legacy_start
                    else write_generated_image_metadata
                )
                writer(
                    metadata_path,
                    inputs=plan.inputs,
                    image=plan.image,
                    output_root=output_root,
                    runtime=getattr(comfy, "runtime_metadata", {}),
                    elapsed_seconds=time.monotonic() - generation_started,
                )
                write_render_manifest(
                    output_root,
                    scene_path,
                    scene,
                    selected_shots=sorted(selected_numbers),
                )
            if images_only or index not in pending_videos:
                continue
            print(f"Rendering shot {index}/{len(scene.shots)}: {shot.name}")
            start_image = paths.get((index, "start"))
            if start_image is None:
                previous_index = index - 1
                start_image = _continuation_path(
                    output_root, previous_index, scene.shots[previous_index - 1].name
                )
                paths[(index, "start")] = start_image
            if start_image is None:
                raise RuntimeError(f"Shot {shot.name!r} has no available start image")
            current_shot_inputs = shot_inputs(index, shot)
            start_remote = (
                f"scene-{index:03d}-start{start_image.suffix.lower() or '.png'}"
            )
            start_remote = _retry_operation(
                f"Start keyframe upload for shot {index}",
                getattr(args, "retries", 2),
                lambda: comfy.upload_image(start_image, start_remote),
            )
            print(f"Uploaded start keyframe: {start_remote}")

            end_remote: str | None = None
            end_image = paths.get((index, "end"))
            if end_image is not None:
                end_remote = (
                    f"scene-{index:03d}-end{end_image.suffix.lower() or '.png'}"
                )
                end_remote = _retry_operation(
                    f"End keyframe upload for shot {index}",
                    getattr(args, "retries", 2),
                    lambda: comfy.upload_image(end_image, end_remote),
                )
                print(f"Uploaded end keyframe: {end_remote}")

            if base_workflow is None:
                raise RuntimeError("Video workflow was not loaded")
            workflow = build_shot_workflow(
                video_selection.adapter,
                base_workflow,
                scene,
                shot,
                shot_number=index,
                start_image_name=start_remote,
                end_image_name=end_remote,
                starting_state=(
                    scene.shots[index - 2].end_state if index > 1 else ""
                ),
            )
            _, history = _queue_with_retries(
                comfy,
                workflow,
                args,
                lambda message, shot_index=index: print(
                    f"[shot {shot_index}/{len(scene.shots)}] {message}",
                    flush=True,
                ),
            )
            shot_dir = _shot_dir(output_root, index, shot.name)
            outputs = _retry_operation(
                f"Output download for shot {index}",
                getattr(args, "retries", 2),
                lambda: comfy.download_outputs(history, shot_dir),
            )
            video_outputs = [
                path for path in outputs if path.suffix.lower() == video_output_suffix
            ]
            if len(video_outputs) != 1:
                raise RuntimeError(
                    f"Shot {shot.name!r} produced {len(video_outputs)} "
                    f"{video_output_suffix} outputs; expected 1"
                )
            rendered_videos[index] = video_outputs[0]
            for output in outputs:
                print(f"Downloaded: {output}")

            continuation = shot_dir / "continuation.png"
            extract_last_frame(video_outputs[0], continuation)
            paths[(index, "continuation")] = continuation
            print(f"Extracted continuation frame: {continuation}")
            _prune_old_shot_videos(
                shot_dir, video_outputs[0], video_output_suffix
            )
            write_shot_metadata(
                shot_dir / "metadata.json",
                inputs=current_shot_inputs,
                video=video_outputs[0],
                continuation=continuation,
                output_root=output_root,
                runtime=getattr(comfy, "runtime_metadata", {}),
                elapsed_seconds=time.monotonic() - shot_started,
            )
            write_render_manifest(
                output_root,
                scene_path,
                scene,
                selected_shots=sorted(selected_numbers),
            )

    if images_only:
        write_render_manifest(
            output_root,
            scene_path,
            scene,
            selected_shots=sorted(selected_numbers),
        )
        label = "start keyframe" if start_image_only else "generated image"
        print(f"Generated {generated_image_count} {label}(s) in {output_root}")
        return
    if not all_shots_selected:
        ordered = [rendered_videos[index] for index, _ in shot_entries]
        print(f"Shots rendered: {len(ordered)} selected shot(s)")
        for video in ordered:
            print(f"Shot rendered: {video}")
        write_render_manifest(
            output_root,
            scene_path,
            scene,
            selected_shots=sorted(selected_numbers),
        )
        return
    final_video = output_root / f"{slugify(scene.title)}{video_output_suffix}"
    concatenate_webm(
        [rendered_videos[index] for index, _ in shot_entries], final_video
    )
    write_render_manifest(
        output_root,
        scene_path,
        scene,
        selected_shots=sorted(selected_numbers),
        final_video=final_video,
    )
    print(f"Scene assembled: {final_video}")


def cleanup(args: argparse.Namespace) -> None:
    cutoff = datetime.now(UTC) - timedelta(hours=args.max_age_hours)
    with RunPodClient(_api_key()) as client:
        for pod in client.list_pods():
            name = str(pod.get("name", ""))
            if not name.startswith("runpod-video-"):
                continue
            created_raw = pod.get("createdAt") or pod.get("lastStartedAt")
            if not isinstance(created_raw, str):
                if args.all:
                    client.terminate_pod(str(pod["id"]))
                continue
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if args.all or created < cutoff:
                print(f"Terminating stale pod {pod['id']} ({name})")
                client.terminate_pod(str(pod["id"]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory", help="List managed resources")
    inventory_parser.set_defaults(func=inventory)

    plan_parser = subparsers.add_parser("plan", help="Show a profile without spending money")
    plan_parser.add_argument("--profile")
    plan_parser.set_defaults(func=plan)

    setup_parser = subparsers.add_parser(
        "setup", help="Provision a worker and install selected model groups only"
    )
    setup_parser.add_argument("--profile")
    setup_parser.add_argument(
        "--model-group",
        action="append",
        help="Model group to install; repeat as needed (defaults to profile presets)",
    )
    setup_parser.add_argument("--ssh-key")
    setup_parser.add_argument("--pod-id", help="Reuse an existing Pod")
    setup_parser.add_argument("--start-timeout", type=int, default=900)
    setup_parser.add_argument(
        "--apply", action="store_true", help="Allow creation of billable resources"
    )
    setup_lifecycle = setup_parser.add_mutually_exclusive_group()
    setup_lifecycle.add_argument("--keep-pod", action="store_true")
    setup_lifecycle.add_argument(
        "--stop-pod",
        action="store_true",
        help="Stop instead of terminating the Pod after setup",
    )
    setup_parser.add_argument(
        "--idle-stop-minutes",
        type=float,
        help="With --keep-pod, stop the Pod after this many idle minutes",
    )
    setup_parser.set_defaults(func=setup)

    run_parser = subparsers.add_parser("run", help="Provision, render, download, and terminate")
    run_parser.add_argument("workflow", help="ComfyUI workflow exported in API format")
    run_parser.add_argument("--profile")
    run_parser.add_argument(
        "--model-group",
        action="append",
        help="Model group required by this workflow; repeat as needed",
    )
    run_parser.add_argument("--ssh-key")
    run_parser.add_argument("--pod-id", help="Reuse an existing running Pod")
    run_parser.add_argument(
        "--restart",
        action="store_true",
        help="Interrupt ComfyUI and clear its queue before rendering; requires --pod-id",
    )
    run_parser.add_argument("--image", action="append", help="LOCAL_PATH[:REMOTE_NAME]")
    run_parser.add_argument("--set", action="append", help="NODE.INPUT=JSON")
    run_parser.add_argument("--output", default="output")
    run_parser.add_argument("--start-timeout", type=int, default=900)
    run_parser.add_argument("--workflow-timeout", type=int, default=7200)
    run_parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retry failed uploads, workflows, and downloads",
    )
    run_parser.add_argument(
        "--apply", action="store_true", help="Allow creation of billable resources"
    )
    run_lifecycle = run_parser.add_mutually_exclusive_group()
    run_lifecycle.add_argument("--keep-pod", action="store_true")
    run_lifecycle.add_argument(
        "--stop-pod",
        action="store_true",
        help="Stop instead of terminating the Pod after the command",
    )
    run_parser.add_argument(
        "--idle-stop-minutes",
        type=float,
        help="With --keep-pod, stop the Pod after this many idle minutes",
    )
    run_parser.set_defaults(func=run)

    scene_parser = subparsers.add_parser(
        "scene", help="Render and assemble a multi-shot scene manifest"
    )
    scene_parser.add_argument(
        "manifest", help="Scene JSON file or project directory containing scene.json"
    )
    scene_parser.add_argument("--workflow", help="Override the profile video workflow")
    scene_parser.add_argument("--video-adapter", help="Override the video adapter")
    scene_parser.add_argument(
        "--video-model-group",
        action="append",
        help="Override video model groups; repeat as needed",
    )
    scene_parser.add_argument(
        "--start-image-workflow",
        help="Override the profile start-image workflow",
    )
    scene_parser.add_argument(
        "--start-image-adapter", help="Override the start-image adapter"
    )
    scene_parser.add_argument(
        "--start-image-model-group",
        action="append",
        help="Override start-image model groups; repeat as needed",
    )
    scene_parser.add_argument(
        "--start-image-only",
        action="store_true",
        help="Generate configured start images without rendering video shots",
    )
    scene_parser.add_argument(
        "--generated-images-only",
        action="store_true",
        help="Generate configured start and end images without rendering video shots",
    )
    scene_parser.add_argument(
        "--approve-start-images",
        action="store_true",
        help="Use previously generated start images without regenerating them",
    )
    scene_parser.add_argument(
        "--approve-generated-images",
        action="store_true",
        help="Require and use matching generated start and end images",
    )
    scene_parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse valid completed shots and generated images",
    )
    scene_parser.add_argument(
        "--backfill-metadata",
        action="store_true",
        help="Infer missing metadata from existing local outputs without using a Pod",
    )
    shot_selection = scene_parser.add_mutually_exclusive_group()
    shot_selection.add_argument(
        "--shot",
        type=int,
        help="Render only the selected 1-based shot number",
    )
    shot_selection.add_argument(
        "--shots",
        type=_parse_shots,
        help="Render 1-based shot numbers and ranges, for example 1,3-5",
    )
    scene_parser.add_argument("--profile")
    scene_parser.add_argument("--ssh-key")
    scene_parser.add_argument("--pod-id", help="Reuse an existing running Pod")
    scene_parser.add_argument(
        "--restart",
        action="store_true",
        help="Interrupt ComfyUI and clear its queue before rendering; requires --pod-id",
    )
    scene_parser.add_argument(
        "--output",
        help="Output directory; defaults to output/ beside scene.json",
    )
    scene_parser.add_argument("--start-timeout", type=int, default=900)
    scene_parser.add_argument("--workflow-timeout", type=int, default=7200)
    scene_parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retry failed uploads, workflows, and downloads",
    )
    scene_parser.add_argument(
        "--plan", action="store_true", help="Validate and print the scene without spending money"
    )
    scene_parser.add_argument(
        "--apply", action="store_true", help="Allow creation of billable resources"
    )
    scene_lifecycle = scene_parser.add_mutually_exclusive_group()
    scene_lifecycle.add_argument("--keep-pod", action="store_true")
    scene_lifecycle.add_argument(
        "--stop-pod",
        action="store_true",
        help="Stop instead of terminating the Pod after the command",
    )
    scene_parser.add_argument(
        "--idle-stop-minutes",
        type=float,
        help="With --keep-pod, stop the Pod after this many idle minutes",
    )
    scene_parser.set_defaults(func=render_scene)

    cleanup_parser = subparsers.add_parser("cleanup", help="Terminate stale managed pods")
    cleanup_parser.add_argument("--max-age-hours", type=float, default=2.0)
    cleanup_parser.add_argument("--all", action="store_true")
    cleanup_parser.set_defaults(func=cleanup)
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        args = build_parser().parse_args()
        args.func(args)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
