from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from runpod_video_automation.adapters import (
    ResolvedStartImageGeneration,
    build_shot_workflow,
    build_start_image_workflow,
    get_video_adapter,
    resolve_start_image_generation,
)
from runpod_video_automation.comfy_client import ComfyClient
from runpod_video_automation.config import ModelFile, Profile, WorkflowSelection
from runpod_video_automation.prompt_refiner import (
    PromptRefinerProfile,
    RefinementResult,
    load_cached_refinement,
    refine_scene,
)
from runpod_video_automation.prompt_refiner.client import KoboldClient
from runpod_video_automation.remote import RemoteWorker
from runpod_video_automation.render_metadata import (
    build_shot_inputs,
    build_start_image_inputs,
    fingerprint,
    read_metadata,
    sha256_file,
    validate_shot_metadata,
    validate_start_image_metadata,
    write_render_manifest,
    write_shot_metadata,
    write_start_image_metadata,
)
from runpod_video_automation.runpod_client import RunPodClient
from runpod_video_automation.scene import (
    Scene,
    Shot,
    concatenate_webm,
    extract_last_frame,
    slugify,
)
from runpod_video_automation.workflow import apply_overrides, load_workflow


PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _profile_path(value: str | None) -> Path:
    path = Path(value or os.environ.get("RUNPOD_VIDEO_PROFILE", "profiles/wan22-i2v-fp8.json"))
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _refiner_profile_path(value: str | None) -> Path:
    path = Path(value or "profiles/prompt-refiner-qwen36.json").expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


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
    prepare_comfy: bool = True,
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
            if prepare_comfy and profile.comfy_args:
                remote.ensure_comfy_args(
                    profile.comfy_args,
                    system_packages=profile.system_packages,
                )
            elif prepare_comfy and profile.system_packages:
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
def _comfy_session(
    args: argparse.Namespace,
    profile: Profile,
    *,
    remote_details: tuple[RemoteWorker, str, float | None, dict[str, Any]],
    models: tuple[ModelFile, ...] | None = None,
) -> Iterator[ComfyClient]:
    remote, pod_id, hourly_cost, pod = remote_details
    if models is not None:
        remote.ensure_models(models, profile.model_path_aliases)
        remote.ensure_comfy_args(
            profile.comfy_args,
            system_packages=profile.system_packages,
        )
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
        with _comfy_session(
            args,
            profile,
            remote_details=remote_details,
        ) as comfy:
            yield comfy


def _validate_refiner_args(args: argparse.Namespace) -> None:
    _validate_execution_args(args)
    if getattr(args, "idle_stop_minutes", None) is not None:
        raise ValueError("Prompt refiner commands do not support --idle-stop-minutes")
    if getattr(args, "pod_id", None) and not getattr(args, "restart", False):
        raise ValueError("Refiner use with --pod-id requires --restart")


def _refine_with_remote(
    *,
    remote: RemoteWorker,
    source_path: Path,
    output_root: Path,
    profile: PromptRefinerProfile,
    force: bool,
    start_timeout: int,
) -> RefinementResult:
    remote.stop_comfyui()
    with remote.koboldcpp_process(profile):
        with remote.tunnel(profile.port) as base_url:
            client = KoboldClient(base_url)
            try:
                info = client.wait_until_ready(timeout_seconds=start_timeout)
                print(f"Prompt refiner ready: {info}")
                return refine_scene(
                    client=client,
                    source_path=source_path,
                    output_root=output_root,
                    profile=profile,
                    force=force,
                )
            finally:
                client.close()


def refine(args: argparse.Namespace) -> None:
    source_path = _scene_path(args.manifest)
    Scene.load(source_path)
    output_root = _scene_output_root(source_path, args.output)
    refiner_profile = PromptRefinerProfile.load(
        _refiner_profile_path(args.refiner_profile)
    )
    if not args.force:
        cached = load_cached_refinement(
            source_path=source_path,
            output_root=output_root,
            profile=refiner_profile,
        )
        if cached is not None:
            print(f"Refined scene cache: {cached.manifest_path}")
            return
    if not args.apply:
        raise RuntimeError("Refinement cache miss; use --apply to create resources")
    _validate_refiner_args(args)
    infrastructure = Profile.load(_profile_path(args.profile))
    with _remote_session(
        args,
        infrastructure,
        models=refiner_profile.artifacts,
        prepare_comfy=False,
    ) as remote_details:
        result = _refine_with_remote(
            remote=remote_details[0],
            source_path=source_path,
            output_root=output_root,
            profile=refiner_profile,
            force=args.force,
            start_timeout=args.start_timeout,
        )
    print(f"Refined scene: {result.manifest_path}")


