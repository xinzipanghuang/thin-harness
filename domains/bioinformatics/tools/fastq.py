"""Bounded FASTQ pilot inspection."""

from __future__ import annotations

import json

from core.tool import ToolContext, ToolResult, clamp_int, tool

from ._io import BoundedTextReader, clamp_bytes, resolve_path, write_report


@tool(name="bio.fastq.inspect", cacheable=True)
def inspect_fastq(
    ctx: ToolContext,
    path: str,
    max_reads: int = 1000,
    max_uncompressed_bytes: int = 4 * 1024 * 1024,
) -> ToolResult:
    """Pilot-scan a bounded FASTQ prefix and save the report as JSON.

    Args:
        path: FASTQ or gzip-compressed FASTQ file.
        max_reads: Maximum complete reads to scan (1-100000).
        max_uncompressed_bytes: Maximum decompressed bytes to scan (1 KiB-64 MiB).
    """
    source = resolve_path(ctx, path)
    read_limit = clamp_int(max_reads, 1, 100_000, 1000)
    byte_limit = clamp_bytes(max_uncompressed_bytes)
    reads = bases = gc = quality_sum = quality_bases = 0
    min_length: int | None = None
    max_length = 0
    record_limited = False

    with BoundedTextReader(source, max_bytes=byte_limit) as reader:
        while reads < read_limit:
            header = reader.readline()
            if not header:
                break
            sequence = reader.readline().rstrip("\r\n")
            plus = reader.readline()
            quality = reader.readline().rstrip("\r\n")
            if not sequence or not plus or not quality:
                if reader.truncated:
                    break
                raise ValueError(f"Malformed FASTQ record near read {reads + 1}")
            if not header.startswith("@") or not plus.startswith("+"):
                raise ValueError(f"Malformed FASTQ markers near read {reads + 1}")
            if len(sequence) != len(quality):
                raise ValueError(f"FASTQ sequence/quality length mismatch near read {reads + 1}")
            length = len(sequence)
            reads += 1
            bases += length
            upper = sequence.upper()
            gc += upper.count("G") + upper.count("C")
            quality_sum += sum(max(0, ord(char) - 33) for char in quality)
            quality_bases += len(quality)
            min_length = length if min_length is None else min(min_length, length)
            max_length = max(max_length, length)
        if reads >= read_limit:
            record_limited = bool(reader.readline()) or reader.truncated
        bytes_scanned = reader.bytes_read
        byte_limited = reader.truncated
        line_too_long = reader.line_too_long

    truncated = record_limited or byte_limited
    report = {
        "path": str(source),
        "scan_mode": "bounded_prefix_pilot",
        "reads_scanned": reads,
        "bytes_scanned": bytes_scanned,
        "limits": {"max_reads": read_limit, "max_uncompressed_bytes": byte_limit},
        "total_bases": bases,
        "min_length": min_length or 0,
        "max_length": max_length,
        "mean_length": round(bases / reads, 2) if reads else 0,
        "gc_fraction": round(gc / bases, 6) if bases else 0,
        "mean_phred33": round(quality_sum / quality_bases, 2) if quality_bases else 0,
        "truncated": truncated,
        "truncation_reason": (
            "line_too_long" if line_too_long else
            "byte_limit" if byte_limited else
            "record_limit" if record_limited else None
        ),
    }
    report_path = write_report(ctx, "fastq", report)
    compact = {**report, "report_path": str(report_path)}
    return ToolResult(
        ok=True,
        summary=f"FASTQ pilot: {reads} complete read(s), {bytes_scanned} byte(s) scanned",
        data=compact,
        preview=json.dumps(compact, ensure_ascii=False, default=str),
        truncated=truncated,
        provenance={
            "source": str(source), "format": "FASTQ", "scan_mode": "bounded_prefix_pilot",
            "report": str(report_path), "bytes_scanned": bytes_scanned,
        },
    )
