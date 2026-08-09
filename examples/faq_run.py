"""Ask the FAQ domain Agent a question about documents in a directory."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import create_agent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--documents", default=".", help="document workspace")
    args = parser.parse_args()
    agent = create_agent("faq", workdir=str(Path(args.documents).resolve()))
    result = await agent.run(args.question)
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
