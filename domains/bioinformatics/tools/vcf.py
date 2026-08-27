"""Bounded VCF pilot inspection."""

from __future__ import annotations

import json
from collections import Counter

from core.tool import ToolContext, ToolResult, clamp_int, tool

from ._io import BoundedTextReader, clamp_bytes, resolve_path, safe_text, write_report


@tool(name="bio.vcf.inspect", cacheable=True)
def inspect_vcf(
    ctx: ToolContext,
    path: str,
    max_variants: int = 1000,
    max_uncompressed_bytes: int = 4 * 1024 * 1024,
) -> ToolResult:
    """Pilot-scan a bounded VCF prefix and save the report as JSON.

    Genotype columns are not returned to the model. The scan stops at either
    the record or decompressed-byte budget.

    Args:
        path: VCF or gzip-compressed VCF file.
        max_variants: Maximum variant records to scan (1-100000).
        max_uncompressed_bytes: Maximum decompressed bytes to scan (1 KiB-64 MiB).
    """
    source = resolve_path(ctx, path)
    variant_limit = clamp_int(max_variants, 1, 100_000, 1000)
    byte_limit = clamp_bytes(max_uncompressed_bytes)
    samples: list[str] = []
    contigs: Counter[str] = Counter()
    filters: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    variants = 0
    record_limited = False

    with BoundedTextReader(source, max_bytes=byte_limit) as reader:
        for raw in reader:
            if raw.startswith("##"):
                continue
            if raw.startswith("#CHROM"):
                columns = raw.rstrip("\r\n").split("\t")
                samples = [safe_text(value, 160) for value in columns[9:]]
                continue
            if raw.startswith("#") or not raw.strip():
                continue
            fields = raw.rstrip("\r\n").split("\t", 8)
            if len(fields) < 8:
                raise ValueError(f"Malformed VCF record near variant {variants + 1}")
            chrom, ref, alt, filter_value = fields[0], fields[3], fields[4], fields[6]
            contigs[safe_text(chrom, 160)] += 1
            filters[safe_text(filter_value, 160)] += 1
            for allele in alt.split(","):
                if len(ref) == 1 and len(allele) == 1:
                    kinds["snp"] += 1
                elif allele.startswith("<"):
                    kinds["symbolic"] += 1
                else:
                    kinds["indel_or_complex"] += 1
            variants += 1
            if variants >= variant_limit:
                record_limited = True
                break
        bytes_scanned = reader.bytes_read
        byte_limited = reader.truncated
        line_too_long = reader.line_too_long

    truncated = record_limited or byte_limited
    report = {
        "path": str(source),
        "scan_mode": "bounded_prefix_pilot",
        "variants_scanned": variants,
        "bytes_scanned": bytes_scanned,
        "limits": {"max_variants": variant_limit, "max_uncompressed_bytes": byte_limit},
        "samples": samples,
        "sample_count": len(samples),
        "variant_types": dict(kinds),
        "contigs": dict(contigs.most_common()),
        "filters": dict(filters.most_common()),
        "truncated": truncated,
        "truncation_reason": (
            "line_too_long" if line_too_long else
            "byte_limit" if byte_limited else
            "record_limit" if record_limited else None
        ),
    }
    report_path = write_report(ctx, "vcf", report)
    compact = {
        key: value
        for key, value in report.items()
        if key not in {"samples", "contigs", "filters"}
    }
    compact["first_samples"] = samples[:5]
    compact["top_contigs"] = dict(contigs.most_common(20))
    compact["top_filters"] = dict(filters.most_common(20))
    compact["report_path"] = str(report_path)
    return ToolResult(
        ok=True,
        summary=f"VCF pilot: {variants} variant(s), {bytes_scanned} byte(s) scanned",
        data=compact,
        preview=json.dumps(compact, ensure_ascii=False, default=str),
        truncated=truncated,
        provenance={
            "source": str(source), "format": "VCF", "scan_mode": "bounded_prefix_pilot",
            "report": str(report_path), "bytes_scanned": bytes_scanned,
        },
    )
