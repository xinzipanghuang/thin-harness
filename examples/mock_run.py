"""Offline example: run the coding agent with a scripted (fake) model.

No API key or network needed. Tool calls in the script execute for real
against the local workspace, so output is deterministic.

Run from the project root:  python examples/mock_run.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import create_agent
from core.model import ModelResponse, ScriptedModel
from core.tool import ToolCall

SCRIPT = [
    ModelResponse(tool_calls=[ToolCall(name="filesystem.list", arguments={"path": "."})]),
    ModelResponse(
        tool_calls=[ToolCall(name="filesystem.read", arguments={"path": "README.md", "max_chars": 2000})]
    ),
    ModelResponse(text="The project is a minimal Codex-style agent runtime; README.md documents it."),
]


async def main() -> None:
    agent = create_agent("coding", model=ScriptedModel(SCRIPT))
    result = await agent.run("Inspect this project and explain the main entry point.")
    print(f"stop_reason : {result.stop_reason}")
    print(f"steps       : {result.state.steps}")
    print(f"tool_calls  : {result.state.tool_calls}")
    print(f"observations: {len(result.state.observations)}")
    print()
    print("final answer:")
    print(result.text)
    print()
    print("run log:")
    for entry in result.log.entries:
        print(entry)


if __name__ == "__main__":
    asyncio.run(main())
