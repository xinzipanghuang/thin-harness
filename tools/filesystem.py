"""Filesystem tools: read, write, list, find."""

import fnmatch
import os
from pathlib import Path

from core.tool import ToolContext, ToolResult, clamp_int, tool

MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_WRITE_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_READ_CHARS = 100_000
MAX_GREP_BYTES = 2 * 1024 * 1024  # skip files larger than 2 MB


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() and ctx.workdir:
        p = Path(ctx.workdir) / p
    return p


@tool(cacheable=True)
def read(
    ctx: ToolContext,
    path: str,
    offset: int = 0,
    max_chars: int = DEFAULT_READ_CHARS,
) -> ToolResult:
    """Read a text file window starting at a character offset.

    Only a window is returned (default 100000 chars). The result records the
    exact window: offset, length, total chars, has_more, and a next_offset —
    call read again with offset=next_offset to continue reading a large file
    in order instead of reloading from the start. Files larger than 10 MB are
    refused; oversized results are saved as artifacts automatically.

    Args:
        path: File path (absolute, or relative to the run workdir).
        offset: Character offset to start reading from (use next_offset to continue).
        max_chars: Maximum number of characters to return.
    """
    p = _resolve(ctx, path)
    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"Refusing to read {p}: {size} bytes exceeds the {MAX_READ_BYTES} byte limit")
    text = p.read_text(encoding="utf-8", errors="replace")
    offset = max(0, int(offset or 0))
    window_size = clamp_int(max_chars, 1, MAX_READ_BYTES, DEFAULT_READ_CHARS)
    window = text[offset : offset + window_size]
    next_offset = offset + len(window)
    has_more = next_offset < len(text)
    return ToolResult(
        ok=True,
        summary=(
            f"Read {p.name} offset {offset}..{next_offset} ({len(window)} chars, "
            f"{len(text)} total, {'more remain' if has_more else 'done'})"
        ),
        data={
            "path": str(p),
            "offset": offset,
            "length": len(window),
            "text": window,
            "total_chars": len(text),
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
        },
    )


@tool(serial=True)
def write(ctx: ToolContext, path: str, content: str) -> str:
    """Write text content to a file, creating parent directories as needed.

    Args:
        path: File path (absolute, or relative to the run workdir).
        content: Text to write.
    """
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ValueError(f"Refusing to write: content exceeds the {MAX_WRITE_BYTES} byte limit")
    p = _resolve(ctx, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {p}"


@tool(name="filesystem.list", cacheable=True)
def list_dir(ctx: ToolContext, path: str = ".") -> list[dict]:
    """List directory entries with name, type, and size.

    Args:
        path: Directory path (absolute, or relative to the run workdir).
    """
    p = _resolve(ctx, path)
    if not p.is_dir():
        raise ValueError(f"Not a directory: {p}")
    entries = []
    for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
        try:
            size = child.stat().st_size if child.is_file() else None
        except OSError:
            size = None
        entries.append(
            {"name": child.name, "type": "dir" if child.is_dir() else "file", "size": size}
        )
    return entries


@tool(cacheable=True)
def find(ctx: ToolContext, root: str = ".", pattern: str = "*", max_results: int = 100) -> list[str]:
    """Recursively find paths under root whose name matches a glob pattern.

    Args:
        root: Directory to search (absolute, or relative to the run workdir).
        pattern: Glob pattern matched against entry names (e.g. *.py).
        max_results: Stop after this many matches.
    """
    base = _resolve(ctx, root)
    if not base.is_dir():
        raise ValueError(f"Not a directory: {base}")
    limit = clamp_int(max_results, 1, 1000, 100)
    matches: list[str] = []
    for current, dirs, files in os.walk(base):
        dirs.sort()
        files.sort()
        for name in files:
            if fnmatch.fnmatch(name, pattern):
                matches.append(str(Path(current) / name))
                if len(matches) >= limit:
                    return matches
        for name in dirs:
            if fnmatch.fnmatch(name, pattern):
                matches.append(str(Path(current) / name))
                if len(matches) >= limit:
                    return matches
    return matches


@tool(cacheable=True)
def grep(ctx: ToolContext, root: str = ".", pattern: str = "", max_results: int = 50) -> list[dict]:
    """Search file contents under a directory (recursively).

    Returns matching files with line numbers and the matching line text, so
    you can locate where something is mentioned before reading it. Use this
    instead of reading files blindly.

    Args:
        root: Directory to search (absolute, or relative to the run workdir).
        pattern: Case-insensitive substring to find in file contents.
        max_results: Stop after this many matches.
    """
    if not pattern.strip():
        raise ValueError("pattern is required")
    base = _resolve(ctx, root)
    if not base.is_dir():
        raise ValueError(f"Not a directory: {base}")
    limit = clamp_int(max_results, 1, 500, 50)
    matches: list[dict] = []
    needle = pattern.lower()
    for current, dirs, files in os.walk(base):
        dirs.sort()
        files.sort()
        for name in files:
            path = Path(current) / name
            try:
                if path.stat().st_size > MAX_GREP_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    matches.append(
                        {"path": str(path), "line": line_number, "text": line[:200]}
                    )
                    if len(matches) >= limit:
                        return matches
    return matches
