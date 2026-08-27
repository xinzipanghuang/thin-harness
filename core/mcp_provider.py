"""Optional MCP Tool discovery for the domain-neutral client."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, AsyncIterator, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from .tool import Tool
from .types import ToolResult


@dataclass(frozen=True)
class McpEndpoint:
    endpoint_id: str
    url: str
    token_env: str
    connect_timeout_seconds: float = 5.0
    call_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class DiscoveredMcpTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class McpTransport(Protocol):
    async def list_tools(self) -> list[DiscoveredMcpTool]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class StreamableHttpTransport:
    def __init__(self, endpoint: McpEndpoint) -> None:
        self.endpoint = endpoint

    def _token(self) -> str:
        token = os.environ.get(self.endpoint.token_env, "")
        if len(token.encode("utf-8")) < 32:
            raise ValueError(f"missing or weak MCP credential: {self.endpoint.token_env}")
        return token

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        timeout = httpx.Timeout(
            self.endpoint.call_timeout_seconds,
            connect=self.endpoint.connect_timeout_seconds,
        )
        headers = {"Authorization": f"Bearer {self._token()}"}
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            async with streamable_http_client(
                self.endpoint.url, http_client=client, terminate_on_close=True
            ) as (read_stream, write_stream, _):
                async with ClientSession(
                    read_stream, write_stream,
                    read_timeout_seconds=timedelta(seconds=self.endpoint.call_timeout_seconds),
                ) as session:
                    await session.initialize()
                    yield session

    async def list_tools(self) -> list[DiscoveredMcpTool]:
        discovered: list[DiscoveredMcpTool] = []
        async with self._session() as session:
            cursor: str | None = None
            while True:
                page = await session.list_tools(cursor=cursor)
                discovered.extend(
                    DiscoveredMcpTool(
                        name=item.name,
                        description=item.description or f"Remote MCP Tool {item.name}.",
                        input_schema=item.inputSchema,
                    )
                    for item in page.tools
                )
                cursor = page.nextCursor
                if not cursor:
                    return discovered

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async with self._session() as session:
            result = await session.call_tool(
                name, arguments,
                read_timeout_seconds=timedelta(seconds=self.endpoint.call_timeout_seconds),
            )
        if result.isError:
            detail = " ".join(
                item.text for item in result.content if isinstance(item, TextContent)
            )
            raise RuntimeError((detail or "remote MCP Tool failed")[:2000])
        if result.structuredContent is not None:
            return result.structuredContent
        texts = [item.text for item in result.content if isinstance(item, TextContent)]
        if len(texts) != 1:
            raise RuntimeError("remote MCP Tool returned no unambiguous result")
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]


class McpToolProvider:
    """Convert an optional MCP catalog into ordinary harness Tools."""

    def __init__(self, endpoint: McpEndpoint, *, optional: bool = True,
                 transport: McpTransport | None = None) -> None:
        self.endpoint = endpoint
        self.optional = optional
        self.transport = transport or StreamableHttpTransport(endpoint)
        self.last_error: str | None = None

    async def load_tools(self) -> list[Tool]:
        try:
            remote_tools = await self.transport.list_tools()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.optional:
                return []
            raise
        names: set[str] = set()
        tools: list[Tool] = []
        for remote in remote_tools:
            if remote.name in names:
                raise ValueError(f"duplicate MCP Tool name: {remote.name}")
            names.add(remote.name)
            tools.append(Tool(
                name=remote.name,
                fn=self._call(remote.name),
                description=remote.description,
                parameters=remote.input_schema,
            ))
        self.last_error = None
        return tools

    def _call(self, remote_name: str):
        async def invoke(**arguments: Any) -> ToolResult:
            try:
                data = await self.transport.call_tool(remote_name, arguments)
            except Exception as exc:
                return ToolResult(
                    ok=False, summary=f"MCP Tool failed: {remote_name}",
                    error=f"{type(exc).__name__}: {exc}",
                    provenance={"mcp_endpoint": self.endpoint.endpoint_id},
                )
            return ToolResult(
                ok=True, summary=f"MCP Tool completed: {remote_name}", data=data,
                provenance={"mcp_endpoint": self.endpoint.endpoint_id},
            )
        return invoke


async def load_optional_mcp_tools(endpoints: list[McpEndpoint]) -> list[Tool]:
    merged: list[Tool] = []
    names: set[str] = set()
    for endpoint in endpoints:
        tools = await McpToolProvider(endpoint, optional=True).load_tools()
        collisions = names.intersection(tool.name for tool in tools)
        if collisions:
            raise ValueError(f"duplicate MCP Tool names: {', '.join(sorted(collisions))}")
        merged.extend(tools)
        names.update(tool.name for tool in tools)
    return merged
