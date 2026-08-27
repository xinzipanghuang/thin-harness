"""Cheap file preflight for large biological datasets."""

from __future__ import annotations

import json
from pathlib import Path

from core.tool import ToolContext, ToolResult, tool

from ._io import bounded_head, is_gzip, resolve_path, safe_text, sidecar_indexes, write_report


def _format_hint(path: Path, head: list[str]) -> str:
    names = [suffix.lower() for suffix in path.suffixes]
    effective = names[-2] if names and names[-1] in (".gz", ".bgz", ".bgzf") and len(names) > 1 else (names[-1] if names else "")
    mapping = {
        ".fa": "FASTA", ".fasta": "FASTA", ".fna": "FASTA",
        ".fq": "FASTQ", ".fastq": "FASTQ",
        ".vcf": "VCF", ".bcf": "BCF",
        ".bam": "BAM", ".sam": "SAM", ".cram": "CRAM",
        ".bed": "BED", ".gff": "GFF", ".gtf": "GTF",
    }
    if effective in mapping:
        return mapping[effective]
    first = head[0] if head else ""
    if first.startswith(">"):
        return "FASTA"
    if first.startswith("##fileformat=VCF") or first.startswith("#CHROM"):
        return "VCF"
    if first.startswith("@"):
        return "FASTQ-or-SAM"
    return "unknown"


@tool(name="bio.file.peep", cacheable=True)
def peep_file(
    ctx: ToolContext,
    path: str,
    head_lines: int = 8,
    head_bytes: int = 16384,
) -> ToolResult:
    """Preflight one biological data file without scanning it.

    Returns only metadata and a small sanitized head. Always call this before
    an inspector, pilot, or long-running job.

    Args:
        path: Input file path.
        head_lines: Maximum sanitized head lines returned to the model (1-20).
        head_bytes: Maximum decompressed bytes read for the head (1-64 KiB).
    """
    source = resolve_path(ctx, path)
    stat = source.stat()
    head = bounded_head(source, max_bytes=head_bytes, max_lines=max(1, min(20, head_lines)))
    indexes = sidecar_indexes(source)
    data = {
        "path": str(source),
        "name": safe_text(source.name, 200),
        "size_bytes": stat.st_size,
        "compressed": is_gzip(source),
        "format_hint": _format_hint(source, head["lines"]),
        "indexes": indexes,
        "head": head,
        "recommended_pilot": {
            "max_records": 1000,
            "max_uncompressed_bytes": 4 * 1024 * 1024,
        },
    }
    report_path = write_report(ctx, "peep", data)
    compact = {**data, "report_path": str(report_path)}
    return ToolResult(
        ok=True,
        summary=f"Peeped {source.name}: {stat.st_size} bytes, {data['format_hint']}",
        data=compact,
        preview=json.dumps(compact, ensure_ascii=False, default=str),
        provenance={"source": str(source), "operation": "bounded_peep", "report": str(report_path)},
    )
