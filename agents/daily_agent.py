"""Default full-featured local agent: filesystem, shell, python, documents, daily tools."""

from __future__ import annotations

from core.agent import Agent
from core.tool import ToolContext
from core.types import ToolCall, ToolResult


class DailyAgent(Agent):
    """Local daily-use agent with the full shared tool set.

    Covers repo/filesystem work (``filesystem.*``), shell and python
    execution, document reading (``documents.*``), artifact inspection
    (``artifacts.read``), and small daily helpers (``daily.*``). Destructive
    ``filesystem.delete`` is excluded. The model comes from .env
    (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL / LLM_ENABLE_THINKING).
    """

    name = "daily-agent"
    prompt_path = "prompts/agent.md"
    tool_include = [
        "filesystem.*",
        "shell.run",
        "python.run",
        "documents.*",
        "artifacts.read",
        "daily.*",
    ]
    tool_exclude = ["filesystem.delete"]
    max_steps = 12
    max_tool_calls = 24
    tool_timeout = 60
    max_consecutive_failures = 3

    async def on_tool_result(
        self,
        ctx: ToolContext,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        """Remember what was read/searched so later turns can find it again.

        Facts persist to SQLite and are loaded into VERIFIED FACTS on the next
        turn. Without them, a follow-up question ("this question", "文档里")
        has no idea where the source file is and answers from general
        knowledge instead of the document.
        """
        if not result.ok:
            return
        args = call.arguments or {}
        if call.name in ("filesystem.read", "documents.read"):
            path = args.get("path")
            if not path:
                return
            tracker = ctx.state.setdefault("read_fact_paths", {})
            if path in tracker:
                return
            tracker[path] = True
            preview = ""
            if isinstance(result.data, str):
                preview = result.data
            elif isinstance(result.data, dict):
                preview = str(result.data.get("text") or "")
            if not preview and result.preview:
                preview = result.preview
            snippet = " ".join(preview.split())[:120]
            ctx.record_fact(
                f"Read file {path}; it begins with: {snippet}",
                source=str(path),
            )
        elif call.name in ("filesystem.grep", "documents.search"):
            pattern = str(args.get("pattern") or "")
            path = str(args.get("path") or args.get("root") or "")
            if not pattern or not path:
                return
            tracker = ctx.state.setdefault("search_fact_keys", set())
            key = f"{path}\x00{pattern}"
            if key in tracker:
                return
            tracker.add(key)
            if isinstance(result.data, dict):
                matches = result.data.get("matches") or result.data.get("total_lines") or "?"
                ctx.record_fact(
                    f"Searched {path} for {pattern!r}: {len(matches)} match(es) found",
                    source=str(path),
                )
