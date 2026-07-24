from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _relative_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Profile field '{field_name}' must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Profile field '{field_name}' must be a relative path")
    return path.as_posix()


@dataclass(frozen=True)
class ModelFile:
    url: str
    path: str
    size: int | None = None
    sha256: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ModelFile:
        url = value.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("Every model URL must use HTTPS")
        path = _relative_path(value.get("path"), "model.path")
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
class ModelPathAlias:
    source: str
    target: str

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ModelPathAlias:
        source = _relative_path(value.get("source"), "model_path_aliases.source")
        target = _relative_path(value.get("target"), "model_path_aliases.target")
        if source == target:
            raise ValueError("Model path alias source and target must differ")
        return cls(source=source, target=target)


@dataclass(frozen=True)
class WorkflowPreset:
    path: Path
    adapter: str
    model_groups: tuple[str, ...]
    defaults: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls,
        value: dict[str, object],
        *,
        profile_directory: Path,
        known_groups: set[str],
        name: str,
    ) -> WorkflowPreset:
        raw_path = value.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(f"Workflow preset '{name}' requires a path")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = profile_directory / path
        adapter = value.get("adapter")
        if not isinstance(adapter, str) or not adapter:
            raise ValueError(f"Workflow preset '{name}' requires an adapter")
        raw_groups = value.get("model_groups", [])
        if not isinstance(raw_groups, list) or not all(
            isinstance(group, str) and group for group in raw_groups
        ):
            raise ValueError(
                f"Workflow preset '{name}' model_groups must be a string list"
            )
        unknown = sorted(set(raw_groups) - known_groups)
        if unknown:
            raise ValueError(
                f"Workflow preset '{name}' references unknown model groups: "
                f"{', '.join(unknown)}"
            )
        defaults = value.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ValueError(f"Workflow preset '{name}' defaults must be an object")
        return cls(
            path=path.resolve(),
            adapter=adapter,
            model_groups=tuple(raw_groups),
            defaults=dict(defaults),
        )


@dataclass(frozen=True)
class WorkflowSelection:
    name: str
    path: Path
    adapter: str
    model_groups: tuple[str, ...]
    models: tuple[ModelFile, ...]
    defaults: dict[str, Any]


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
    model_groups: dict[str, tuple[ModelFile, ...]] = field(default_factory=dict)
    model_path_aliases: tuple[ModelPathAlias, ...] = ()
    workflows: dict[str, WorkflowPreset] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Profile:
        path = path.expanduser().resolve()
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Profile must be a JSON object")
        legacy_fields = sorted({"models", "start_image_models"} & set(data))
        if legacy_fields:
            raise ValueError(
                "Legacy profile fields are not supported; migrate "
                f"{', '.join(legacy_fields)} to named model_groups"
            )
        required_strings = ("name", "image", "data_center_id", "volume_name")
        for key in required_strings:
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"Profile field '{key}' must be a non-empty string")
        gpu_type_ids = data.get("gpu_type_ids")
        if not isinstance(gpu_type_ids, list) or not all(
            isinstance(item, str) and item for item in gpu_type_ids
        ):
            raise ValueError("Profile field 'gpu_type_ids' must be a string list")
        raw_groups = data.get("model_groups", {})
        if not isinstance(raw_groups, dict):
            raise ValueError("Profile field 'model_groups' must be an object")
        model_groups: dict[str, tuple[ModelFile, ...]] = {}
        for group_name, raw_models in raw_groups.items():
            if not isinstance(group_name, str) or not group_name:
                raise ValueError("Model group names must be non-empty strings")
            if not isinstance(raw_models, list):
                raise ValueError(f"Model group '{group_name}' must be a list")
            model_groups[group_name] = tuple(
                ModelFile.from_dict(model) for model in raw_models
            )
        raw_aliases = data.get("model_path_aliases", [])
        if not isinstance(raw_aliases, list):
            raise ValueError("Profile field 'model_path_aliases' must be a list")
        aliases = tuple(ModelPathAlias.from_dict(alias) for alias in raw_aliases)
        raw_workflows = data.get("workflows", {})
        if not isinstance(raw_workflows, dict):
            raise ValueError("Profile field 'workflows' must be an object")
        workflows = {
            name: WorkflowPreset.from_dict(
                preset,
                profile_directory=path.parent,
                known_groups=set(model_groups),
                name=name,
            )
            for name, preset in raw_workflows.items()
            if isinstance(name, str) and isinstance(preset, dict)
        }
        if len(workflows) != len(raw_workflows):
            raise ValueError("Workflow names and definitions must be valid objects")
        max_hourly_cost = float(data.get("max_hourly_cost", 3.0))
        if max_hourly_cost <= 0:
            raise ValueError("Profile field 'max_hourly_cost' must be positive")
        profile = cls(
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
            model_groups=model_groups,
            model_path_aliases=aliases,
            workflows=workflows,
        )
        profile.models_for_groups(profile.model_groups)
        return profile

    def models_for_groups(self, group_names: Iterable[str]) -> tuple[ModelFile, ...]:
        names = tuple(group_names)
        unknown = sorted(set(names) - set(self.model_groups))
        if unknown:
            raise ValueError(f"Unknown model groups: {', '.join(unknown)}")
        selected: dict[str, ModelFile] = {}
        for name in names:
            for model in self.model_groups[name]:
                existing = selected.get(model.path)
                if existing is not None and existing != model:
                    raise ValueError(
                        f"Conflicting model definitions for path {model.path!r}"
                    )
                selected.setdefault(model.path, model)
        models = tuple(selected.values())
        physical_paths = set(selected)
        alias_targets: dict[str, str] = {}
        for model in models:
            for alias in self.model_path_aliases:
                try:
                    relative = Path(model.path).relative_to(alias.source)
                except ValueError:
                    continue
                target = (Path(alias.target) / relative).as_posix()
                if target in physical_paths and target != model.path:
                    raise ValueError(
                        f"Model path alias target {target!r} collides with a model file"
                    )
                existing = alias_targets.get(target)
                if existing is not None and existing != model.path:
                    raise ValueError(f"Model path alias collision at {target!r}")
                alias_targets[target] = model.path
        exposed_paths = physical_paths | set(alias_targets)
        for path in exposed_paths:
            for parent in Path(path).parents:
                parent_path = parent.as_posix()
                if parent_path == ".":
                    break
                if parent_path in exposed_paths:
                    raise ValueError(
                        f"Model path {path!r} is nested below model file "
                        f"{parent_path!r}"
                    )
        return models

    @property
    def default_model_groups(self) -> tuple[str, ...]:
        referenced = {
            group for workflow in self.workflows.values() for group in workflow.model_groups
        }
        return tuple(name for name in self.model_groups if name in referenced) or tuple(
            self.model_groups
        )

    def select_workflow(
        self,
        name: str,
        *,
        path: Path | None = None,
        adapter: str | None = None,
        model_groups: tuple[str, ...] | None = None,
    ) -> WorkflowSelection:
        preset = self.workflows.get(name)
        if preset is None:
            raise ValueError(f"Profile has no workflow preset {name!r}")
        selected_groups = model_groups if model_groups is not None else preset.model_groups
        selected_adapter = adapter or preset.adapter
        return WorkflowSelection(
            name=name,
            path=(path.resolve() if path is not None else preset.path),
            adapter=selected_adapter,
            model_groups=selected_groups,
            models=self.models_for_groups(selected_groups),
            defaults=(
                dict(preset.defaults) if selected_adapter == preset.adapter else {}
            ),
        )
