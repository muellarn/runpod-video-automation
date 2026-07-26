import hashlib
from pathlib import Path
from typing import Any

import pytest

from runpod_video_automation.run_artifacts import RunArtifacts


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.json: dict[str, dict[str, Any]] = {}
        self.operations: list[str] = []

    def object_size(self, key: str) -> int | None:
        value = self.objects.get(key)
        return len(value) if value is not None else None

    def upload_file(self, key: str, source: Path) -> tuple[int, str]:
        value = source.read_bytes()
        self.objects[key] = value
        self.operations.append(f"upload:{key}")
        return len(value), hashlib.sha256(value).hexdigest()

    def download_file(self, key: str, target: Path) -> tuple[int, str]:
        value = self.objects[key]
        target.write_bytes(value)
        self.operations.append(f"download:{key}")
        return len(value), hashlib.sha256(value).hexdigest()

    def put_json(self, key: str, value: dict[str, Any]) -> None:
        self.json[key] = value
        self.operations.append(f"json:{key}")


def _history(filename: str = "result.webm", subfolder: str = "video") -> dict[str, Any]:
    return {
        "outputs": {
            "1": {
                "videos": [
                    {
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": "output",
                    }
                ]
            }
        }
    }


def test_run_artifacts_stage_and_publish_success_marker_last(tmp_path: Path) -> None:
    source = tmp_path / "start.png"
    source.write_bytes(b"input")
    storage = FakeStorage()
    artifacts = RunArtifacts(storage, "run-123")

    records = artifacts.stage_inputs([(source, "input.png")], {"1": {}})
    output_key = f"{artifacts.output_prefix}/video/result.webm"
    storage.objects[output_key] = b"rendered"
    outputs = artifacts.publish_outputs(
        _history(), tmp_path / "output", prompt_id="prompt-1"
    )

    assert records[0]["sha256"] == hashlib.sha256(b"input").hexdigest()
    assert outputs[0].read_bytes() == b"rendered"
    assert storage.operations[-1] == f"json:{artifacts.success_key}"
    marker = storage.json[artifacts.success_key]
    assert marker["prompt_id"] == "prompt-1"
    assert marker["outputs"] == [
        {
            "key": output_key,
            "size": 8,
            "sha256": hashlib.sha256(b"rendered").hexdigest(),
            "local_name": "result.webm",
        }
    ]
    assert artifacts.comfy_args() == (
        "--input-directory",
        "/runpod-volume/.runpod-video/runs/run-123/inputs",
        "--output-directory",
        "/runpod-volume/.runpod-video/runs/run-123/outputs",
    )


def test_run_artifacts_reject_output_traversal_without_success_marker(
    tmp_path: Path,
) -> None:
    storage = FakeStorage()
    artifacts = RunArtifacts(storage, "safe-run")
    artifacts.stage_inputs([], {"1": {}})

    with pytest.raises(ValueError, match="Invalid output filename"):
        artifacts.publish_outputs(
            _history("../secret", ""), tmp_path / "output", prompt_id="prompt-1"
        )

    assert artifacts.success_key not in storage.json


def test_run_artifacts_do_not_commit_a_missing_output(tmp_path: Path) -> None:
    storage = FakeStorage()
    sleeps: list[float] = []
    artifacts = RunArtifacts(storage, "missing-run", sleep=sleeps.append)
    artifacts.stage_inputs([], {"1": {}})

    with pytest.raises(RuntimeError, match="not visible"):
        artifacts.publish_outputs(
            _history(), tmp_path / "output", prompt_id="prompt-1"
        )

    assert sleeps == [1, 2, 4, 8, 10, 10]
    assert artifacts.success_key not in storage.json


def test_run_artifacts_reject_duplicate_and_empty_inputs(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    empty = tmp_path / "empty.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    empty.write_bytes(b"")

    with pytest.raises(ValueError, match="Duplicate staged input"):
        RunArtifacts(FakeStorage(), "duplicate-run").stage_inputs(
            [(first, "input.png"), (second, "input.png")], {"1": {}}
        )
    with pytest.raises(ValueError, match="empty"):
        RunArtifacts(FakeStorage(), "empty-run").stage_inputs(
            [(empty, None)], {"1": {}}
        )
