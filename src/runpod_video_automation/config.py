from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelFile:
    url: str
    path: str
    size: int | None = None
    sha256: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ModelFile:
        url = value.get("url")
        path = value.get("path")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("Every model URL must use HTTPS")
        if not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts:
            raise ValueError("Model paths must be relative and may not contain '..'")
        raw_size = value.get("size")
        size = int(raw_size) if raw_size is not None else None
        if size is not None and size <= 0:
            raise ValueError("Model size must be positive")
        raw_sha256 = value.get("sha256")
        sha256 = str(raw_sha256).lower() if raw_sha256 is not None else None
        if sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Model SHA-256 must contain 64 hexadecimal characters")
        return cls(url=url, path=path, size=size, sha256=sha256)


@dataclass(frozen=True)
class Profile:
    name: str
    image: str
    data_center_id: str
    volume_name: str
    volume_size_gb: int
    gpu_type_ids: tuple[str, ...]
    min_ram_per_gpu: int
    min_vcpu_per_gpu: int
    container_disk_gb: int
    max_hourly_cost: float
    models: tuple[ModelFile, ...]
    start_image_models: tuple[ModelFile, ...]

    @classmethod
    def load(cls, path: Path) -> Profile:
        data = json.loads(path.read_text())
        required_strings = (
            "name",
            "image",
            "data_center_id",
            "volume_name",
        )
        for key in required_strings:
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"Profile field '{key}' must be a non-empty string")
        gpu_type_ids = data.get("gpu_type_ids")
        if not isinstance(gpu_type_ids, list) or not all(
            isinstance(item, str) and item for item in gpu_type_ids
        ):
            raise ValueError("Profile field 'gpu_type_ids' must be a string list")
        models = data.get("models", [])
        if not isinstance(models, list):
            raise ValueError("Profile field 'models' must be a list")
        start_image_models = data.get("start_image_models", [])
        if not isinstance(start_image_models, list):
            raise ValueError("Profile field 'start_image_models' must be a list")
        max_hourly_cost = float(data.get("max_hourly_cost", 3.0))
        if max_hourly_cost <= 0:
            raise ValueError("Profile field 'max_hourly_cost' must be positive")
        return cls(
            name=data["name"],
            image=data["image"],
            data_center_id=data["data_center_id"],
            volume_name=data["volume_name"],
            volume_size_gb=int(data.get("volume_size_gb", 250)),
            gpu_type_ids=tuple(gpu_type_ids),
            min_ram_per_gpu=int(data.get("min_ram_per_gpu", 64)),
            min_vcpu_per_gpu=int(data.get("min_vcpu_per_gpu", 8)),
            container_disk_gb=int(data.get("container_disk_gb", 50)),
            max_hourly_cost=max_hourly_cost,
            models=tuple(ModelFile.from_dict(item) for item in models),
            start_image_models=tuple(
                ModelFile.from_dict(item) for item in start_image_models
            ),
        )
