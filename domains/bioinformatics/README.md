# Bioinformatics domain

This Agent treats biological data as a file-backed data plane. The model sees
only compact control-plane metadata: absolute paths, bounded summaries,
sanitized head/tail fragments, limits, status, and provenance.

## Safe workflow

1. `bio.file.peep` reads metadata and at most a small decompressed head.
2. `bio.fasta.inspect`, `bio.fastq.inspect`, and `bio.vcf.inspect` pilot-scan
   at most 1000 records and 4 MiB by default. JSON reports go to
   `bio_outputs/inspections/`.
3. `bio.command.plan` records an allowlisted, placeholder-based pilot command
   without running the workload (a bounded `--version` probe is allowed).
4. `bio.command.pilot` runs it for at most 60 seconds with stdout/stderr sent
   directly to files. Each log is continuously drained and capped at 100 MiB;
   excess log output is discarded rather than buffered in memory.
5. `bio.job.submit` accepts a full command only after a successful pilot with
   unchanged input paths, sizes, and mtimes. It returns immediately.
6. `bio.job.status` and `bio.job.tail` expose compact state and bounded logs;
   `bio.job.cancel` requires the exact job id twice.

All command outputs live under `bio_outputs/jobs/<job_id>/`. Existing outputs
are never overwritten. Input arguments use `{input0}`, `{input1}`, etc.; full
output paths use `{output_dir}`.

The executable allowlist currently includes common tools such as samtools,
bcftools, bedtools, seqkit, fastp, minimap2, bwa, bowtie2, STAR, salmon,
kallisto, GATK, FastQC, and MultiQC. Extending the allowlist is a code review,
not a model decision.
