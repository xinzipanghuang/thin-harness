"""Wire contracts for the explicitly assembled remote tool server."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


_IDENTIFIER = r"^[a-zA-Z][a-zA-Z0-9_.-]*$"
_VERSION = r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][a-zA-Z0-9.-]+)?$"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutableSpec(ClosedModel):
    """One executable fixed by trusted server code, never by a request."""

    id: str = Field(pattern=_IDENTIFIER)
    name: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.+-]+$")
    path: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if (self.name is None) == (self.path is None):
            raise ValueError("executable must define exactly one of name or path")


class ServerToolSafety(ClosedModel):
    """MCP safety hints fixed by trusted tool code, never by a request."""

    read_only: bool = False
    destructive: bool = True
    idempotent: bool = False
    open_world: bool = False


class ServerToolManifest(ClosedModel):
    """Safe discovery document exposed to clients."""

    name: str = Field(pattern=_IDENTIFIER)
    version: str = Field(pattern=_VERSION)
    description: str = Field(min_length=1, max_length=4000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    execution: Literal["sync"] = "sync"
    timeout_seconds: int = Field(gt=0)
    runtime: Literal["server_python_environment", "explicit_path", "mixed"]
    safety: ServerToolSafety


class ErrorDetail(ClosedModel):
    code: str
    message: str


class RunResponse(ClosedModel):
    request_id: str
    tool: str
    version: str
    ok: bool
    summary: str
    result: Any = None
    error: ErrorDetail | None = None
    truncated: bool = False
    result_bytes: int | None = Field(default=None, ge=0)
    duration_ms: float = Field(ge=0)


class ProcessResult(ClosedModel):
    executable_id: str
    returncode: int
    timed_out: bool
    cancelled: bool = False
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: float = Field(ge=0)


class ServerLimits(ClosedModel):
    """Hard server-side limits; requests cannot override these values."""

    max_request_bytes: int = Field(default=256 * 1024, ge=1024, le=16 * 1024 * 1024)
    max_response_bytes: int = Field(default=256 * 1024, ge=1024, le=16 * 1024 * 1024)
    max_stream_bytes: int = Field(default=16 * 1024, ge=1024, le=1024 * 1024)
    max_log_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    max_argument_count: int = Field(default=256, ge=1, le=4096)
    max_argument_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)
    max_concurrent_runs: int = Field(default=4, ge=1, le=256)
    max_timeout_seconds: int = Field(default=3600, ge=1, le=7 * 24 * 60 * 60)
