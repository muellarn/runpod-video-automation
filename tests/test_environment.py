import os
from pathlib import Path

import pytest

from runpod_video_automation.environment import load_environment


def test_environment_file_loads_without_overriding_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "RUNPOD_API_KEY=from-file\nRUNPOD_S3_ACCESS_KEY_ID='access'\n"
    )
    monkeypatch.setenv("RUNPOD_API_KEY", "from-process")
    monkeypatch.delenv("RUNPOD_S3_ACCESS_KEY_ID", raising=False)

    loaded = load_environment(tmp_path)

    assert loaded == tmp_path / ".env"
    assert os.environ["RUNPOD_API_KEY"] == "from-process"
    assert os.environ["RUNPOD_S3_ACCESS_KEY_ID"] == "access"


def test_environment_file_can_be_shared_between_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared.env"
    shared.write_text("RUNPOD_API_KEY=shared\n")
    monkeypatch.setenv("RUNPOD_VIDEO_ENV_FILE", str(shared))
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    assert load_environment(tmp_path / "other") == shared
    assert os.environ["RUNPOD_API_KEY"] == "shared"
