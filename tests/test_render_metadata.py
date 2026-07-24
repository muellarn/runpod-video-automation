from pathlib import Path

from runpod_video_automation.config import ModelFile, Profile
from runpod_video_automation.render_metadata import (
    build_shot_inputs,
    read_metadata,
    validate_shot_metadata,
    write_shot_metadata,
)
from runpod_video_automation.scene import Scene, Shot


def _scene(prompt: str = "Walks forward") -> tuple[Scene, Shot, Profile]:
    shot = Shot(
        name="Opening",
        prompt=prompt,
        camera="locked camera",
        negative_prompt="blur",
        start_image=None,
        generate_start_image=None,
        end_image=None,
        duration_seconds=5,
        frames=81,
        seed=42,
        cfg=3.5,
    )
    scene = Scene(
        title="Test",
        global_prompt="Same adult character",
        negative_prompt="flicker",
        width=576,
        height=800,
        fps=16,
        steps=20,
        transition_step=10,
        cfg=3.5,
        shots=(shot,),
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
        models=(ModelFile("https://example.test/model", "models/unet/model", 1),),
        start_image_models=(),
        comfy_args=("--enable-triton-backend",),
    )
    return scene, shot, profile


def test_shot_metadata_round_trip_and_detects_input_change(tmp_path: Path) -> None:
    scene, shot, profile = _scene()
    start_image = tmp_path / "start.png"
    start_image.write_bytes(b"start")
    output_root = tmp_path / "output"
    video = output_root / "001-opening/shot.webm"
    continuation = output_root / "001-opening/continuation.png"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    continuation.write_bytes(b"continuation")
    inputs = build_shot_inputs(
        scene,
        shot,
        index=1,
        start_image=start_image,
        profile=profile,
        video_workflow_sha256="a" * 64,
        start_workflow_sha256=None,
    )
    metadata_path = video.parent / "metadata.json"
    write_shot_metadata(
        metadata_path,
        inputs=inputs,
        video=video,
        continuation=continuation,
        output_root=output_root,
        runtime={"pod_id": "pod-1", "gpu": "GPU"},
        elapsed_seconds=12.5,
    )

    metadata = read_metadata(metadata_path)
    reused_video, differences = validate_shot_metadata(
        metadata, inputs, output_root
    )

    assert reused_video == video
    assert differences == []
    assert inputs["runtime"]["comfy_args"] == ["--enable-triton-backend"]
    changed_scene, changed_shot, _ = _scene("Turns around")
    changed_inputs = build_shot_inputs(
        changed_scene,
        changed_shot,
        index=1,
        start_image=start_image,
        profile=profile,
        video_workflow_sha256="a" * 64,
        start_workflow_sha256=None,
    )
    _, differences = validate_shot_metadata(metadata, changed_inputs, output_root)
    assert any("prompts.shot" in difference for difference in differences)


def test_shot_metadata_detects_modified_output(tmp_path: Path) -> None:
    scene, shot, profile = _scene()
    output_root = tmp_path / "output"
    video = output_root / "001-opening/shot.webm"
    continuation = output_root / "001-opening/continuation.png"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    continuation.write_bytes(b"continuation")
    inputs = build_shot_inputs(
        scene,
        shot,
        index=1,
        start_image=None,
        profile=profile,
        video_workflow_sha256="a" * 64,
        start_workflow_sha256=None,
    )
    metadata_path = video.parent / "metadata.json"
    metadata = write_shot_metadata(
        metadata_path,
        inputs=inputs,
        video=video,
        continuation=continuation,
        output_root=output_root,
        runtime={},
        elapsed_seconds=1,
    )
    video.write_bytes(b"tampered")

    _, differences = validate_shot_metadata(metadata, inputs, output_root)

    assert any("outputs.video.sha256" in difference for difference in differences)
