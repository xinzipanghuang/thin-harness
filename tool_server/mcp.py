"""Authenticated MCP Streamable HTTP transport for ``ServerToolRegistry``."""

from __future__ import annotations

import hmac
import json
import threading
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

import anyio
import mcp.types as mcp_types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .registry import ServerToolRegistry, ToolInputError, ToolNotFoundError


DEFAULT_INSTRUCTIONS = (
    "This server exposes reviewed atomic tools and durable long skills. Start a long "
    "skill with skill.<id>.submit, retain its task_id, poll task.get, then call "
    "task.result after a terminal status. Use task.logs for bounded diagnostics and "
    "task.cancel only when cancellation is intended. Only explicitly registered "
    "capabilities are available. Arguments and results use closed schemas. Large data "
    "must be stored in the approved data plane and returned as an artifact URI."
)
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class BearerAuthASGI:
    """Small fixed-token gate suitable behind a TLS reverse proxy."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if len(token.encode("utf-8")) < 32:
            raise ValueError("bearer token must contain at least 32 UTF-8 bytes")
        self.app = app
        self.expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self.expected):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=HTTPStatus.UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BoundedResponseASGI:
    """Buffer bounded JSON responses so an oversized payload is never emitted."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        start: Message | None = None
        chunks: list[bytes] = []
        size = 0

        async def capture(message: Message) -> None:
            nonlocal start, size
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            body = message.get("body", b"")
            size += len(body)
            if size <= self.max_bytes:
                chunks.append(body)

        await self.app(scope, receive, capture)
        if size > self.max_bytes:
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "MCP response exceeds the configured byte limit",
                    },
                },
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        if start is None:
            raise RuntimeError("MCP transport did not start an HTTP response")
        headers = _replace_headers(start.get("headers", []), size)
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": b"".join(chunks)})


def _replace_headers(headers: list[tuple[bytes, bytes]], length: int) -> list[tuple[bytes, bytes]]:
    blocked = {b"content-length", b"cache-control", b"x-content-type-options"}
    kept = [(key, value) for key, value in headers if key.lower() not in blocked]
    kept.extend(
        [
            (b"content-length", str(length).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]
    )
    return kept


class McpToolServer:
    """One MCP endpoint backed by an explicitly assembled registry."""

    def __init__(
        self,
        registry: ServerToolRegistry,
        *,
        bearer_token: str,
        name: str = "thin-harness-tool-server",
        version: str = "1.0.0",
        instructions: str = DEFAULT_INSTRUCTIONS,
        host: str = "127.0.0.1",
        port: int = 8765,
        allow_insecure_remote: bool = False,
    ) -> None:
        if host not in _LOOPBACK_HOSTS and not allow_insecure_remote:
            raise ValueError(
                "plaintext HTTP may bind only to loopback; use a TLS reverse proxy or "
                "set allow_insecure_remote=True explicitly"
            )
        self.registry = registry
        self.host = host
        self.port = port
        self.run_slots = threading.BoundedSemaphore(registry.limits.max_concurrent_runs)
        self.protocol = Server(name=name, version=version, instructions=instructions)
        self._install_handlers()
        self._validate_catalog_size()

        security = None
        if host in _LOOPBACK_HOSTS:
            security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"],
                allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
            )
        self.session_manager = StreamableHTTPSessionManager(
            app=self.protocol,
            json_response=True,
            stateless=True,
            security_settings=security,
            max_request_body_size=registry.limits.max_request_bytes,
        )
        endpoint: ASGIApp = self.session_manager.handle_request
        endpoint = BearerAuthASGI(endpoint, bearer_token)
        endpoint = BoundedResponseASGI(endpoint, registry.limits.max_response_bytes)

        @asynccontextmanager
        async def lifespan(_: Starlette):
            async with self.session_manager.run():
                yield

        async def health(_: Request) -> JSONResponse:
            return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

        self.app = Starlette(
            routes=[
                Route("/healthz", health, methods=["GET"]),
                Route("/mcp", endpoint=endpoint, methods=["POST"]),
            ],
            lifespan=lifespan,
        )

    def _mcp_tools(self) -> list[mcp_types.Tool]:
        tools: list[mcp_types.Tool] = []
        for manifest in self.registry.manifests():
            tools.append(
                mcp_types.Tool(
                    name=manifest.name,
                    description=manifest.description,
                    inputSchema=manifest.input_schema,
                    outputSchema=manifest.output_schema,
                    annotations=mcp_types.ToolAnnotations(
                        readOnlyHint=manifest.safety.read_only,
                        destructiveHint=manifest.safety.destructive,
                        idempotentHint=manifest.safety.idempotent,
                        openWorldHint=manifest.safety.open_world,
                    ),
                    _meta={
                        "version": manifest.version,
                        "runtime": manifest.runtime,
                        "timeoutSeconds": manifest.timeout_seconds,
                    },
                )
            )
        return tools

    def _install_handlers(self) -> None:
        @self.protocol.list_tools()
        async def list_tools() -> list[mcp_types.Tool]:
            return self._mcp_tools()

        @self.protocol.call_tool(validate_input=True)
        async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any] | mcp_types.CallToolResult:
            if not self.run_slots.acquire(blocking=False):
                return _error_result("server_busy", "maximum concurrent tool runs reached")
            try:
                # Do not abandon a worker thread on client cancellation: the
                # concurrency slot remains held until the real run has ended.
                response = await anyio.to_thread.run_sync(
                    self.registry.execute,
                    name,
                    arguments,
                    abandon_on_cancel=False,
                )
            except ToolNotFoundError:
                return _error_result("tool_not_found", "tool is not registered")
            except ToolInputError as exc:
                return _error_result("invalid_arguments", str(exc))
            finally:
                self.run_slots.release()
            if not response.ok:
                error = response.error
                return _error_result(
                    error.code if error else "tool_execution_failed",
                    error.message if error else "tool execution failed",
                )
            if not isinstance(response.result, dict):
                return _error_result("invalid_result", "tool output must be a JSON object")
            return response.result

    def _validate_catalog_size(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    item.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for item in self._mcp_tools()
                ]
            },
        }
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > self.registry.limits.max_response_bytes:
            raise ValueError(
                "registered MCP tool catalog exceeds max_response_bytes; split the server "
                "or simplify tool schemas"
            )

    def run(self) -> None:
        uvicorn.run(
            self.app,
            host=self.host,
            port=self.port,
            access_log=False,
            server_header=False,
        )


def _error_result(code: str, message: str) -> mcp_types.CallToolResult:
    text = json.dumps({"code": code, "message": message}, ensure_ascii=False, separators=(",", ":"))
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        isError=True,
    )


def serve(
    registry: ServerToolRegistry,
    *,
    bearer_token: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_insecure_remote: bool = False,
) -> None:
    """Serve the registry through the sole public tool interface: ``POST /mcp``."""
    McpToolServer(
        registry,
        bearer_token=bearer_token,
        host=host,
        port=port,
        allow_insecure_remote=allow_insecure_remote,
    ).run()
