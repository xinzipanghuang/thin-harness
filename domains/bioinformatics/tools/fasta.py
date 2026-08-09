"""FASTA inspection tools."""

from __future__ import annotations

from core.tool import ToolContext, ToolResult, clamp_int, tool

from ._io import open_text, resolve_path


@tool(name="bio.fasta.inspect", cacheable=True)
def inspect_fasta(
    ctx: ToolContext,
    path: str,
    max_records: int = 100000,
) -> ToolResult:
    """Summarize FASTA record counts, lengths, GC content, and identifiers.

    Args:
        path: FASTA or gzip-compressed FASTA file.
        max_records: Maximum records to scan before reporting truncation.
    """
    source = resolve_path(ctx, path)
    limit = clamp_int(max_records, 1, 1_000_000, 100000)
    lengths: list[int] = []
    identifiers: list[str] = []
    gc = 0
    ambiguous = 0
    current = 0
    truncated = False

    with open_text(source) as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(">"):
                if lengths or current:
                    lengths.append(current)
                    if len(lengths) >= limit:
                        truncated = True
                        break
                current = 0
                if len(identifiers) < 20:
                    identifiers.append(line[1:].split()[0] if line[1:].strip() else "")
                continue
            sequence = line.upper()
            current += len(sequence)
            gc += sequence.count("G") + sequence.count("C")
            ambiguous += sum(base not in "ACGT" for base in sequence)
        else:
            if current or not lengths:
                lengths.append(current)

    total = sum(lengths)
    data = {
        "path": str(source),
        "records_scanned": len(lengths),
        "total_bases": total,
        "min_length": min(lengths) if lengths else 0,
        "max_length": max(lengths) if lengths else 0,
        "mean_length": round(total / len(lengths), 2) if lengths else 0,
        "gc_fraction": round(gc / total, 6) if total else 0,
        "ambiguous_bases": ambiguous,
        "first_identifiers": identifiers,
        "truncated": truncated,
    }
    return ToolResult(
        ok=True,
        summary=f"FASTA: {len(lengths)} record(s), {total} base(s)",
        data=data,
        truncated=truncated,
        provenance={"source": str(source), "format": "FASTA", "scanner": "thin-harness"},
    )
