"""Default full-featured local agent: filesystem, shell, python, documents, daily tools."""

from __future__ import annotations

from core.agent import Agent


class DailyAgent(Agent):
    """Local daily-use agent with the full shared tool set.

    Covers repo/filesystem work (``filesystem.*``), shell and python
    execution, document reading (``documents.*``), artifact inspection
    (``artifacts.read``), web search (``web.*``), and small daily helpers
    (``daily.*``). Destructive ``filesystem.delete`` is excluded. The model
    comes from .env (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL /
    LLM_ENABLE_THINKING).
    """

    name = "daily-agent"
    prompt_path = "prompts/agent.md"
    tool_include = [
        "filesystem.*",
        "shell.run",
        "python.run",
        "documents.*",
        "artifacts.read",
        "web.*",
        "daily.*",
    ]
    tool_exclude = ["filesystem.delete"]
    max_steps = 8
    max_tool_calls = 12
    tool_timeout = 60
    max_consecutive_failures = 3
