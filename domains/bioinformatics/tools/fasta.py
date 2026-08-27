"""Bounded FASTA pilot inspection."""

from __future__ import annotations

import json

from core.tool import ToolContext, ToolResult, clamp_int, tool

from ._io import BoundedTextReader, clamp_bytes, resolve_path, safe_text, write_report


@tool(name="bio.fasta.inspect", cacheable=True)
def inspect_fasta(
    ctx: ToolContext,
    path: str,
    max_records: int = 1000,
    max_uncompressed_bytes: int = 4 * 1024 * 1024,
) -> ToolResult:
    """Pilot-scan a bounded FASTA prefix and save the report as JSON.

    Call ``bio.file.peep`` first. Both record count and decompressed bytes are
    hard limits; this tool never performs an implicit full-file scan.

    Args:
        path: FASTA or gzip-compressed FASTA file.
        max_records: Maximum complete records to scan (1-100000).
        max_uncompressed_bytes: Maximum decompressed bytes to scan (1 KiB-64 MiB).
    """
    source = resolve_path(ctx, path)
    record_limit = clamp_int(max_records, 1, 100_000, 1000)
    byte_limit = clamp_bytes(max_uncompressed_bytes)
    identifiers: list[str] = []
    records = total = gc = ambiguous = 0
    min_length: int | None = None
    max_length = 0
    current_length = current_gc = current_ambiguous = 0
    in_record = False
    record_limited = False

    def commit_record() -> None:
        nonlocal records, total, gc, ambiguous, min_length, max_length
        records += 1
        total += current_length
        gc += current_gc
        ambiguous += current_ambiguous
        min_length = current_length if min_length is None else min(min_length, current_length)
        max_length = max(max_length, current_length)

    with BoundedTextReader(source, max_bytes=byte_limit) as reader:
        for raw in reader:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(">"):
                if in_record:
                    commit_record()
                    if records >= record_limit:
                        record_limited = True
                        break
                in_record = True
                current_length = current_gc = current_ambiguous = 0
                if len(identifiers) < 20:
                    identifier = line[1:].split()[0] if line[1:].strip() else ""
                    identifiers.append(safe_text(identifier, 160))
                continue
            if not in_record:
                raise ValueError("Malformed FASTA: sequence data appears before the first header")
            sequence = line.upper()
            current_length += len(sequence)
            current_gc += sequence.count("G") + sequence.count("C")
            current_ambiguous += sum(base not in "ACGT" for base in sequence)
        if in_record and not reader.truncated and not record_limited and records < record_limit:
            commit_record()
        bytes_scanned = reader.bytes_read
        byte_limited = reader.truncated
        line_too_long = reader.line_too_long

    truncated = record_limited or byte_limited
    report = {
        "path": str(source),
        "scan_mode": "bounded_prefix_pilot",
        "records_scanned": records,
        "bytes_scanned": bytes_scanned,
        "limits": {"max_records": record_limit, "max_uncompressed_bytes": byte_limit},
        "total_bases": total,
        "min_length": min_length or 0,
        "max_length": max_length,
        "mean_length": round(total / records, 2) if records else 0,
        "gc_fraction": round(gc / total, 6) if total else 0,
        "ambiguous_bases": ambiguous,
        "first_identifiers": identifiers,
        "truncated": truncated,
        "truncation_reason": (
            "line_too_long" if line_too_long else
            "byte_limit" if byte_limited else
            "record_limit" if record_limited else None
        ),
    }
    report_path = write_report(ctx, "fasta", report)
    compact = {
        key: value for key, value in report.items()
        if key not in {"first_identifiers"}
    }
    compact["first_identifiers"] = identifiers[:5]
    compact["report_path"] = str(report_path)
    return ToolResult(
        ok=True,
        summary=f"FASTA pilot: {records} complete record(s), {bytes_scanned} byte(s) scanned",
        data=compact,
        preview=json.dumps(compact, ensure_ascii=False, default=str),
        truncated=truncated,
        provenance={
            "source": str(source), "format": "FASTA", "scan_mode": "bounded_prefix_pilot",
            "report": str(report_path), "bytes_scanned": bytes_scanned,
        },
    )