def chat(args: argparse.Namespace) -> None:
    if not args.apply:
        raise RuntimeError("Refusing to create billable resources without --apply")
    _validate_refiner_args(args)
    if args.duration_seconds is not None and args.duration_seconds <= 0:
        raise ValueError("--duration-seconds must be positive")
    infrastructure = Profile.load(_profile_path(args.profile))
    refiner_profile = PromptRefinerProfile.load(
        _refiner_profile_path(args.refiner_profile)
    )
    with _remote_session(
        args,
        infrastructure,
        models=refiner_profile.artifacts,
        prepare_comfy=False,
    ) as remote_details:
        remote = remote_details[0]
        remote.stop_comfyui()
        with remote.koboldcpp_process(refiner_profile):
            with remote.tunnel(refiner_profile.port) as base_url:
                client = KoboldClient(base_url)
                try:
                    client.wait_until_ready(timeout_seconds=args.start_timeout)
                finally:
                    client.close()
                print(f"Prompt refiner chat: {base_url}/", flush=True)
                if not args.no_browser:
                    webbrowser.open(f"{base_url}/")
                if args.duration_seconds is not None:
                    time.sleep(args.duration_seconds)
                else:
                    input("Press Enter to close the chat server... ")


def setup(args: argparse.Namespace) -> None:
    if not args.apply:
        raise RuntimeError("Refusing to create billable resources without --apply")
    _validate_execution_args(args)
    profile = Profile.load(_profile_path(args.profile))
    groups = tuple(args.model_group or profile.default_model_groups)
    models = profile.models_for_groups(groups)
    if args.include_refiner:
        refiner_profile = PromptRefinerProfile.load(
            _refiner_profile_path(args.refiner_profile)
        )
        models = tuple(dict.fromkeys((*models, *refiner_profile.artifacts)))
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
    generations: dict[int, ResolvedStartImageGeneration],
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
            generation = generations.get(index)
            if generation is None:
                source = "generated start image (unselected shot)"
            else:
                source = (
                    f"generated with {generation.adapter} ({generation.checkpoint}) "
                    f"at {generation.width}x{generation.height}"
                )
        else:
            source = "previous shot's last frame"
        end = f", end keyframe {shot.end_image}" if shot.end_image else ""
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
    start_selection: WorkflowSelection | None,
    generations: dict[int, ResolvedStartImageGeneration],
) -> None:
    if not output_root.is_dir():
        raise ValueError(f"Scene output directory not found: {output_root}")
    video_workflow_sha256 = fingerprint(load_workflow(video_selection.path))
    start_workflow_sha256: str | None = None
    if start_selection is not None:
        start_workflow_sha256 = fingerprint(load_workflow(start_selection.path))

    input_snapshots: dict[tuple[int, str], Path] = {}
    generated_images: dict[int, Path] = {}
    start_image_plans: list[tuple[Path, dict[str, Any], Path]] = []
    shot_plans: list[tuple[Path, dict[str, Any], Path, Path]] = []
    runtime = {
        "backfilled": True,
        "provenance": "inferred_from_existing_outputs",
        "historical_render_time_unknown": True,
    }

    # Validate the complete adoption set before writing any metadata sidecars.
    for index, shot in shot_entries:
        if shot.start_image is not None:
            input_snapshots[(index, "start")] = _snapshot_input(
                shot.start_image,
                output_root,
                index=index,
                role="start",
            )
        if shot.end_image is not None:
            input_snapshots[(index, "end")] = _snapshot_input(
                shot.end_image,
                output_root,
                index=index,
                role="end",
            )
        if shot.generate_start_image is None:
            continue
        if start_selection is None or start_workflow_sha256 is None:
            raise RuntimeError("Start image workflow selection is unavailable")
        inputs = build_start_image_inputs(
            shot,
            index=index,
            profile=profile,
            generation=generations[index],
            start_workflow=start_selection,
            start_workflow_sha256=start_workflow_sha256,
        )
        metadata_path = _start_image_metadata_path(output_root, index, shot.name)
        if metadata_path.is_file():
            existing_image, differences = validate_start_image_metadata(
                read_metadata(metadata_path), inputs, output_root
            )
            if existing_image is None or differences:
                raise ValueError(
                    f"Cannot backfill shot {index}: existing start image metadata "
                    "does not match the current scene:\n  - "
                    + "\n  - ".join(differences)
                )
            generated_images[index] = existing_image
            print(f"Metadata already exists: {metadata_path}")
            continue
        image = _single_existing_output(
            output_root / "000-generated-start-image",
            prefix=f"{index:03d}-{slugify(shot.name)}_",
            suffixes=IMAGE_SUFFIXES,
            label=f"generated start image for shot {index}",
        )
        if image is None:
            continue
        generated_images[index] = image
        start_image_plans.append((metadata_path, inputs, image))

    for index, shot in shot_entries:
        shot_dir = _shot_dir(output_root, index, shot.name)
        metadata_path = shot_dir / "metadata.json"
        if metadata_path.is_file():
            print(f"Metadata already exists: {metadata_path}")
            continue
        video = _single_existing_output(
            shot_dir,
            prefix="",
            suffixes={video_output_suffix},
            label=f"video for shot {index}",
        )
        continuation = _continuation_path(output_root, index, shot.name)
        if video is None and not continuation.is_file():
            print(f"Backfill: no existing output for shot {index}; skipping")
            continue
        if video is None:
            raise ValueError(f"Cannot backfill shot {index}: video is missing")
        if not continuation.is_file():
            raise ValueError(
                f"Cannot backfill shot {index}: continuation image is missing"
            )
        if shot.start_image is not None:
            start_image = input_snapshots[(index, "start")]
        elif shot.generate_start_image is not None:
            start_image = generated_images.get(index)
            if start_image is None:
                raise ValueError(
                    f"Cannot backfill shot {index}: generated start image is missing"
                )
        else:
            previous_index = index - 1
            previous_shot = scene.shots[previous_index - 1]
            start_image = _continuation_path(
                output_root, previous_index, previous_shot.name
            )
            if not start_image.is_file():
                raise ValueError(
                    f"Cannot backfill shot {index}: previous continuation is missing"
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
            start_workflow=start_selection,
            start_workflow_sha256=start_workflow_sha256,
            generation=generations.get(index),
            starting_state=(scene.shots[index - 2].end_state if index > 1 else ""),
            end_image=input_snapshots.get((index, "end")),
        )
        shot_plans.append((metadata_path, inputs, video, continuation))

    _snapshot_scene(scene_path, output_root)
    for metadata_path, inputs, image in start_image_plans:
        write_start_image_metadata(
            metadata_path,
            inputs=inputs,
            image=image,
            output_root=output_root,
            runtime=runtime,
            elapsed_seconds=0,
        )
        print(f"Backfilled start image metadata: {metadata_path}")
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
        f"{len(start_image_plans)} start image(s)"
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
    source_scene = Scene.load(scene_path)
    if not getattr(args, "refine_prompts", False):
        if getattr(args, "force", False) or getattr(
            args, "refiner_profile", None
        ) is not None:
            raise ValueError("--force and --refiner-profile require --refine-prompts")
        _render_scene_effective(args, scene_path, source_scene)
        return
    if getattr(args, "backfill_metadata", False):
        raise ValueError("--refine-prompts cannot be combined with --backfill-metadata")

    output_root = _scene_output_root(scene_path, args.output)
    refiner_profile = PromptRefinerProfile.load(
        _refiner_profile_path(args.refiner_profile)
    )
    refinement = None
    if not args.force:
        refinement = load_cached_refinement(
            source_path=scene_path,
            output_root=output_root,
            profile=refiner_profile,
        )
    if refinement is not None:
        print(f"Refined scene cache: {refinement.manifest_path}")
        _render_scene_effective(
            args,
            scene_path,
            refinement.scene,
            refinement=refinement,
        )
        return
    if args.plan:
        raise RuntimeError(
            "Refinement cache miss; use scene --apply --refine-prompts to create it"
        )
    if not args.apply:
        raise RuntimeError("Refinement cache miss; use --apply to create resources")
    _validate_execution_args(args)
    if args.pod_id and not args.restart:
        raise ValueError("Prompt refinement with --pod-id requires --restart")

    infrastructure = Profile.load(_profile_path(args.profile))
    with _remote_session(
        args,
        infrastructure,
        models=refiner_profile.artifacts,
        prepare_comfy=False,
    ) as remote_details:
        refinement = _refine_with_remote(
            remote=remote_details[0],
            source_path=scene_path,
            output_root=output_root,
            profile=refiner_profile,
            force=args.force,
            start_timeout=args.start_timeout,
        )
        print(f"Refined scene: {refinement.manifest_path}")
        _render_scene_effective(
            args,
            scene_path,
            refinement.scene,
            refinement=refinement,
            remote_details=remote_details,
        )


