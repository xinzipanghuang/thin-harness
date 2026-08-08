"""Default coding agent: filesystem/shell/python tools, model from .env."""

from __future__ import annotations

from core.agent import Agent


class CodingAgent(Agent):
    """General coding agent.

    Model comes from .env (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL /
    LLM_ENABLE_THINKING) and is resolved by the provider layer into the
    OpenAI-SDK transport — this agent only defines the domain: prompt, tools,
    and runtime limits.
    """

    name = "coding-agent"
    prompt_path = "prompts/agent.md"
    tool_include = ["filesystem.*", "shell.run", "python.run", "artifacts.read"]
    tool_exclude = ["filesystem.delete"]
    max_steps = 8
    max_tool_calls = 12
    tool_timeout = 60
    max_consecutive_failures = 3
