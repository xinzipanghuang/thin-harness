"""Run the bioinformatics domain Agent against one local data file."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import create_agent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="FASTA, FASTQ, or VCF file to inspect")
    args = parser.parse_args()
    source = str(Path(args.path).resolve())
    agent = create_agent("bioinformatics", workdir=str(Path.cwd()))
    result = await agent.run(
        f"Inspect {source}, summarize its measurable properties, and preserve provenance."
    )
    print(result.text)
    print(f"\ntrace: {result.trace().get('run_id')}")


if __name__ == "__main__":
    asyncio.run(main())
