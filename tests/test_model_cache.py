import hashlib
from threading import Barrier
from typing import Any

import httpx
import pytest

from runpod_video_automation.config import ModelFile
from runpod_video_automation.model_cache import (
    ModelCache,
    ModelCacheIncomplete,
    build_cache_plan,
)


class FakeStorage:
    def __init__(self) -> None:
        self.json: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, list[bytes]] = {}
        self.aborted = False
        self.json_writes: list[str] = []

    def get_json(self, key: str):
        return self.json.get(key)

    def put_json(self, key: str, value: dict[str, Any]) -> None:
        self.json[key] = value
        self.json_writes.append(key)

    def object_size(self, key: str):
        value = self.objects.get(key)
        return len(value) if value is not None else None

    def object_sha256(self, key: str) -> str:
        return hashlib.sha256(self.objects[key]).hexdigest()

    def create_multipart_upload(self, key: str) -> str:
        self.uploads[key] = []
        return "upload-1"

    def upload_part(self, key: str, upload_id: str, number: int, body: bytes) -> str:
        assert upload_id == "upload-1"
        self.uploads[key].append(body)
        return f"etag-{number}"

    def complete_multipart_upload(self, key: str, upload_id: str, parts) -> None:
        self.objects[key] = b"".join(self.uploads[key])

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        self.aborted = True


def _model(
    data: bytes,
    *,
    checksum: str | None = None,
    path: str = "models/model.bin",
) -> ModelFile:
    return ModelFile(
        "https://example.test/model",
        path,
        len(data),
        checksum or hashlib.sha256(data).hexdigest(),
    )


def test_prewarm_streams_ranges_and_writes_complete_marker(monkeypatch) -> None:
    data = b"abcdefghij"
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        value = request.headers["Range"]
        ranges.append(value)
        start, end = (int(part) for part in value.removeprefix("bytes=").split("-"))
        return httpx.Response(
            206,
            content=data[start : end + 1],
            request=request,
        )

    monkeypatch.setattr("runpod_video_automation.model_cache.PART_SIZE", 4)
    storage = FakeStorage()
    cache = ModelCache(
        storage, source_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    plan = cache.prewarm((_model(data),))

    assert storage.objects["models/model.bin"] == data
    assert ranges == ["bytes=0-3", "bytes=4-7", "bytes=8-9"]
    assert storage.json[plan.marker_key]["fingerprint"] == plan.fingerprint
    assert cache.require_complete((_model(data),)) == plan


def test_cache_miss_is_reported_before_gpu_creation() -> None:
    cache = ModelCache(FakeStorage(), source_client=httpx.Client())

    with pytest.raises(ModelCacheIncomplete, match="setup --apply"):
        cache.require_complete((_model(b"data"),))


def test_checksum_mismatch_aborts_multipart_upload() -> None:
    data = b"wrong"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(206, content=data, request=request)

    storage = FakeStorage()
    cache = ModelCache(
        storage, source_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        cache.prewarm((_model(data, checksum="0" * 64),))

    assert storage.aborted is True
    assert storage.json == {}


def test_cache_plan_requires_pinned_integrity_metadata() -> None:
    with pytest.raises(ValueError, match="pinned size and SHA-256"):
        build_cache_plan((ModelFile("https://example.test/model", "models/model"),))


def test_prewarm_uploads_models_in_parallel_and_commits_marker_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _model(b"first", path="models/first.bin")
    second = _model(b"second", path="models/second.bin")
    data = {first.path: b"first", second.path: b"second"}
    barrier = Barrier(2)
    storage = FakeStorage()
    cache = ModelCache(
        storage,
        source_client=httpx.Client(),
        max_parallel_uploads=2,
    )

    def upload(model: ModelFile) -> None:
        barrier.wait(timeout=2)
        storage.objects[model.path] = data[model.path]

    monkeypatch.setattr(cache, "_upload_model", upload)

    plan = cache.prewarm((first, second))

    assert storage.json_writes[-1] == plan.marker_key
    assert cache.require_complete((first, second)) == plan


def test_prewarm_adopts_matching_unmarked_s3_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"already complete"
    model = _model(data)
    storage = FakeStorage()
    storage.objects[model.path] = data
    cache = ModelCache(storage, source_client=httpx.Client())
    monkeypatch.setattr(
        cache,
        "_upload_model",
        lambda selected: pytest.fail(f"Unexpected upload: {selected.path}"),
    )

    plan = cache.prewarm((model,))

    assert cache.require_complete((model,)) == plan
    assert storage.json_writes == [
        f".runpod-video/model-objects/{model.sha256}.json",
        plan.marker_key,
    ]


def test_require_complete_reconstructs_marker_for_verified_subset() -> None:
    first = _model(b"first", path="models/first.bin")
    second = _model(b"second", path="models/second.bin")
    storage = FakeStorage()
    storage.objects = {first.path: b"first", second.path: b"second"}
    for model in (first, second):
        storage.json[f".runpod-video/model-objects/{model.sha256}.json"] = {
            "path": model.path,
            "url": model.url,
            "size": model.size,
            "sha256": model.sha256,
        }
    cache = ModelCache(storage, source_client=httpx.Client())
    subset = build_cache_plan((first,))

    result = cache.require_complete((first,))

    assert result == subset
    assert storage.json_writes == [subset.marker_key]
