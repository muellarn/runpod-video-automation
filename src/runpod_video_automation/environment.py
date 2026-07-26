from __future__ import annotations

import os
import re
from pathlib import Path


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_environment(project_root: Path) -> Path | None:
    """Load a local environment file without overriding exported secrets."""

    configured = os.environ.get("RUNPOD_VIDEO_ENV_FILE")
    path = Path(configured).expanduser() if configured else project_root / ".env"
    if not path.is_file():
        return None
    for number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid environment line {number} in {path}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _KEY.fullmatch(key):
            raise ValueError(f"Invalid environment key on line {number} in {path}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return path
