from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable

import httpx

from runpod_video_automation.config import ModelFile
from runpod_video_automation.s3_storage import NetworkVolumeStorage


PART_SIZE = 64 * 1024 * 1024


class ModelCacheIncomplete(RuntimeError):
    """The persistent model cache is not ready for a GPU worker."""


@dataclass(frozen=True)
class CachePlan:
    fingerprint: str
    marker_key: str
    models: tuple[ModelFile, ...]
    total_bytes: int


def build_cache_plan(models: Iterable[ModelFile]) -> CachePlan:
    unique: dict[str, ModelFile] = {}
    for model in models:
        if model.size is None or model.sha256 is None:
            raise ValueError(
                f"Model {model.path!r} requires pinned size and SHA-256 for S3 prewarm"
            )
        existing = unique.get(model.path)
        if existing is not None and existing != model:
            raise ValueError(f"Conflicting model definitions for {model.path!r}")
        unique[model.path] = model
    selected = tuple(unique[path] for path in sorted(unique))
    payload = [_model_record(model) for model in selected]
    fingerprint = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return CachePlan(
        fingerprint=fingerprint,
        marker_key=f".runpod-video/model-cache/{fingerprint}/COMPLETE.json",
        models=selected,
        total_bytes=sum(model.size or 0 for model in selected),
    )


class ModelCache:
    def __init__(
        self,
        storage: NetworkVolumeStorage,
        *,
        source_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_parallel_uploads: int = 3,
    ) -> None:
        if max_parallel_uploads < 1:
            raise ValueError("max_parallel_uploads must be positive")
        self.storage = storage
        self._source_client = source_client or httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=30, read=180, write=180, pool=30),
        )
        self._owns_source_client = source_client is None
        self._sleep = sleep
        self._max_parallel_uploads = max_parallel_uploads

    def close(self) -> None:
        if self._owns_source_client:
            self._source_client.close()

    def __enter__(self) -> ModelCache:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def status(self, models: Iterable[ModelFile]) -> tuple[CachePlan, bool]:
        plan = build_cache_plan(models)
        marker = self.storage.get_json(plan.marker_key)
        complete = marker == _complete_marker(plan) and all(
            self.storage.object_size(model.path) == model.size
            for model in plan.models
        )
        return plan, complete

    def require_complete(self, models: Iterable[ModelFile]) -> CachePlan:
        plan, complete = self.status(models)
        if not complete:
            raise ModelCacheIncomplete(
                "Model cache is incomplete; run 'runpod-video setup --apply' "
                f"before creating a GPU Pod (cache {plan.fingerprint[:12]})"
            )
        return plan

    def prewarm(self, models: Iterable[ModelFile]) -> CachePlan:
        plan, complete = self.status(models)
        if complete:
            return plan
        pending: list[tuple[int, ModelFile]] = []
        for index, model in enumerate(plan.models, start=1):
            print(
                f"Prewarm {index}/{len(plan.models)}: {model.path} "
                f"({model.size / 1024**3:.2f} GiB)",
                flush=True,
            )
            if self._object_is_verified(model):
                print("  cached", flush=True)
                continue
            pending.append((index, model))
        workers = min(self._max_parallel_uploads, len(pending))
        if workers:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures: dict[Future[None], ModelFile] = {
                    executor.submit(
                        self._prewarm_model, index, len(plan.models), model
                    ): model
                    for index, model in pending
                }
                try:
                    for future in as_completed(futures):
                        future.result()
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise
        self.storage.put_json(plan.marker_key, _complete_marker(plan))
        return plan

    def _prewarm_model(self, index: int, total: int, model: ModelFile) -> None:
        print(f"  start {index}/{total}: {model.path}", flush=True)
        self._upload_model(model)
        self.storage.put_json(_object_marker_key(model), _model_record(model))
        print(f"  ready {index}/{total}: {model.path}", flush=True)

    def _object_is_verified(self, model: ModelFile) -> bool:
        marker = self.storage.get_json(_object_marker_key(model))
        marked = (
            marker == _model_record(model)
            and self.storage.object_size(model.path) == model.size
        )
        if marked:
            return True
        if self.storage.object_size(model.path) != model.size:
            return False
        assert model.sha256 is not None
        if self.storage.object_sha256(model.path) != model.sha256:
            return False
        self.storage.put_json(_object_marker_key(model), _model_record(model))
        return True

    def _upload_model(self, model: ModelFile) -> None:
        assert model.size is not None
        assert model.sha256 is not None
        upload_id = self.storage.create_multipart_upload(model.path)
        parts: list[dict[str, object]] = []
        digest = hashlib.sha256()
        try:
            for number, start in enumerate(range(0, model.size, PART_SIZE), start=1):
                end = min(start + PART_SIZE, model.size) - 1
                body = self._download_range(model.url, start, end, model.size)
                digest.update(body)
                etag = self.storage.upload_part(model.path, upload_id, number, body)
                parts.append({"ETag": etag, "PartNumber": number})
                print(
                    f"  {model.path}: "
                    f"{min(end + 1, model.size) * 100 // model.size}%",
                    flush=True,
                )
            if digest.hexdigest() != model.sha256:
                raise RuntimeError(f"SHA-256 mismatch for {model.path}")
            self.storage.complete_multipart_upload(model.path, upload_id, parts)
        except Exception:
            self.storage.abort_multipart_upload(model.path, upload_id)
            raise
        for attempt in range(6):
            if self.storage.object_size(model.path) == model.size:
                break
            if attempt < 5:
                self._sleep(2**attempt)
        else:
            raise RuntimeError(f"Remote size mismatch for {model.path}")

    def _download_range(
        self, url: str, start: int, end: int, total_size: int
    ) -> bytes:
        expected = end - start + 1
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                headers = {"Range": f"bytes={start}-{end}"}
                token = os.environ.get("HF_TOKEN")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                with self._source_client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    if response.status_code == 200 and not (
                        start == 0 and end + 1 == total_size
                    ):
                        raise RuntimeError("Source server ignored the Range request")
                    body = b"".join(response.iter_bytes())
                if len(body) != expected:
                    if start == 0 and end + 1 == total_size and len(body) == total_size:
                        return body
                    raise RuntimeError(
                        f"Source returned {len(body)} bytes, expected {expected}"
                    )
                return body
            except Exception as error:
                last_error = error
                if attempt < 4:
                    self._sleep(2**attempt)
        raise RuntimeError(
            f"Failed to download source range {start}-{end}: {last_error}"
        ) from None


def _model_record(model: ModelFile) -> dict[str, object]:
    return {
        "path": model.path,
        "url": model.url,
        "size": model.size,
        "sha256": model.sha256,
    }


def _object_marker_key(model: ModelFile) -> str:
    assert model.sha256 is not None
    return f".runpod-video/model-objects/{model.sha256}.json"


def _complete_marker(plan: CachePlan) -> dict[str, object]:
    return {
        "schema_version": 1,
        "fingerprint": plan.fingerprint,
        "total_bytes": plan.total_bytes,
        "models": [_model_record(model) for model in plan.models],
    }
