"""FASTQ inspection tools."""

from __future__ import annotations

from core.tool import ToolContext, ToolResult, clamp_int, tool

from ._io import open_text, resolve_path


@tool(name="bio.fastq.inspect", cacheable=True)
def inspect_fastq(
    ctx: ToolContext,
    path: str,
    max_reads: int = 100000,
) -> ToolResult:
    """Summarize FASTQ read lengths, GC content, and mean Phred+33 quality.

    Args:
        path: FASTQ or gzip-compressed FASTQ file.
        max_reads: Maximum reads to scan.
    """
    source = resolve_path(ctx, path)
    limit = clamp_int(max_reads, 1, 10_000_000, 100000)
    reads = bases = gc = quality_sum = quality_bases = 0
    min_length: int | None = None
    max_length = 0
    truncated = False

    with open_text(source) as handle:
        while reads < limit:
            header = handle.readline()
            if not header:
                break
            sequence = handle.readline().rstrip("\r\n")
            plus = handle.readline()
            quality = handle.readline().rstrip("\r\n")
            if not sequence or not plus or len(sequence) != len(quality):
                raise ValueError(f"Malformed FASTQ record near read {reads + 1}")
            if not header.startswith("@") or not plus.startswith("+"):
                raise ValueError(f"Malformed FASTQ markers near read {reads + 1}")
            length = len(sequence)
            reads += 1
            bases += length
            gc += sequence.upper().count("G") + sequence.upper().count("C")
            quality_sum += sum(max(0, ord(char) - 33) for char in quality)
            quality_bases += len(quality)
            min_length = length if min_length is None else min(min_length, length)
            max_length = max(max_length, length)
        if reads == limit and handle.readline():
            truncated = True

    data = {
        "path": str(source),
        "reads_scanned": reads,
        "total_bases": bases,
        "min_length": min_length or 0,
        "max_length": max_length,
        "mean_length": round(bases / reads, 2) if reads else 0,
        "gc_fraction": round(gc / bases, 6) if bases else 0,
        "mean_phred33": round(quality_sum / quality_bases, 2) if quality_bases else 0,
        "truncated": truncated,
    }
    return ToolResult(
        ok=True,
        summary=f"FASTQ: {reads} read(s), {bases} base(s)",
        data=data,
        truncated=truncated,
        provenance={"source": str(source), "format": "FASTQ", "scanner": "thin-harness"},
    )
