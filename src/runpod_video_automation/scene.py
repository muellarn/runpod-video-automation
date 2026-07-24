from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Scene field '{field}' must be a non-empty string")
    return value.strip()


def _optional_string(value: object, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Scene field '{field}' must be a string")
    return value.strip()


def _image_path(value: object, field: str, base_dir: Path) -> Path | None:
    if value is None:
        return None
    raw_path = _required_string(value, field)
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"Scene image not found for '{field}': {path}")
    return path


def duration_to_frames(duration_seconds: float, fps: float) -> int:
    if duration_seconds <= 0 or fps <= 0:
        raise ValueError("Duration and FPS must be positive")
    target = duration_seconds * fps
    return max(5, int(((target - 1) / 4) + 0.5) * 4 + 1)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "shot"


def _ffconcat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


@dataclass(frozen=True)
class StartImageGeneration:
    adapter: str | None
    prompt: str
    negative_prompt: str
    checkpoint: str | None
    width: int
    height: int
    seed: int
    steps: int | None
    cfg: float | None
    sampler_name: str | None
    scheduler: str | None


@dataclass(frozen=True)
class Shot:
    name: str
    prompt: str
    camera: str
    negative_prompt: str
    start_image: Path | None
    generate_start_image: StartImageGeneration | None
    end_image: Path | None
    duration_seconds: float
    frames: int
    seed: int
    cfg: float
    end_state: str = ""


