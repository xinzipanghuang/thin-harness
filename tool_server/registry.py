"""Explicit server-tool registration and execution."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from .contracts import (
    ErrorDetail,
    ExecutableSpec,
    RunResponse,
    ServerLimits,
    ServerToolManifest,
    ServerToolSafety,
)
from .runtime import RuntimeEnvironment, ToolExecutionContext


class ServerTool(ABC):
    """Trusted Python definition of one remotely callable tool."""

    name: ClassVar[str]
    version: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    executables: ClassVar[tuple[ExecutableSpec, ...]] = ()
    timeout_seconds: ClassVar[int] = 60
    read_only: ClassVar[bool] = False
    destructive: ClassVar[bool] = True
    idempotent: ClassVar[bool] = False
    open_world: ClassVar[bool] = False

    @abstractmethod
    def run(self, ctx: ToolExecutionContext, arguments: BaseModel) -> BaseModel | dict[str, Any]:
        """Execute validated input. Implementations are trusted server code."""

    def manifest(self, limits: ServerLimits) -> ServerToolManifest:
        timeout = min(max(1, int(self.timeout_seconds)), limits.max_timeout_seconds)
        has_paths = any(spec.path is not None for spec in self.executables)
        has_names = any(spec.name is not None for spec in self.executables)
        runtime = (
            "mixed" if has_paths and has_names
            else "explicit_path" if has_paths
            else "server_python_environment"
        )
        return ServerToolManifest(
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            timeout_seconds=timeout,
            runtime=runtime,
            safety=ServerToolSafety(
                read_only=self.read_only,
                destructive=self.destructive,
                idempotent=self.idempotent,
                open_world=self.open_world,
            ),
        )


class ToolNotFoundError(KeyError):
    pass


class ToolInputError(ValueError):
    pass


class ServerToolRegistry:
    """Registry populated only by direct calls from trusted server startup code."""

    def __init__(
        self,
        *,
        work_root: str | Path,
        limits: ServerLimits | None = None,
        runtime: RuntimeEnvironment | None = None,
        retain_run_files: bool = False,
    ) -> None:
        self.work_root = Path(work_root).expanduser().resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        if not self.work_root.is_dir():
            raise ValueError(f"server work_root is not a directory: {self.work_root}")
        self.limits = limits or ServerLimits()
        self.runtime = runtime or RuntimeEnvironment()
        self.retain_run_files = retain_run_files
        self._tools: dict[str, ServerTool] = {}

    def register(self, tool: ServerTool) -> ServerTool:
        if not isinstance(tool, ServerTool):
            raise TypeError("only ServerTool instances can be registered")
        _require_closed_model(tool.input_model, "input_model")
        _require_closed_model(tool.output_model, "output_model")
        manifest = tool.manifest(self.limits)
        if manifest.safety.read_only and manifest.safety.destructive:
            raise ValueError(
                f"tool {manifest.name} cannot be both read-only and destructive"
            )
        if manifest.name in self._tools:
            raise ValueError(f"server tool already registered: {manifest.name}")
        executable_ids = [spec.id for spec in tool.executables]
        if len(executable_ids) != len(set(executable_ids)):
            raise ValueError(f"duplicate executable id in tool {manifest.name}")
        for executable in tool.executables:
            # Fail closed at startup. An unavailable declared tool is not
            # advertised and then discovered to be broken only at call time.
            self.runtime.resolve(executable)
        self._tools[manifest.name] = tool
        return tool

    def manifests(self) -> list[ServerToolManifest]:
        return [self._tools[name].manifest(self.limits) for name in sorted(self._tools)]

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: int | None = None,
    ) -> RunResponse:
        try:
            tool = self._tools[name]
        except KeyError:
            raise ToolNotFoundError(name) from None
        request_id = "run_" + uuid.uuid4().hex[:16]
        started = time.perf_counter()
        try:
            validated = tool.input_model.model_validate(arguments)
        except Exception as exc:
            raise ToolInputError(_safe_validation_message(exc)) from None
        request_dir = Path(
            tempfile.mkdtemp(prefix=f"{request_id}_", dir=str(self.work_root))
        ).resolve()
        ctx = ToolExecutionContext(
            request_id=request_id,
            workdir=request_dir,
            executables=tool.executables,
            runtime=self.runtime,
            limits=self.limits,
            timeout_seconds=min(tool.timeout_seconds, timeout_seconds)
            if timeout_seconds is not None
            else tool.timeout_seconds,
            cancel_event=cancel_event,
        )
        try:
            value = tool.run(ctx, validated)
            output = tool.output_model.model_validate(value)
            result = output.model_dump(mode="json")
            result_bytes = len(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            response_reserve = min(4096, self.limits.max_response_bytes // 4)
            safe_result_limit = self.limits.max_response_bytes - response_reserve
            if result_bytes > safe_result_limit:
                return RunResponse(
                    request_id=request_id,
                    tool=tool.name,
                    version=tool.version,
                    ok=False,
                    summary=f"{tool.name} produced an oversized result",
                    error=ErrorDetail(
                        code="result_too_large",
                        message="tool must persist large data and return a bounded reference",
                    ),
                    truncated=True,
                    result_bytes=result_bytes,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            return RunResponse(
                request_id=request_id,
                tool=tool.name,
                version=tool.version,
                ok=True,
                summary=f"{tool.name} completed",
                result=result,
                result_bytes=result_bytes,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        except Exception as exc:
            _record_failure(request_dir, exc)
            return RunResponse(
                request_id=request_id,
                tool=tool.name,
                version=tool.version,
                ok=False,
                summary=f"{tool.name} failed",
                error=ErrorDetail(
                    code="tool_execution_failed",
                    message="tool execution failed",
                ),
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        finally:
            if not self.retain_run_files:
                shutil.rmtree(request_dir, ignore_errors=True)


def _require_closed_model(model: type[BaseModel], role: str) -> None:
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError(f"{role} must be a Pydantic BaseModel class")
    if model.model_config.get("extra") != "forbid":
        raise TypeError(f"{role} must set ConfigDict(extra='forbid')")


def _safe_validation_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        compact = [
            {"type": item.get("type"), "loc": item.get("loc"), "msg": item.get("msg")}
            for item in errors(include_url=False)[:20]
        ]
        return json.dumps(compact, ensure_ascii=False, default=str)[:4000]
    return f"invalid tool input: {type(exc).__name__}"


def _record_failure(request_dir: Path, exc: Exception) -> None:
    """Keep bounded local diagnostics without disclosing them over the wire."""
    payload = {
        "type": type(exc).__name__,
        "message": str(exc)[:4000],
    }
    try:
        (request_dir / "error.json").write_text(
            json.dumps(payload, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass
