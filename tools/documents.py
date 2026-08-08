"""Document tools: discovery and position-aware window reading.

Documents are read in small character windows (default 200) so the model
context stays small. Every read records the exact window (offset + length,
unit) and returns a ``next_offset`` so the agent can continue reading in
order. Supported formats: PDF, DOCX, and common text files (txt/md/csv/json/
yaml/py/...).
"""

import fnmatch
import os
from pathlib import Path

from core.tool import ToolContext, clamp_int, tool
from core.types import ToolResult

DEFAULT_WINDOW_CHARS = 200
MAX_WINDOW_CHARS = 6000
MAX_DOC_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_TEXT_BYTES = 10 * 1024 * 1024  # 10 MB for plain text

_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".rst",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".log",
        ".html",
        ".xml",
        ".py",
        ".js",
        ".ts",
        ".sh",
        ".bat",
        ".ps1",
        ".sql",
    }
)
SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", *_TEXT_EXTENSIONS})

_PDF_CACHE: dict[str, tuple[int, list[str]]] = {}  # path -> (mtime_ns, pages)
_TEXT_CACHE: dict[str, tuple[int, tuple[list[str], str]]] = {}  # path -> (mtime_ns, (parts, sep))


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() and ctx.workdir:
        p = Path(ctx.workdir) / p
    return p


def _pdf_pages(path: Path) -> list[str]:
    """Extract per-page text from a PDF, cached by path + mtime."""
    stat = path.stat()
    key = str(path)
    cached = _PDF_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns:
        return cached[1]
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise ValueError("pypdf is required to read PDFs: pip install pypdf") from exc
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise ValueError(f"Not a readable PDF: {path} ({exc})") from exc
    _PDF_CACHE[key] = (stat.st_mtime_ns, pages)
    return pages


def _docx_paragraphs(path: Path) -> list[str]:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover
        raise ValueError("python-docx is required to read DOCX files: pip install python-docx") from exc
    try:
        document = Document(str(path))
        return [paragraph.text for paragraph in document.paragraphs]
    except Exception as exc:
        raise ValueError(f"Not a readable DOCX: {path} ({exc})") from exc


