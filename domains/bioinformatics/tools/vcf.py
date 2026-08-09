"""VCF inspection tools."""

from __future__ import annotations

from collections import Counter

from core.tool import ToolContext, ToolResult, clamp_int, tool

from ._io import open_text, resolve_path


@tool(name="bio.vcf.inspect", cacheable=True)
def inspect_vcf(
    ctx: ToolContext,
    path: str,
    max_variants: int = 1_000_000,
) -> ToolResult:
    """Summarize VCF samples, contigs, filters, SNPs, and indels.

    Args:
        path: VCF or gzip-compressed VCF file.
        max_variants: Maximum variant records to scan.
    """
    source = resolve_path(ctx, path)
    limit = clamp_int(max_variants, 1, 10_000_000, 1_000_000)
    samples: list[str] = []
    contigs: Counter[str] = Counter()
    filters: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    variants = 0
    truncated = False

    with open_text(source) as handle:
        for raw in handle:
            if raw.startswith("##"):
                continue
            if raw.startswith("#CHROM"):
                columns = raw.rstrip("\r\n").split("\t")
                samples = columns[9:]
                continue
            if raw.startswith("#") or not raw.strip():
                continue
            fields = raw.rstrip("\r\n").split("\t")
            if len(fields) < 8:
                raise ValueError(f"Malformed VCF record near variant {variants + 1}")
            chrom, ref, alt, filter_value = fields[0], fields[3], fields[4], fields[6]
            contigs[chrom] += 1
            filters[filter_value] += 1
            alleles = alt.split(",")
            for allele in alleles:
                if len(ref) == 1 and len(allele) == 1:
                    kinds["snp"] += 1
                elif allele.startswith("<"):
                    kinds["symbolic"] += 1
                else:
                    kinds["indel_or_complex"] += 1
            variants += 1
            if variants >= limit:
                truncated = True
                break

    data = {
        "path": str(source),
        "variants_scanned": variants,
        "samples": samples,
        "sample_count": len(samples),
        "variant_types": dict(kinds),
        "contigs": dict(contigs.most_common()),
        "filters": dict(filters.most_common()),
        "truncated": truncated,
    }
    return ToolResult(
        ok=True,
        summary=f"VCF: {variants} variant(s), {len(samples)} sample(s)",
        data=data,
        truncated=truncated,
        provenance={"source": str(source), "format": "VCF", "scanner": "thin-harness"},
    )
