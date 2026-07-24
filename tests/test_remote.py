import subprocess
from pathlib import Path

import pytest

from runpod_video_automation.config import ModelFile
from runpod_video_automation.remote import RemoteWorker


def test_wait_for_ssh_reports_last_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("runpod_video_automation.remote.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("runpod_video_automation.remote.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "runpod_video_automation.remote.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args, returncode=255, stdout="", stderr="Connection refused"
        ),
    )
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))

    with pytest.raises(TimeoutError, match="Connection refused"):
        worker.wait_for_ssh(timeout_seconds=1)


def test_ensure_models_uses_parallel_segmented_resumable_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int | None]] = []
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))
    monkeypatch.setattr(
        worker,
        "run",
        lambda command, timeout=None: calls.append((command, timeout)),
    )
    models = (
        ModelFile("https://example.test/one", "models/unet/one.safetensors", 100),
        ModelFile("https://example.test/two", "models/vae/two.safetensors", 200),
    )

    worker.ensure_models(models)

    assert len(calls) == 2
    download_command, timeout = calls[1]
    assert "apt-get install -y -qq aria2" in download_command
    assert download_command.count("timeout --signal=INT 600 aria2c") == 2
    assert download_command.count("--max-connection-per-server=4") == 2
    assert download_command.count("--continue=true") == 2
    assert "pids=\"\"" in download_command
    assert "for pid in $pids" in download_command
    assert timeout == 4 * 60 * 60


def test_ensure_models_verifies_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int | None]] = []
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))
    monkeypatch.setattr(
        worker,
        "run",
        lambda command, timeout=None: calls.append((command, timeout)),
    )
    checksum = "a" * 64

    worker.ensure_models(
        (
            ModelFile(
                "https://example.test/model",
                "models/unet/model.safetensors",
                100,
                checksum,
            ),
        )
    )

    download_command, _ = calls[1]
    assert download_command.count("sha256sum") == 2
    assert checksum in download_command


def test_ensure_models_exposes_modern_model_directories_to_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int | None]] = []
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))
    monkeypatch.setattr(
        worker,
        "run",
        lambda command, timeout=None: calls.append((command, timeout)),
    )

    worker.ensure_models(
        (
            ModelFile(
                "https://example.test/unet",
                "models/diffusion_models/model.safetensors",
                100,
            ),
            ModelFile(
                "https://example.test/clip",
                "models/text_encoders/encoder.safetensors",
                200,
            ),
        )
    )

    download_command, _ = calls[1]
    assert "mkdir -p /runpod-volume/models/unet /runpod-volume/models/clip" in download_command
    assert (
        "ln -sfn /runpod-volume/models/diffusion_models/model.safetensors "
        "/runpod-volume/models/unet/model.safetensors"
    ) in download_command
    assert (
        "ln -sfn /runpod-volume/models/text_encoders/encoder.safetensors "
        "/runpod-volume/models/clip/encoder.safetensors"
    ) in download_command