def _document_parts(path: Path) -> tuple[list[str], str]:
    """Return (parts, separator) for a document: pages / paragraphs / lines."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdf_pages(path), "\n\n"
    if ext == ".docx":
        return _docx_paragraphs(path), "\n"
    if ext in _TEXT_EXTENSIONS:
        stat = path.stat()
        key = str(path)
        cached = _TEXT_CACHE.get(key)
        if cached is not None and cached[0] == stat.st_mtime_ns:
            return cached[1]
        if stat.st_size > MAX_TEXT_BYTES:
            raise ValueError(f"Refusing to read {path}: exceeds the {MAX_TEXT_BYTES} byte limit")
        text = path.read_text(encoding="utf-8", errors="replace")
        parts = text.splitlines() or [""]
        _TEXT_CACHE[key] = (stat.st_mtime_ns, (parts, "\n"))
        return parts, "\n"
    supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
    raise ValueError(f"Unsupported document type {ext!r} (supported: {supported})")


def _unit_at(parts: list[str], offset: int, separator_len: int) -> int | None:
    if not parts:
        return None
    pos = 0
    for index, part in enumerate(parts):
        if offset < pos + len(part):
            return index + 1
        pos += len(part) + separator_len
    return len(parts)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping [start, end) character ranges."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted((a, b) for a, b in ranges if b > a):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _record_read(ctx: ToolContext, path: Path, offset: int, end: int) -> None:
    tracker = ctx.state.setdefault("read_progress", {})
    tracker.setdefault(str(path), []).append((offset, end))


@tool(name="documents.list", cacheable=True)
def list_documents(
    ctx: ToolContext,
    root: str = ".",
    pattern: str = "*",
    max_results: int = 50,
) -> list[dict]:
    """Find readable documents under a directory (recursively).

    Supported formats: pdf, docx, and common text files (txt/md/csv/json/
    yaml/py/...).

    Args:
        root: Directory to search (absolute, or relative to the run workdir).
        pattern: Glob pattern for document names (default *).
        max_results: Stop after this many matches.
    """
    base = _resolve(ctx, root)
    if not base.is_dir():
        raise ValueError(f"Not a directory: {base}")
    limit = clamp_int(max_results, 1, 1000, 50)
    matches: list[dict] = []
    for current, dirs, files in os.walk(base):
        dirs.sort()
        files.sort()
        for name in files:
            ext = Path(name).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS or not fnmatch.fnmatch(name, pattern):
                continue
            path = Path(current) / name
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            matches.append({"name": name, "path": str(path), "size": size, "ext": ext.lstrip(".")})
            if len(matches) >= limit:
                return matches
    return matches


@tool(cacheable=True)
def read(
    ctx: ToolContext,
    path: str,
    offset: int = 0,
    max_chars: int = DEFAULT_WINDOW_CHARS,
) -> ToolResult:
    """Read a document window starting at a character offset.

    Supports PDF, DOCX, and common text files (txt/md/csv/json/yaml/py/...).
    Only a small window is read (default 200 chars) so the model context stays
    small. The result records the exact window: offset, length, unit (page /
    paragraph / line), and a next_offset — call read again with
    offset=next_offset to continue reading the document in order.

    Args:
        path: Document path (absolute, or relative to the run workdir).
        offset: Character offset to start reading from (use next_offset to continue).
        max_chars: Window size in characters (1..6000).
    """
    p = _resolve(ctx, path)
    if not p.is_file():
        raise ValueError(f"No such file: {p}")
    if p.stat().st_size > MAX_DOC_BYTES:
        raise ValueError(f"Refusing to read {p}: exceeds the {MAX_DOC_BYTES} byte limit")
    try:
        raw_requested = int(max_chars)
    except (TypeError, ValueError):
        raw_requested = DEFAULT_WINDOW_CHARS
    requested_chars = clamp_int(raw_requested, 1, MAX_WINDOW_CHARS, DEFAULT_WINDOW_CHARS)
    window_size = requested_chars
    offset = max(0, int(offset or 0))

    parts, sep = _document_parts(p)
    text = sep.join(parts)
    total = len(text)
    window = text[offset : offset + window_size]
    next_offset = offset + len(window)
    has_more = next_offset < total
    ext = p.suffix.lower()
    unit_type = "page" if ext == ".pdf" else ("paragraph" if ext == ".docx" else "line")
    unit = _unit_at(parts, offset, len(sep))
    _record_read(ctx, p, offset, next_offset)
    status = "more remain" if has_more else "done"
    return ToolResult(
        ok=True,
        summary=(
            f"Read {p.name} offset {offset}..{next_offset} ({len(window)} chars, "
            f"{unit_type} {unit}, {total} total, {status})"
        ),
    data={
        "path": str(p),
        "offset": offset,
        "length": len(window),
        "requested_chars": requested_chars,
        "returned_chars": len(window),
        "clamped": requested_chars != raw_requested,
        "unit_type": unit_type,
        "unit": unit,
        "text": window,
        "total_chars": total,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
    },
    )


@tool(cacheable=True)
def search(
    ctx: ToolContext,
    path: str,
    pattern: str,
    max_matches: int = 10,
) -> ToolResult:
    """Find lines matching a pattern in a document and return their offsets.

    Use this to locate specific content (e.g. services, image names, keywords)
    instead of reading a whole document sequentially. Each match reports its
    line number and character offset — read around a match with
    documents.read(path=..., offset=...).

    Args:
        path: Document path (absolute, or relative to the run workdir).
        pattern: Case-insensitive substring to search for.
        max_matches: Stop after this many matches (default 10).
    """
    p = _resolve(ctx, path)
    if not p.is_file():
        raise ValueError(f"No such file: {p}")
    parts, sep = _document_parts(p)
    text = sep.join(parts)
    lines = text.split("\n")
    needle = pattern.lower()
    limit = clamp_int(max_matches, 1, 500, 10)
    matches: list[dict] = []
    offset = 0
    for index, line in enumerate(lines, 1):
        if needle in line.lower():
            matches.append(
                {
                    "line": index,
                    "offset": offset,
                    "length": len(line),
                    "text": line[:200],
                }
            )
            if len(matches) >= limit:
                break
        offset += len(line) + 1
    return ToolResult(
        ok=True,
        summary=(
            f"Found {len(matches)} match(es) for {pattern!r} in {p.name} "
            f"({len(lines)} lines total)"
        ),
        data={
            "path": str(p),
            "pattern": pattern,
            "total_lines": len(lines),
            "matches": matches,
        },
    )


@tool(cacheable=True)
def progress(ctx: ToolContext, path: str = "") -> list[dict]:
    """Show which character windows have already been read in this run.

    Returns per-document covered ranges and total covered characters, so you
    never re-read a window you have already seen. Call this before reading
    again, or just continue with the next_offset returned by documents.read.

    Args:
        path: Optional document path to narrow the report.
    """
    tracker = ctx.state.get("read_progress", {})
    if path:
        key = str(_resolve(ctx, path))
        tracker = {key: tracker.get(key, [])}
    report = []
    for file_path, ranges in sorted(tracker.items()):
        merged = _merge_ranges(ranges)
        report.append(
            {
                "path": file_path,
                "ranges": [[start, end] for start, end in merged],
                "covered_chars": sum(end - start for start, end in merged),
            }
        )
    return report
