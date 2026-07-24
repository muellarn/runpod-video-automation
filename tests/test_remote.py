import subprocess
from pathlib import Path

import pytest

from runpod_video_automation.config import ModelFile, ModelPathAlias
from runpod_video_automation.prompt_refiner.config import PromptRefinerProfile
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
        ),
        (
            ModelPathAlias("models/diffusion_models", "models/unet"),
            ModelPathAlias("models/text_encoders", "models/clip"),
        ),
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
    assert "|| status=1; fi" in download_command


def test_ensure_system_packages_installs_only_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int | None]] = []
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))
    monkeypatch.setattr(
        worker,
        "run",
        lambda command, timeout=None: calls.append((command, timeout)),
    )

    worker.ensure_system_packages(("gcc", "python3-dev"))

    command, timeout = calls[0]
    assert "dpkg-query -W gcc python3-dev" in command
    assert "apt-get install -y gcc python3-dev" in command
    assert timeout == 20 * 60


def test_ensure_comfy_args_restarts_only_for_a_new_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int | None]] = []
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))
    monkeypatch.setattr(
        worker,
        "run",
        lambda command, timeout=None: calls.append((command, timeout)),
    )

    worker.ensure_comfy_args(
        ("--enable-triton-backend",),
        system_packages=("gcc", "python3-dev"),
    )

    command, timeout = calls[0]
    assert "runpod-video-comfy-args" in command
    assert "dpkg-query -W gcc python3-dev" in command
    assert "--enable-triton-backend" in command
    assert "kill -0" in command
    assert "nohup /opt/venv/bin/python" in command
    assert timeout == 20 * 60


def test_ensure_comfy_args_starts_comfy_without_extra_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))
    monkeypatch.setattr(
        worker,
        "run",
        lambda command, timeout=None: calls.append(command),
    )

    worker.ensure_comfy_args(())

    assert len(calls) == 1
    assert "nohup /opt/venv/bin/python -u /comfyui/main.py" in calls[0]


def test_stop_comfyui_process_pattern_does_not_match_its_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))
    monkeypatch.setattr(
        worker,
        "run",
        lambda command, timeout=None: calls.append(command),
    )

    worker.stop_comfyui()

    assert "/[c]omfyui/main.py" in calls[0]
    assert "pgrep -f '/comfyui/main.py'" not in calls[0]
    subprocess.run(["bash", "-n", "-c", calls[0]], check=True)


def test_koboldcpp_process_is_loopback_only_and_stops_after_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))
    system_prompt = tmp_path / "system.txt"
    system_prompt.write_text("Prompt")
    profile = PromptRefinerProfile(
        name="test",
        runtime=ModelFile(
            "https://example.test/runtime", "tools/koboldcpp", 1, "a" * 64
        ),
        model=ModelFile(
            "https://example.test/model", "models/model.gguf", 2, "b" * 64
        ),
        system_prompt_path=system_prompt,
        reference_document_path=None,
        port=5001,
        context_size=4096,
        max_tokens=1024,
        gpu_layers=65,
        seed=42,
        temperature=0.2,
        top_p=0.8,
        top_k=20,
    )
    events: list[str] = []
    monkeypatch.setattr(
        worker, "stop_koboldcpp", lambda: events.append("stop")
    )
    monkeypatch.setattr(
        worker,
        "run",
        lambda command, timeout=None: events.append(command),
    )

    with worker.koboldcpp_process(profile):
        events.append("yield")

    assert events[0] == "stop"
    assert "--host 127.0.0.1 --port 5001" in events[1]
    assert "--contextsize 4096" in events[1]
    assert "runpod-video-koboldcpp-runtime" in events[1]
    subprocess.run(["bash", "-n", "-c", events[1]], check=True)
    assert events[2:] == ["yield", "stop"]


def test_stop_koboldcpp_verifies_the_recorded_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    worker = RemoteWorker(host="example.test", port=22, ssh_key=Path(__file__))
    monkeypatch.setattr(
        worker,
        "run",
        lambda command, timeout=None: calls.append(command),
    )

    worker.stop_koboldcpp()

    assert "runpod-video-koboldcpp-runtime" in calls[0]
    assert 'grep -Fq -- "$runtime"' in calls[0]
    assert "koboldcpp-linux-x64" not in calls[0]
    subprocess.run(["bash", "-n", "-c", calls[0]], check=True)
