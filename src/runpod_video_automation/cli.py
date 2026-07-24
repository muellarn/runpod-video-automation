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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from runpod_video_automation.comfy_client import ComfyClient
from runpod_video_automation.config import ModelFile, Profile
from runpod_video_automation.remote import RemoteWorker
from runpod_video_automation.render_metadata import (
    build_shot_inputs,
    build_start_image_inputs,
    fingerprint,
    read_metadata,
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
    build_start_image_workflow,
    build_shot_workflow,
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
    model_count = len(profile.models)
    print(f"Profile: {profile.name}")
    print(f"Image: {profile.image}")
    print(f"Data center: {profile.data_center_id}")
    print(f"Persistent volume: {profile.volume_name} ({profile.volume_size_gb} GB)")
    print(f"GPU fallback order: {', '.join(profile.gpu_type_ids)}")
    print(f"Model files: {model_count}")
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
    if args.restart and not args.pod_id:
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
def _worker_session(
    args: argparse.Namespace,
    profile: Profile,
    *,
    models: tuple[ModelFile, ...] | None = None,
) -> Iterator[ComfyClient]:
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
            remote.ensure_models(models if models is not None else profile.models)
            if profile.comfy_args:
                remote.ensure_comfy_args(
                    profile.comfy_args,
                    system_packages=profile.system_packages,
                )
            elif profile.system_packages:
                remote.ensure_system_packages(profile.system_packages)
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
                    if args.restart:
                        print("Interrupting active ComfyUI execution and clearing queue")
                        comfy.interrupt_and_clear()
                    yield comfy
                finally:
                    comfy.close()
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


def run(args: argparse.Namespace) -> None:
    if not args.apply:
        raise RuntimeError("Refusing to create billable resources without --apply")
    _validate_execution_args(args)
    profile = Profile.load(_profile_path(args.profile))
    workflow = load_workflow(Path(args.workflow))
    apply_overrides(workflow, args.set or [])
    with _worker_session(args, profile) as comfy:
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


def _scene_plan(scene: Scene) -> None:
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
            source = (
                f"generated with {shot.generate_start_image.model_type} "
                f"({shot.generate_start_image.checkpoint}) "
                f"at {shot.generate_start_image.width}x"
                f"{shot.generate_start_image.height}"
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


def _prune_old_shot_videos(shot_dir: Path, keep: Path) -> None:
    for path in shot_dir.glob("*.webm"):
        if path != keep:
            path.unlink()


def _selected_shot_entries(
    scene: Scene, args: argparse.Namespace
) -> list[tuple[int, Shot]]:
    shot_number = getattr(args, "shot", None)
    shot_numbers = getattr(args, "shots", None)
    selected = shot_numbers or ((shot_number,) if shot_number is not None else None)
    if selected is None:
        return list(enumerate(scene.shots, start=1))
    invalid = [number for number in selected if number > len(scene.shots)]
    if invalid:
        raise ValueError(
            f"Selected shots must be between 1 and {len(scene.shots)}, got "
            f"{', '.join(map(str, invalid))}"
        )
    return [(number, scene.shots[number - 1]) for number in selected]


def render_scene(args: argparse.Namespace) -> None:
    scene_path = Path(args.manifest)
    scene = Scene.load(scene_path)
    shot_entries = _selected_shot_entries(scene, args)
    selected_numbers = {index for index, _ in shot_entries}
    all_shots_selected = len(selected_numbers) == len(scene.shots)
    _scene_plan(scene)
    if not all_shots_selected:
        print(f"Selected shots: {', '.join(map(str, sorted(selected_numbers)))}")
    if args.plan:
        if args.apply:
            raise ValueError("Use either --plan or --apply, not both")
        return
    if not args.apply:
        raise RuntimeError("Refusing to create billable resources without --apply")
    _validate_execution_args(args)
    if args.start_image_only and getattr(args, "approve_start_images", False):
        raise ValueError("Use either --start-image-only or --approve-start-images")
    output_root = Path(args.output)
    if not args.start_image_only and shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to assemble scene outputs")
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

    configured_start_images = any(
        shot.generate_start_image is not None for _, shot in shot_entries
    )
    if args.start_image_only and not configured_start_images:
        raise ValueError(
            "--start-image-only requires at least one generate_start_image shot"
        )
    profile = Profile.load(_profile_path(args.profile))
    base_workflow: dict[str, Any] | None = None
    video_workflow_sha256 = ""
    if not args.start_image_only:
        workflow_path = Path(args.workflow)
        if not workflow_path.is_absolute():
            workflow_path = PROJECT_ROOT / workflow_path
        base_workflow = load_workflow(workflow_path)
        video_workflow_sha256 = fingerprint(base_workflow)
    start_image_workflow: dict[str, Any] | None = None
    start_workflow_sha256: str | None = None
    if configured_start_images:
        start_workflow_path = Path(args.start_image_workflow)
        if not start_workflow_path.is_absolute():
            start_workflow_path = PROJECT_ROOT / start_workflow_path
        start_image_workflow = load_workflow(start_workflow_path)
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
            start_workflow_sha256=start_workflow_sha256,
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
                expected_start_image = shot.start_image
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
                video_workflow_sha256=video_workflow_sha256,
                start_workflow_sha256=start_workflow_sha256,
                starting_state=(
                    scene.shots[index - 2].end_state if index > 1 else ""
                ),
            )
            metadata_path = _shot_dir(output_root, index, shot.name) / "metadata.json"
            metadata = read_metadata(metadata_path)
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
                scene_path,
                scene,
                selected_shots=sorted(selected_numbers),
            )
            return
        ordered_videos = [resumed_videos[index] for index, _ in shot_entries]
        if all_shots_selected:
            final_video = output_root / f"{slugify(scene.title)}.webm"
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

    generates_start_images = any(
        shot.generate_start_image is not None and index not in approved_images
        for index, shot in pending_entries
    )
    required_models = () if args.start_image_only else profile.models
    if generates_start_images:
        required_models += profile.start_image_models
    rendered_videos = dict(resumed_videos)
    generated_image_count = 0

    with _worker_session(args, profile, models=required_models) as comfy:
        for index, shot in pending_entries:
            if args.start_image_only and shot.generate_start_image is None:
                continue
            shot_started = time.monotonic()
            print(f"Rendering shot {index}/{len(scene.shots)}: {shot.name}")
            start_image = shot.start_image
            if index in approved_images:
                start_image = approved_images[index]
                print(f"Approved start keyframe: {start_image}")
            elif shot.generate_start_image:
                if start_image_workflow is None:
                    raise RuntimeError("Start image workflow was not loaded")
                print(f"Generating start keyframe for shot {index}: {shot.name}")
                generation_started = time.monotonic()
                generation_workflow = build_start_image_workflow(
                    start_image_workflow,
                    shot.generate_start_image,
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
                    start_workflow_sha256=start_workflow_sha256,
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
                    scene_path,
                    scene,
                    selected_shots=sorted(selected_numbers),
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
                video_workflow_sha256=video_workflow_sha256,
                start_workflow_sha256=start_workflow_sha256,
                starting_state=(
                    scene.shots[index - 2].end_state if index > 1 else ""
                ),
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
                end_remote = f"scene-{index:03d}-end{shot.end_image.suffix.lower() or '.png'}"
                end_remote = _retry_operation(
                    f"End keyframe upload for shot {index}",
                    getattr(args, "retries", 2),
                    lambda: comfy.upload_image(shot.end_image, end_remote),
                )
                print(f"Uploaded end keyframe: {end_remote}")

            if base_workflow is None:
                raise RuntimeError("Video workflow was not loaded")
            workflow = build_shot_workflow(
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
            webm_outputs = [path for path in outputs if path.suffix.lower() == ".webm"]
            if len(webm_outputs) != 1:
                raise RuntimeError(
                    f"Shot {shot.name!r} produced {len(webm_outputs)} WebM outputs; expected 1"
                )
            rendered_videos[index] = webm_outputs[0]
            for output in outputs:
                print(f"Downloaded: {output}")

            continuation = shot_dir / "continuation.png"
            extract_last_frame(webm_outputs[0], continuation)
            print(f"Extracted continuation frame: {continuation}")
            _prune_old_shot_videos(shot_dir, webm_outputs[0])
            write_shot_metadata(
                shot_dir / "metadata.json",
                inputs=shot_inputs,
                video=webm_outputs[0],
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

    if args.start_image_only:
        write_render_manifest(
            output_root,
            scene_path,
            scene,
            selected_shots=sorted(selected_numbers),
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
            scene_path,
            scene,
            selected_shots=sorted(selected_numbers),
        )
        return
    final_video = output_root / f"{slugify(scene.title)}.webm"
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

    run_parser = subparsers.add_parser("run", help="Provision, render, download, and terminate")
    run_parser.add_argument("workflow", help="ComfyUI workflow exported in API format")
    run_parser.add_argument("--profile")
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
    scene_parser.add_argument("manifest", help="Scene manifest in JSON format")
    scene_parser.add_argument(
        "--workflow", default="workflows/wan22-i2v-14b-api.json"
    )
    scene_parser.add_argument(
        "--start-image-workflow",
        default="workflows/z-image-turbo-start-image-api.json",
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
    scene_parser.add_argument("--output", default="output/scene")
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
