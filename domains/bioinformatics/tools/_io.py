"""Bounded I/O and file-backed reporting for bioinformatics tools."""

from __future__ import annotations

import gzip
import json
import os
import re
import uuid
from pathlib import Path
from typing import BinaryIO, Iterator

from core.tool import ToolContext


DEFAULT_SCAN_BYTES = 4 * 1024 * 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
DEFAULT_HEAD_BYTES = 16 * 1024
MAX_HEAD_BYTES = 64 * 1024


def resolve_path(ctx: ToolContext, value: str, *, require_file: bool = True) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and ctx.workdir:
        path = Path(ctx.workdir) / path
    path = path.resolve()
    if require_file and not path.is_file():
        raise ValueError(f"Not a regular file: {path}")
    return path


def output_root(ctx: ToolContext) -> Path:
    base = Path(ctx.workdir).resolve() if ctx.workdir else Path.cwd().resolve()
    root = base / "bio_outputs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.partial")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_report(ctx: ToolContext, kind: str, data: dict) -> Path:
    directory = output_root(ctx) / "inspections"
    path = directory / f"{kind}_{uuid.uuid4().hex[:12]}.json"
    atomic_write_json(path, data)
    return path.resolve()


def clamp_bytes(value: int, default: int = DEFAULT_SCAN_BYTES) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1024, min(MAX_SCAN_BYTES, number))


def is_gzip(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def open_binary(path: Path) -> BinaryIO:
    if is_gzip(path):
        return gzip.open(path, "rb")
    return path.open("rb")


class BoundedTextReader:
    """Line reader with hard decompressed-byte and per-line limits."""

    def __init__(
        self,
        path: Path,
        max_bytes: int = DEFAULT_SCAN_BYTES,
        max_line_bytes: int = MAX_LINE_BYTES,
    ) -> None:
        self.path = path
        self.max_bytes = clamp_bytes(max_bytes)
        self.max_line_bytes = max(1024, min(MAX_LINE_BYTES, int(max_line_bytes)))
        self.bytes_read = 0
        self.truncated = False
        self.line_too_long = False
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "BoundedTextReader":
        self._handle = open_binary(self.path)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is not None:
            self._handle.close()

    def readline(self) -> str:
        if self._handle is None:
            raise RuntimeError("BoundedTextReader must be used as a context manager")
        remaining = self.max_bytes - self.bytes_read
        if remaining <= 0:
            self.truncated = bool(self._handle.read(1))
            return ""
        cap = min(remaining, self.max_line_bytes)
        raw = self._handle.readline(cap + 1)
        if not raw:
            return ""
        if len(raw) > cap:
            self.truncated = True
            self.line_too_long = cap == self.max_line_bytes
            return ""
        self.bytes_read += len(raw)
        return raw.decode("utf-8", errors="replace")

    def __iter__(self) -> Iterator[str]:
        while True:
            line = self.readline()
            if not line:
                break
            yield line


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_text(value: str, max_chars: int = 240) -> str:
    """Make an untrusted file/log fragment compact and terminal-safe."""
    cleaned = _CONTROL_CHARS.sub("�", str(value)).replace("\r", "")
    cleaned = cleaned.replace("\n", "\\n")
    if len(cleaned) > max_chars:
        return cleaned[:max_chars] + "…"
    return cleaned


def bounded_head(path: Path, max_bytes: int = DEFAULT_HEAD_BYTES, max_lines: int = 12) -> dict:
    budget = max(1024, min(MAX_HEAD_BYTES, int(max_bytes)))
    lines: list[str] = []
    with BoundedTextReader(path, max_bytes=budget, max_line_bytes=budget) as reader:
        for raw in reader:
            lines.append(safe_text(raw.rstrip("\r\n")))
            if len(lines) >= max(1, min(50, int(max_lines))):
                reader.truncated = True
                break
        return {
            "lines": lines,
            "bytes_read": reader.bytes_read,
            "truncated": reader.truncated,
            "line_too_long": reader.line_too_long,
        }


def sidecar_indexes(path: Path) -> list[str]:
    candidates = [
        Path(str(path) + suffix)
        for suffix in (".fai", ".bai", ".csi", ".tbi", ".gzi")
    ]
    if path.suffix:
        candidates.extend(path.with_suffix(suffix) for suffix in (".fai", ".bai", ".csi", ".tbi"))
    return sorted({str(item.resolve()) for item in candidates if item.is_file()})
