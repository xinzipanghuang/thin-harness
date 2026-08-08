"""FAQ agent: reads PDF documents (in small windows) and answers questions."""

from __future__ import annotations

import json

from core.agent import Agent
from core.tool import ToolContext, agent_tool
from core.types import Observation


@agent_tool(cacheable=True)
def count_pages(ctx: ToolContext, path: str) -> int:
    """Count the pages in a PDF document.

    Args:
        path: PDF file path (absolute, or relative to the run workdir).
    """
    from tools.documents import _pdf_pages, _resolve

    return len(_pdf_pages(_resolve(ctx, path)))


class FAQAgent(Agent):
    """FAQ assistant that answers questions from documents.

    FAQ agent: answers questions from documents (PDF/DOCX/txt/md/...).

    ``documents.*`` owns knowledge-base reading; ``filesystem.*`` here is
    limited to repo exploration (list/find/grep); ``artifacts.read`` only
    accepts artifact ids shown in AVAILABLE ARTIFACTS. The model comes from
    .env (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL / LLM_ENABLE_THINKING).
    """

    name = "faq-agent"
    prompt_path = "prompts/agent.md"
    tool_include = [
        "documents.*",
        "filesystem.list",
        "filesystem.find",
        "filesystem.grep",
        "artifacts.read",
    ]
    own_tools = [count_pages]
    max_steps = 10
    max_tool_calls = 20
    tool_timeout = 60
    max_consecutive_failures = 3
    keep_recent_observations = 8

    async def bootstrap(self, ctx: ToolContext) -> list[Observation]:
        """Pre-search: list candidate documents before the first model call."""
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
                step=0,
            )
        ]
