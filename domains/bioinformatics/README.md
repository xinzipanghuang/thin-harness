# Bioinformatics domain

The first specialist example built on thin-harness. It exposes:

- `bio.fasta.inspect` — record count, lengths, GC and ambiguous bases;
- `bio.fastq.inspect` — read lengths, GC and mean Phred+33 quality;
- `bio.vcf.inspect` — samples, contigs, filters and variant types;
- `bio.command.run` — structured local executable invocation without a shell.

Run it in chat:

```bash
python -m chat --agent bioinformatics --session bio-work
```

Or inspect one file directly:

```bash
python examples/bioinformatics_run.py data/sample.vcf
```

Format summaries are measurements, not biological conclusions. The Agent's
domain prompt preserves that distinction and its `finalize` hook appends a
deterministic provenance section.
