"""FAQ Agent assembled from the generic runtime and FAQ-owned tools."""

from __future__ import annotations

import json

from core.agent import Agent
from core.registry import register_agent
from core.tool import ToolContext
from core.types import Observation


@register_agent("faq")
class FAQAgent(Agent):
    """Answer questions from workspace documents with source-grounded output."""

    name = "faq-agent"
    prompt_paths = ["prompts/agent.md", "domains/faq/prompt.md"]
    tool_packages = ["tools", "domains.faq.tools"]
    tool_include = [
        "documents.*",
        "filesystem.list",
        "filesystem.find",
        "filesystem.grep",
        "artifacts.read",
        "faq.*",
    ]
    max_steps = 10
    max_tool_calls = 20
    tool_timeout = 60
    max_consecutive_failures = 3
    keep_recent_observations = 8
    experience_enabled = False

    async def bootstrap(self, ctx: ToolContext) -> list[Observation]:
        """List a small set of candidate documents before the first model call."""
        try:
            from tools.documents import list_documents

            docs = list_documents(ctx, root=ctx.workdir or ".", max_results=5)
        except Exception:
            return []
        if not docs:
            return []
        preview = json.dumps(docs, ensure_ascii=False, default=str)
        return [
            Observation(
                tool="documents.list",
                status="success",
                summary=f"Found {len(docs)} document(s) in the workspace",
                preview=preview[:2000],
                provenance={"root": ctx.workdir or ".", "document_count": len(docs)},
                step=0,
            )
        ]
