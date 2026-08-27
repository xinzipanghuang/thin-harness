"""A controllable local Agent for common bioinformatics file workflows."""

from __future__ import annotations

import json

from core.agent import Agent
from core.registry import register_agent
from core.tool import ToolContext
from core.types import RunState


@register_agent("bioinformatics")
class BioinformaticsAgent(Agent):
    """Inspect biological data files and run explicitly selected local tools."""

    name = "bioinformatics-agent"
    prompt_paths = ["prompts/agent.md", "domains/bioinformatics/prompt.md"]
    tool_packages = ["tools", "domains.bioinformatics.tools"]
    tool_include = [
        "filesystem.list",
        "bio.*",
    ]
    max_steps = 12
    max_tool_calls = 20
    tool_timeout = 120
    max_consecutive_failures = 3
    experience_enabled = False
    stream_before_finalize = True

    async def finalize(self, ctx: ToolContext, text: str, state: RunState) -> str:
        """Append a compact deterministic provenance record to analytical output."""
        records: list[dict] = []
        seen: set[str] = set()
        for evidence in state.evidence:
            if not evidence.provenance:
                continue
            key = json.dumps(evidence.provenance, sort_keys=True, ensure_ascii=False, default=str)
            if key in seen:
                continue
            seen.add(key)
            records.append(evidence.provenance)
        if not records:
            return text
        lines = ["### Provenance"]
        for record in records:
            label = str(
                record.get("command")
                or record.get("source")
                or record.get("format")
                or "tool result"
            )
            version = record.get("tool_version") or record.get("parser_version")
            if version:
                label += f" (version {version})"
            lines.append(f"- {label}")
        separator = "\n" if text.endswith("\n") else "\n\n"
        return text + separator + "\n".join(lines)