def _render_scene_effective(
    args: argparse.Namespace,
    scene_path: Path,
    scene: Scene,
    *,
    refinement: RefinementResult | None = None,
    remote_details: (
        tuple[RemoteWorker, str, float | None, dict[str, Any]] | None
    ) = None,
) -> None:
    prompt_refinement = refinement.provenance if refinement is not None else None
    metadata_scene_path = (
        refinement.manifest_path if refinement is not None else scene_path
    )
    shot_entries = _selected_shot_entries(scene, args)
    selected_numbers = {index for index, _ in shot_entries}
    all_shots_selected = len(selected_numbers) == len(scene.shots)
    selected_start_images = any(
        shot.generate_start_image is not None for _, shot in shot_entries
    )
    scene_uses_start_images = any(
        shot.generate_start_image is not None for shot in scene.shots
    )
    profile = Profile.load(_profile_path(args.profile))
    video_selection: WorkflowSelection | None = None
    video_output_suffix = ".webm"
    if not args.start_image_only:
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
    start_selection: WorkflowSelection | None = None
    generations: dict[int, ResolvedStartImageGeneration] = {}
    if scene_uses_start_images:
        start_selection = _workflow_selection(
            profile,
            "start_image",
            path=getattr(args, "start_image_workflow", None),
            adapter=getattr(args, "start_image_adapter", None),
            model_groups=getattr(args, "start_image_model_group", None),
        )
        for index, shot in shot_entries:
            if shot.generate_start_image is not None:
                generations[index] = resolve_start_image_generation(
                    shot.generate_start_image,
                    start_selection.adapter,
                    start_selection.defaults,
                )
    _scene_plan(scene, generations)
    if not all_shots_selected:
        print(f"Selected shots: {', '.join(map(str, sorted(selected_numbers)))}")
    if getattr(args, "backfill_metadata", False):
        conflicting = [
            flag
            for enabled, flag in (
                (args.plan, "--plan"),
                (args.apply, "--apply"),
                (args.start_image_only, "--start-image-only"),
                (getattr(args, "approve_start_images", False), "--approve-start-images"),
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
            start_selection=start_selection,
            generations=generations,
        )
        return
    if args.plan:
        if args.apply:
            raise ValueError("Use either --plan or --apply, not both")
        return
    if not args.apply:
        raise RuntimeError("Refusing to create billable resources without --apply")
    _validate_execution_args(args)
    if args.start_image_only and getattr(args, "approve_start_images", False):
        raise ValueError("Use either --start-image-only or --approve-start-images")
    output_root = _scene_output_root(scene_path, args.output)
    if not args.start_image_only and shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to assemble scene outputs")
    scene_snapshot = _snapshot_scene(metadata_scene_path, output_root)
    print(f"Scene snapshot: {scene_snapshot}")
    input_snapshots: dict[tuple[int, str], Path] = {}
    for index, shot in shot_entries:
        if shot.start_image is not None:
            input_snapshots[(index, "start")] = _snapshot_input(
                shot.start_image,
                output_root,
                index=index,
                role="start",
            )
        if shot.end_image is not None:
            input_snapshots[(index, "end")] = _snapshot_input(
                shot.end_image,
                output_root,
                index=index,
                role="end",
            )
    for index, shot in shot_entries:
        if args.start_image_only:
            continue
        if shot.start_image is not None or shot.generate_start_image is not None:
            continue
        previous_index = index - 1
        if previous_index in selected_numbers:
            continue
        previous_shot = scene.shots[previous_index - 1]
        continuation = _continuation_path(
            output_root, previous_index, previous_shot.name
        )
        if not continuation.is_file():
            raise ValueError(
                f"Shot {index} requires the previous continuation image, but it "
                f"does not exist: {continuation}"
            )

    if args.start_image_only and not selected_start_images:
        raise ValueError(
            "--start-image-only requires at least one generate_start_image shot"
        )
    base_workflow: dict[str, Any] | None = None
    video_workflow_sha256 = ""
    if not args.start_image_only:
        if video_selection is None:
            raise RuntimeError("Video workflow selection is unavailable")
        base_workflow = load_workflow(video_selection.path)
        video_workflow_sha256 = fingerprint(base_workflow)
    start_image_workflow: dict[str, Any] | None = None
    start_workflow_sha256: str | None = None
    if scene_uses_start_images:
        if start_selection is None:
            raise RuntimeError("Start image workflow selection is unavailable")
        start_image_workflow = load_workflow(start_selection.path)
        start_workflow_sha256 = fingerprint(start_image_workflow)

    approved_images: dict[int, Path] = {}
    for index, shot in shot_entries:
        if shot.generate_start_image is None:
            continue
        if start_workflow_sha256 is None:
            raise RuntimeError("Start image workflow fingerprint is unavailable")
        expected_start_inputs = build_start_image_inputs(
            shot,
            index=index,
            profile=profile,
            generation=generations[index],
            start_workflow=start_selection,
            start_workflow_sha256=start_workflow_sha256,
            prompt_refinement=prompt_refinement,
        )
        start_metadata = read_metadata(
            _start_image_metadata_path(output_root, index, shot.name)
        )
        existing_image, start_differences = validate_start_image_metadata(
            start_metadata,
            expected_start_inputs,
            output_root,
        )
        if getattr(args, "approve_start_images", False):
            if existing_image is None or start_differences:
                print(f"Start image approval rejected for shot {index}:")
                for difference in start_differences:
                    print(f"  - {difference}")
                raise ValueError(
                    f"Shot {index} start image does not match the current scene"
                )
            approved_images[index] = existing_image
        elif (
            getattr(args, "resume", False)
            and existing_image is not None
            and not start_differences
        ):
            approved_images[index] = existing_image
            print(f"Resume: reusing generated start image {existing_image}")
        elif getattr(args, "resume", False) and start_differences:
            print(f"Resume: start image {index} must be generated again:")
            for difference in start_differences:
                print(f"  - {difference}")

    resumed_videos: dict[int, Path] = {}
    if getattr(args, "resume", False) and not args.start_image_only:
        for index, shot in shot_entries:
            if shot.start_image is not None:
                expected_start_image = input_snapshots[(index, "start")]
            elif shot.generate_start_image is not None:
                expected_start_image = approved_images.get(index)
            else:
                previous_index = index - 1
                previous_shot = scene.shots[previous_index - 1]
                expected_start_image = _continuation_path(
                    output_root, previous_index, previous_shot.name
                )
            expected_inputs = build_shot_inputs(
                scene,
                shot,
                index=index,
                start_image=expected_start_image,
                profile=profile,
                video_workflow=video_selection,
                video_workflow_sha256=video_workflow_sha256,
                video_output_suffix=video_output_suffix,
                start_workflow=start_selection,
                start_workflow_sha256=start_workflow_sha256,
                generation=generations.get(index),
                starting_state=(
                    scene.shots[index - 2].end_state if index > 1 else ""
                ),
                end_image=input_snapshots.get((index, "end")),
                prompt_refinement=prompt_refinement,
            )
            metadata_path = _shot_dir(output_root, index, shot.name) / "metadata.json"
            metadata = read_metadata(metadata_path)
            if shot.generate_start_image is None and metadata is not None:
                saved_inputs = metadata.get("inputs")
                saved_runtime = (
                    saved_inputs.get("runtime")
                    if isinstance(saved_inputs, dict)
                    else None
                )
                expected_runtime = expected_inputs.get("runtime")
                if isinstance(saved_runtime, dict) and isinstance(
                    expected_runtime, dict
                ):
                    # Schema v2 initially made this irrelevant field depend on
                    # whether a generated-image shot was selected in the same run.
                    expected_runtime["start_image_workflow"] = saved_runtime.get(
                        "start_image_workflow"
                    )
            existing_video, differences = validate_shot_metadata(
                metadata,
                expected_inputs,
                output_root,
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
                    metadata,
                    expected_inputs,
                    output_root,
                )
            depends_on_previous = (
                shot.start_image is None and shot.generate_start_image is None
            )
            if (
                depends_on_previous
                and index - 1 in selected_numbers
                and index - 1 not in resumed_videos
            ):
                differences.append(
                    f"dependency: shot {index - 1} is being rendered again"
                )
            if existing_video is None or differences:
                _print_resume_differences(index, differences)
                continue
            resumed_videos[index] = existing_video
            print(f"Resume: shot {index}/{len(scene.shots)} metadata matches")

    if args.start_image_only:
        pending_entries = [
            (index, shot)
            for index, shot in shot_entries
            if shot.generate_start_image is not None and index not in approved_images
        ]
    else:
        pending_entries = [
            (index, shot)
            for index, shot in shot_entries
            if index not in resumed_videos
        ]
    if not pending_entries:
        if args.start_image_only:
            print(
                f"Resume complete: {len(approved_images)} generated start image(s) ready"
            )
            write_render_manifest(
                output_root,
                metadata_scene_path,
                scene,
                selected_shots=sorted(selected_numbers),
                prompt_refinement=prompt_refinement,
            )
            return
        ordered_videos = [resumed_videos[index] for index, _ in shot_entries]
        if all_shots_selected:
            final_video = output_root / f"{slugify(scene.title)}{video_output_suffix}"
            concatenate_webm(ordered_videos, final_video)
            write_render_manifest(
                output_root,
                metadata_scene_path,
                scene,
                selected_shots=sorted(selected_numbers),
                final_video=final_video,
                prompt_refinement=prompt_refinement,
            )
            print(f"Scene assembled: {final_video}")
        else:
            write_render_manifest(
                output_root,
                metadata_scene_path,
                scene,
                selected_shots=sorted(selected_numbers),
                prompt_refinement=prompt_refinement,
            )
            print(f"Resume complete: {len(ordered_videos)} selected shot(s) ready")
        return

    generates_start_images = any(
        shot.generate_start_image is not None and index not in approved_images
        for index, shot in pending_entries
    )
    required_groups: list[str] = []
    if not args.start_image_only:
        if video_selection is None:
            raise RuntimeError("Video workflow selection is unavailable")
        required_groups.extend(video_selection.model_groups)
    if generates_start_images:
        if start_selection is None:
            raise RuntimeError("Start image workflow selection is unavailable")
        required_groups.extend(start_selection.model_groups)
    required_models = profile.models_for_groups(required_groups)
    rendered_videos = dict(resumed_videos)
    generated_image_count = 0

    session = (
        _worker_session(args, profile, models=required_models)
        if remote_details is None
        else _comfy_session(
            args,
            profile,
            remote_details=remote_details,
            models=required_models,
        )
    )
    with session as comfy:
        for index, shot in pending_entries:
            if args.start_image_only and shot.generate_start_image is None:
                continue
            shot_started = time.monotonic()
            print(f"Rendering shot {index}/{len(scene.shots)}: {shot.name}")
            start_image = input_snapshots.get((index, "start"))
            if index in approved_images:
                start_image = approved_images[index]
                print(f"Approved start keyframe: {start_image}")
            elif shot.generate_start_image:
                if start_image_workflow is None:
                    raise RuntimeError("Start image workflow was not loaded")
                print(f"Generating start keyframe for shot {index}: {shot.name}")
                generation_started = time.monotonic()
                generation_workflow = build_start_image_workflow(
                    start_selection.adapter,
                    start_image_workflow,
                    generations[index],
                    shot_number=index,
                    shot_name=shot.name,
                )
                _, generation_history = _queue_with_retries(
                    comfy,
                    generation_workflow,
                    args,
                    lambda message, shot_index=index: print(
                        f"[start image {shot_index}/{len(scene.shots)}] {message}",
                        flush=True,
                    ),
                )
                generated_dir = output_root / "000-generated-start-image"
                generated_outputs = _retry_operation(
                    "Start image download",
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
                        f"Start image generation for {shot.name!r} produced "
                        f"{len(generated_images)} images; expected 1"
                    )
                start_image = generated_images[0]
                generated_image_count += 1
                print(f"Generated start keyframe: {start_image}")
                if start_workflow_sha256 is None:
                    raise RuntimeError("Start image workflow fingerprint is unavailable")
                start_inputs = build_start_image_inputs(
                    shot,
                    index=index,
                    profile=profile,
                    generation=generations[index],
                    start_workflow=start_selection,
                    start_workflow_sha256=start_workflow_sha256,
                    prompt_refinement=prompt_refinement,
                )
                write_start_image_metadata(
                    _start_image_metadata_path(output_root, index, shot.name),
                    inputs=start_inputs,
                    image=start_image,
                    output_root=output_root,
                    runtime=getattr(comfy, "runtime_metadata", {}),
                    elapsed_seconds=time.monotonic() - generation_started,
                )
                write_render_manifest(
                    output_root,
                    metadata_scene_path,
                    scene,
                    selected_shots=sorted(selected_numbers),
                    prompt_refinement=prompt_refinement,
                )
            if args.start_image_only:
                continue
            if start_image is None:
                previous_index = index - 1
                previous_shot = scene.shots[previous_index - 1]
                start_image = _continuation_path(
                    output_root, previous_index, previous_shot.name
                )
            if start_image is None:
                raise RuntimeError(f"Shot {shot.name!r} has no available start image")
            shot_inputs = build_shot_inputs(
                scene,
                shot,
                index=index,
                start_image=start_image,
                profile=profile,
                video_workflow=video_selection,
                video_workflow_sha256=video_workflow_sha256,
                video_output_suffix=video_output_suffix,
                start_workflow=start_selection,
                start_workflow_sha256=start_workflow_sha256,
                generation=generations.get(index),
                starting_state=(
                    scene.shots[index - 2].end_state if index > 1 else ""
                ),
                end_image=input_snapshots.get((index, "end")),
                prompt_refinement=prompt_refinement,
            )
            start_remote = f"scene-{index:03d}-start{start_image.suffix.lower() or '.png'}"
            start_remote = _retry_operation(
                f"Start keyframe upload for shot {index}",
                getattr(args, "retries", 2),
                lambda: comfy.upload_image(start_image, start_remote),
            )
            print(f"Uploaded start keyframe: {start_remote}")

            end_remote: str | None = None
            if shot.end_image:
                end_image = input_snapshots[(index, "end")]
                end_remote = f"scene-{index:03d}-end{end_image.suffix.lower() or '.png'}"
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
            print(f"Extracted continuation frame: {continuation}")
            _prune_old_shot_videos(
                shot_dir, video_outputs[0], video_output_suffix
            )
            write_shot_metadata(
                shot_dir / "metadata.json",
                inputs=shot_inputs,
                video=video_outputs[0],
                continuation=continuation,
                output_root=output_root,
                runtime=getattr(comfy, "runtime_metadata", {}),
                elapsed_seconds=time.monotonic() - shot_started,
            )
            write_render_manifest(
                output_root,
                metadata_scene_path,
                scene,
                selected_shots=sorted(selected_numbers),
                prompt_refinement=prompt_refinement,
            )

    if args.start_image_only:
        write_render_manifest(
            output_root,
            metadata_scene_path,
            scene,
            selected_shots=sorted(selected_numbers),
            prompt_refinement=prompt_refinement,
        )
        print(f"Generated {generated_image_count} start keyframe(s) in {output_root}")
        return
    if not all_shots_selected:
        ordered = [rendered_videos[index] for index, _ in shot_entries]
        print(f"Shots rendered: {len(ordered)} selected shot(s)")
        for video in ordered:
            print(f"Shot rendered: {video}")
        write_render_manifest(
            output_root,
            metadata_scene_path,
            scene,
            selected_shots=sorted(selected_numbers),
            prompt_refinement=prompt_refinement,
        )
        return
    final_video = output_root / f"{slugify(scene.title)}{video_output_suffix}"
    concatenate_webm(
        [rendered_videos[index] for index, _ in shot_entries], final_video
    )
    write_render_manifest(
        output_root,
        metadata_scene_path,
        scene,
        selected_shots=sorted(selected_numbers),
        final_video=final_video,
        prompt_refinement=prompt_refinement,
    )
    print(f"Scene assembled: {final_video}")


def cleanup(args: argparse.Namespace) -> None:
    cutoff = datetime.now(UTC) - timedelta(hours=args.max_age_hours)
    with RunPodClient(_api_key()) as client:
        for pod in client.list_pods():
            name = str(pod.get("name", ""))
            if not name.startswith("runpod-video-"):
                continue
            pod_id = str(pod["id"])
            if args.all:
                print(f"Terminating managed pod {pod_id} ({name})")
                client.terminate_pod(pod_id)
                continue
            created_raw = pod.get("createdAt") or pod.get("lastStartedAt")
            if not isinstance(created_raw, str):
                print(f"Skipping pod {pod_id}: no creation timestamp")
                continue
            normalized = created_raw.removesuffix(" UTC")
            if (
                len(normalized) >= 6
                and normalized[-6] == " "
                and normalized[-5] in "+-"
                and normalized[-4:].isdigit()
            ):
                normalized = normalized[:-6] + normalized[-5:]
            try:
                created = datetime.fromisoformat(
                    normalized.replace("Z", "+00:00")
                )
            except ValueError:
                print(f"Skipping pod {pod_id}: invalid timestamp {created_raw!r}")
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created < cutoff:
                print(f"Terminating stale pod {pod_id} ({name})")
                client.terminate_pod(pod_id)


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
    setup_parser.add_argument(
        "--include-refiner",
        action="store_true",
        help="Also install the pinned prompt-refiner runtime and model",
    )
    setup_parser.add_argument("--refiner-profile")
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

    refine_parser = subparsers.add_parser(
        "refine", help="Refine and cache prompt fields in a scene manifest"
    )
    refine_parser.add_argument(
        "manifest", help="Scene JSON file or project directory containing scene.json"
    )
    refine_parser.add_argument("--profile")
    refine_parser.add_argument("--refiner-profile")
    refine_parser.add_argument("--output")
    refine_parser.add_argument("--ssh-key")
    refine_parser.add_argument("--pod-id", help="Reuse an existing Pod")
    refine_parser.add_argument(
        "--restart",
        action="store_true",
        help="Stop the active workload before refinement; requires --pod-id",
    )
    refine_parser.add_argument("--start-timeout", type=int, default=900)
    refine_parser.add_argument(
        "--force", action="store_true", help="Ignore a valid refinement cache entry"
    )
    refine_parser.add_argument(
        "--apply", action="store_true", help="Allow creation of billable resources"
    )
    refine_lifecycle = refine_parser.add_mutually_exclusive_group()
    refine_lifecycle.add_argument("--keep-pod", action="store_true")
    refine_lifecycle.add_argument("--stop-pod", action="store_true")
    refine_parser.set_defaults(func=refine)

    chat_parser = subparsers.add_parser(
        "chat", help="Open the prompt-refiner web UI through a loopback SSH tunnel"
    )
    chat_parser.add_argument("--profile")
    chat_parser.add_argument("--refiner-profile")
    chat_parser.add_argument("--ssh-key")
    chat_parser.add_argument("--pod-id", help="Reuse an existing Pod")
    chat_parser.add_argument(
        "--restart",
        action="store_true",
        help="Stop the active workload before starting chat; requires --pod-id",
    )
    chat_parser.add_argument("--start-timeout", type=int, default=900)
    chat_parser.add_argument("--duration-seconds", type=float)
    chat_parser.add_argument("--no-browser", action="store_true")
    chat_parser.add_argument(
        "--apply", action="store_true", help="Allow creation of billable resources"
    )
    chat_lifecycle = chat_parser.add_mutually_exclusive_group()
    chat_lifecycle.add_argument("--keep-pod", action="store_true")
    chat_lifecycle.add_argument("--stop-pod", action="store_true")
    chat_parser.set_defaults(func=chat)

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
        "--approve-start-images",
        action="store_true",
        help="Use previously generated start images without regenerating them",
    )
    scene_parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse valid completed shots and generated start images",
    )
    scene_parser.add_argument(
        "--backfill-metadata",
        action="store_true",
        help="Infer missing metadata from existing local outputs without using a Pod",
    )
    scene_parser.add_argument(
        "--refine-prompts",
        action="store_true",
        help="Refine prompt fields before rendering, using a deterministic cache",
    )
    scene_parser.add_argument("--refiner-profile")
    scene_parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore a valid prompt-refinement cache entry",
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