@dataclass(frozen=True)
class Scene:
    title: str
    global_prompt: str
    negative_prompt: str
    width: int
    height: int
    fps: float
    steps: int
    transition_step: int
    cfg: float
    shots: tuple[Shot, ...]

    @classmethod
    def load(cls, path: Path) -> Scene:
        path = path.expanduser().resolve()
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("The scene manifest must be a JSON object")

        title = _required_string(data.get("title"), "title")
        global_prompt = _optional_string(data.get("global_prompt"), "global_prompt")
        negative_prompt = _optional_string(
            data.get("negative_prompt"), "negative_prompt"
        )
        width = int(data.get("width", 768))
        height = int(data.get("height", 768))
        fps = float(data.get("fps", 16))
        steps = int(data.get("steps", 20))
        transition_step = int(data.get("transition_step", steps // 2))
        cfg = float(data.get("cfg", 3.5))
        if width <= 0 or height <= 0 or width % 16 or height % 16:
            raise ValueError("Scene width and height must be positive multiples of 16")
        if fps <= 0:
            raise ValueError("Scene FPS must be positive")
        if steps < 2 or not 0 < transition_step < steps:
            raise ValueError("Scene transition_step must be between 1 and steps - 1")
        if cfg <= 0:
            raise ValueError("Scene CFG must be positive")

        raw_shots = data.get("shots")
        if not isinstance(raw_shots, list) or not raw_shots:
            raise ValueError("Scene field 'shots' must be a non-empty list")
        shots: list[Shot] = []
        names: set[str] = set()
        for index, raw_shot in enumerate(raw_shots, start=1):
            if not isinstance(raw_shot, dict):
                raise ValueError(f"Scene shot {index} must be an object")
            name = _required_string(raw_shot.get("name"), f"shots[{index}].name")
            if name in names:
                raise ValueError(f"Scene shot name is duplicated: {name!r}")
            names.add(name)
            prompt = _optional_string(
                raw_shot.get("prompt"), f"shots[{index}].prompt"
            )
            if not global_prompt and not prompt:
                raise ValueError(f"Scene shot {name!r} has no prompt")
            camera = _optional_string(
                raw_shot.get("camera"), f"shots[{index}].camera"
            )
            shot_negative = _optional_string(
                raw_shot.get("negative_prompt"),
                f"shots[{index}].negative_prompt",
            )
            end_state = _optional_string(
                raw_shot.get("end_state"),
                f"shots[{index}].end_state",
            )
            start_image = _image_path(
                raw_shot.get("start_image"),
                f"shots[{index}].start_image",
                path.parent,
            )
            raw_generation = raw_shot.get("generate_start_image")
            generation: StartImageGeneration | None = None
            if raw_generation is not None:
                if not isinstance(raw_generation, dict):
                    raise ValueError(
                        f"Scene field 'shots[{index}].generate_start_image' "
                        "must be an object"
                    )
                if "model_type" in raw_generation:
                    raise ValueError(
                        f"Scene field 'shots[{index}].generate_start_image.model_type' "
                        "was replaced by 'adapter'"
                    )
                generation_width = int(raw_generation.get("width", width))
                generation_height = int(raw_generation.get("height", height))
                generation_seed = int(raw_generation.get("seed", 1000 + index))
                raw_adapter = raw_generation.get("adapter")
                if raw_adapter is not None and (
                    not isinstance(raw_adapter, str) or not raw_adapter.strip()
                ):
                    raise ValueError(
                        f"Scene field 'shots[{index}].generate_start_image.adapter' "
                        "must be a non-empty string"
                    )
                generation_adapter = raw_adapter.strip() if raw_adapter else None
                generation_steps = (
                    int(raw_generation["steps"])
                    if raw_generation.get("steps") is not None
                    else None
                )
                generation_cfg = (
                    float(raw_generation["cfg"])
                    if raw_generation.get("cfg") is not None
                    else None
                )
                if (
                    generation_width <= 0
                    or generation_height <= 0
                    or generation_width % 8
                    or generation_height % 8
                ):
                    raise ValueError(
                        f"Scene shot {name!r} generated image dimensions must be "
                        "positive multiples of 8"
                    )
                if not 0 <= generation_seed <= 0xFFFFFFFFFFFFFFFF:
                    raise ValueError(
                        f"Scene shot {name!r} generated image seed is out of range"
                    )
                if (generation_steps is not None and generation_steps <= 0) or (
                    generation_cfg is not None and generation_cfg <= 0
                ):
                    raise ValueError(
                        f"Scene shot {name!r} generated image steps and CFG "
                        "must be positive"
                    )
                generation_negative_prompt = _optional_string(
                    raw_generation.get("negative_prompt"),
                    f"shots[{index}].generate_start_image.negative_prompt",
                )
                generation = StartImageGeneration(
                    adapter=generation_adapter,
                    prompt=_required_string(
                        raw_generation.get("prompt"),
                        f"shots[{index}].generate_start_image.prompt",
                    ),
                    negative_prompt=generation_negative_prompt,
                    checkpoint=(
                        _required_string(
                            raw_generation.get("checkpoint"),
                            f"shots[{index}].generate_start_image.checkpoint",
                        )
                        if raw_generation.get("checkpoint") is not None
                        else None
                    ),
                    width=generation_width,
                    height=generation_height,
                    seed=generation_seed,
                    steps=generation_steps,
                    cfg=generation_cfg,
                    sampler_name=(
                        _required_string(
                            raw_generation.get("sampler_name"),
                            f"shots[{index}].generate_start_image.sampler_name",
                        )
                        if raw_generation.get("sampler_name") is not None
                        else None
                    ),
                    scheduler=(
                        _required_string(
                            raw_generation.get("scheduler"),
                            f"shots[{index}].generate_start_image.scheduler",
                        )
                        if raw_generation.get("scheduler") is not None
                        else None
                    ),
                )
            if start_image is not None and generation is not None:
                raise ValueError(
                    f"Scene shot {name!r} cannot set both start_image and "
                    "generate_start_image"
                )
            end_image = _image_path(
                raw_shot.get("end_image"),
                f"shots[{index}].end_image",
                path.parent,
            )
            if index == 1 and start_image is None and generation is None:
                raise ValueError(
                    "The first scene shot requires start_image or "
                    "generate_start_image"
                )
            duration_seconds = float(raw_shot.get("duration_seconds", 5.0))
            frames = duration_to_frames(duration_seconds, fps)
            seed = int(raw_shot.get("seed", 41 + index))
            shot_cfg = float(raw_shot.get("cfg", cfg))
            if not 0 <= seed <= 0xFFFFFFFFFFFFFFFF:
                raise ValueError(f"Scene shot {name!r} seed is out of range")
            if shot_cfg <= 0:
                raise ValueError(f"Scene shot {name!r} CFG must be positive")
            shots.append(
                Shot(
                    name=name,
                    prompt=prompt,
                    camera=camera,
                    negative_prompt=shot_negative,
                    start_image=start_image,
                    generate_start_image=generation,
                    end_image=end_image,
                    duration_seconds=duration_seconds,
                    frames=frames,
                    seed=seed,
                    cfg=shot_cfg,
                    end_state=end_state,
                )
            )
        return cls(
            title=title,
            global_prompt=global_prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            fps=fps,
            steps=steps,
            transition_step=transition_step,
            cfg=cfg,
            shots=tuple(shots),
        )

    @property
    def duration_seconds(self) -> float:
        return sum(shot.frames / self.fps for shot in self.shots)


def extract_last_frame(video: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-sseof",
            "-1",
            "-i",
            str(video),
            "-fps_mode",
            "passthrough",
            "-update",
            "1",
            str(destination),
        ],
        check=True,
    )


def concatenate_webm(videos: list[Path], destination: Path) -> None:
    if not videos:
        raise ValueError("No WebM clips were produced")
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = "".join(f"file '{_ffconcat_path(video)}'\n" for video in videos)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp_dir:
        manifest_path = Path(temp_dir) / "clips.ffconcat"
        manifest_path.write_text(manifest)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest_path),
                "-c",
                "copy",
                str(destination),
            ],
            check=True,
        )
