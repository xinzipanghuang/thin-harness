"""Shared text-file helpers for bioinformatics formats."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import TextIO

from core.tool import ToolContext


def resolve_path(ctx: ToolContext, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and ctx.workdir:
        path = Path(ctx.workdir) / path
    return path.resolve()


def open_text(path: Path) -> TextIO:
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")
