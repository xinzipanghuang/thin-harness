"""Tiny .env loader (stdlib only, no python-dotenv dependency)."""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: str | None = None, *, override: bool = False) -> None:
    """Load ``KEY=VALUE`` pairs from a .env file into ``os.environ``.

    By default existing environment variables win (``override=False``), the
    conventional dotenv behavior. ``override=True`` makes the file
    authoritative (used by tests).
    """
    env_path = _resolve_env_path(path)
    if env_path is None:
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def get_env(name: str) -> str | None:
    """Read an environment variable, loading the project .env first."""
    load_dotenv()
    return os.environ.get(name)


def _resolve_env_path(path: str | None) -> Path | None:
    if path:
        candidate = Path(path)
        return candidate if candidate.is_file() else None
    candidates = [Path.cwd() / ".env", _PROJECT_ROOT / ".env"]
    return next((candidate for candidate in candidates if candidate.is_file()), None)

