"""WES support Agent backed by the central WES MCP service."""

from __future__ import annotations

from core.agent import Agent
from core.dotenv import get_env
from core.mcp_provider import McpEndpoint, McpToolProvider
from core.registry import register_agent
from core.tool import ToolContext
from core.types import Observation


@register_agent("wes")
class WESAgent(Agent):
    """Review WES runs and result evidence through the central MCP facade."""

    name = "wes-support-agent"
    prompt_paths = ["prompts/agent.md", "domains/wes/prompt.md"]
    tool_packages = ["tools"]
    tool_include = ["artifacts.read"]
    max_steps = 14
    max_tool_calls = 24
    tool_timeout = 120
    max_consecutive_failures = 3
    keep_recent_observations = 10
    experience_enabled = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._mcp_loaded = False
        self._mcp_error: str | None = None
        self._mcp_url = get_env("WES_MCP_URL") or "http://127.0.0.1:9090/mcp"

    async def on_run_start(self, request: str) -> None:
        if self._mcp_loaded:
            return
        endpoint = McpEndpoint(
            endpoint_id="wes-center",
            url=self._mcp_url,
            token_env=get_env("WES_MCP_TOKEN_ENV") or "WES_MCP_TOKEN",
            connect_timeout_seconds=float(get_env("WES_MCP_CONNECT_TIMEOUT") or "5"),
            call_timeout_seconds=float(get_env("WES_MCP_CALL_TIMEOUT") or "120"),
            allow_unauthenticated_loopback=True,
        )
        provider = McpToolProvider(endpoint, optional=True)
        discovered = await provider.load_tools()
        self._mcp_error = provider.last_error
        # Prefer the consolidated catalog once the center exposes it. During
        # migration, keep the current compatibility tools so the full path can
        # already be exercised against an older center process.
        consolidated = [tool for tool in discovered if tool.name.startswith("wes_")]
        selected = consolidated or discovered
        existing = {tool.name for tool in self.tools}
        self.tools.extend(tool for tool in selected if tool.name not in existing)
        self._mcp_loaded = True

    async def bootstrap(self, ctx: ToolContext) -> list[Observation]:
        remote = [tool.name for tool in self.tools if tool.name != "artifacts.read"]
        if self._mcp_error:
            return [Observation(
                tool="wes.mcp.connect",
                status="error",
                summary="WES Center MCP is unavailable",
                preview=f"endpoint={self._mcp_url}; error={self._mcp_error}",
                provenance={"mcp_endpoint": "wes-center", "url": self._mcp_url},
                step=0,
            )]
        return [Observation(
            tool="wes.mcp.catalog",
            status="success",
            summary=f"Loaded {len(remote)} WES Center MCP tools",
            preview=", ".join(remote),
            provenance={"mcp_endpoint": "wes-center", "url": self._mcp_url, "tool_count": len(remote)},
            step=0,
        )]
