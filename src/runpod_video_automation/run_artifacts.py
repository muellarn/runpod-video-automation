from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from runpod_video_automation.s3_storage import NetworkVolumeStorage
from runpod_video_automation.workflow import collect_output_files


class RunArtifacts:
    def __init__(
        self,
        storage: NetworkVolumeStorage,
        run_id: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        _validate_run_id(run_id)
        self.storage = storage
        self.run_id = run_id
        self.prefix, self.input_prefix, self.output_prefix = self.volume_paths(run_id)
        self.request_key = f"{self.prefix}/request.json"
        self.success_key = f"{self.prefix}/_SUCCESS"
        self._sleep = sleep
        self._request_fingerprint: str | None = None

    @classmethod
    def create(
        cls,
        storage: NetworkVolumeStorage,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> RunArtifacts:
        return cls(storage, uuid.uuid4().hex, sleep=sleep)

    def comfy_args(self) -> tuple[str, ...]:
        return self.comfy_args_for(self.run_id)

    @staticmethod
    def volume_paths(run_id: str) -> tuple[str, str, str]:
        _validate_run_id(run_id)
        prefix = f".runpod-video/runs/{run_id}"
        return prefix, f"{prefix}/inputs", f"{prefix}/outputs"

    @classmethod
    def comfy_args_for(cls, run_id: str) -> tuple[str, ...]:
        _, input_prefix, output_prefix = cls.volume_paths(run_id)
        return (
            "--input-directory",
            f"/runpod-volume/{input_prefix}",
            "--output-directory",
            f"/runpod-volume/{output_prefix}",
        )

    def stage_inputs(
        self,
        inputs: Iterable[tuple[Path, str | None]],
        workflow: dict[str, Any],
    ) -> list[dict[str, object]]:
        if self.storage.object_size(self.request_key) is not None:
            raise RuntimeError(f"Run artifact prefix already exists: {self.run_id}")
        records: list[dict[str, object]] = []
        names: set[str] = set()
        for source, requested_name in inputs:
            name = _safe_relative_name(requested_name or source.name, "input")
            if name in names:
                raise ValueError(f"Duplicate staged input name: {name}")
            names.add(name)
            if source.stat().st_size <= 0:
                raise ValueError(f"Input file is empty: {source}")
            key = f"{self.input_prefix}/{name}"
            size, sha256 = self.storage.upload_file(key, source)
            if self.storage.object_size(key) != size:
                raise RuntimeError(f"Staged input size mismatch: {name}")
            records.append({"name": name, "key": key, "size": size, "sha256": sha256})
        workflow_sha256 = hashlib.sha256(
            json.dumps(workflow, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        request: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "workflow_sha256": workflow_sha256,
            "inputs": records,
        }
        self._request_fingerprint = hashlib.sha256(
            json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        request["fingerprint"] = self._request_fingerprint
        self.storage.put_json(self.request_key, request)
        return records

    def publish_outputs(
        self,
        history: dict[str, Any],
        output_dir: Path,
        *,
        prompt_id: str,
    ) -> list[Path]:
        if self._request_fingerprint is None:
            raise RuntimeError("Run inputs have not been staged")
        items = collect_output_files(history)
        if not items:
            raise RuntimeError("The workflow completed without downloadable outputs")
        output_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, object]] = []
        written: list[Path] = []
        keys: set[str] = set()
        for item in items:
            if item["type"] != "output":
                raise RuntimeError(f"Unsupported ComfyUI output type: {item['type']!r}")
            filename = _safe_relative_name(item["filename"], "output filename")
            subfolder = _safe_relative_name(item["subfolder"], "output subfolder", empty=True)
            relative = str(PurePosixPath(subfolder) / filename) if subfolder else filename
            key = f"{self.output_prefix}/{relative}"
            if key in keys:
                raise RuntimeError(f"Duplicate ComfyUI output path: {relative}")
            keys.add(key)
            remote_size = self._wait_for_object(key)
            target = _available_target(output_dir, Path(filename).name)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
            try:
                size, sha256 = self.storage.download_file(key, temporary)
                if size != remote_size or size <= 0:
                    raise RuntimeError(f"Output size mismatch: {relative}")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            written.append(target)
            records.append(
                {
                    "key": key,
                    "size": size,
                    "sha256": sha256,
                    "local_name": target.name,
                }
            )
        self.storage.put_json(
            self.success_key,
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "request_fingerprint": self._request_fingerprint,
                "prompt_id": prompt_id,
                "completed_at": datetime.now(UTC).isoformat(),
                "outputs": records,
            },
        )
        return written

    def _wait_for_object(self, key: str) -> int:
        for attempt in range(7):
            size = self.storage.object_size(key)
            if size is not None and size > 0:
                return size
            if attempt < 6:
                self._sleep(min(2**attempt, 10))
        raise RuntimeError(f"ComfyUI output is not visible on Network Volume: {key}")


def _safe_relative_name(value: str, label: str, *, empty: bool = False) -> str:
    if not value:
        if empty:
            return ""
        raise ValueError(f"Empty {label}")
    if "\\" in value:
        raise ValueError(f"Invalid {label}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Invalid {label}: {value!r}")
    return str(path)


def _validate_run_id(run_id: str) -> None:
    if not run_id or not run_id.isascii() or not run_id.replace("-", "").isalnum():
        raise ValueError("Invalid run ID")


def _available_target(directory: Path, name: str) -> Path:
    target = directory / name
    if target.exists():
        target = target.with_name(f"{target.stem}-{uuid.uuid4().hex[:8]}{target.suffix}")
    return target
