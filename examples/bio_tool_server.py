"""Minimal explicitly assembled bioinformatics tool server.

Run from the project root after setting a strong token, then connect an MCP
Streamable HTTP client to ``http://127.0.0.1:8765/mcp``:

    TOOL_SERVER_TOKEN=... python examples/bio_tool_server.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_server import (
    ExecutableSpec,
    ProcessResult,
    ServerLimits,
    ServerTool,
    ServerToolRegistry,
    ToolExecutionContext,
    serve,
)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process: ProcessResult


class SamtoolsVersion(ServerTool):
    name = "bio.samtools.version"
    version = "1.0.0"
    description = "Return a bounded samtools version report."
    input_model = NoArguments
    output_model = VersionOutput
    executables = (ExecutableSpec(id="samtools", name="samtools"),)
    timeout_seconds = 10
    read_only = True
    destructive = False
    idempotent = True

    def run(self, ctx: ToolExecutionContext, arguments: NoArguments) -> VersionOutput:
        process = ctx.run_process("samtools", ["--version"])
        if process.timed_out or process.returncode != 0:
            raise RuntimeError("samtools version probe failed")
        return VersionOutput(process=process)


class RuntimePythonVersion(ServerTool):
    name = "runtime.python.version"
    version = "1.0.0"
    description = "Return the bounded version of the Python that started this server."
    input_model = NoArguments
    output_model = VersionOutput
    executables = (
        ExecutableSpec(id="python", name=Path(sys.executable).name),
    )
    timeout_seconds = 10
    read_only = True
    destructive = False
    idempotent = True

    def run(self, ctx: ToolExecutionContext, arguments: NoArguments) -> VersionOutput:
        process = ctx.run_process("python", ["--version"])
        if process.timed_out or process.returncode != 0:
            raise RuntimeError("Python version probe failed")
        return VersionOutput(process=process)


def build_registry() -> ServerToolRegistry:
    registry = ServerToolRegistry(
        work_root=Path.cwd() / "server_runs",
        limits=ServerLimits(
            max_request_bytes=64 * 1024,
            max_response_bytes=64 * 1024,
            max_stream_bytes=8 * 1024,
            max_concurrent_runs=2,
        ),
    )
    # This is the complete exposure boundary. Nothing is auto-discovered.
    registry.register(RuntimePythonVersion())
    # Register only after samtools is installed in this exact server
    # environment (or change its ExecutableSpec to a reviewed absolute path):
    # registry.register(SamtoolsVersion())
    return registry


if __name__ == "__main__":
    token = os.environ.get("TOOL_SERVER_TOKEN", "")
    if not token:
        raise SystemExit("TOOL_SERVER_TOKEN is required")
    serve(build_registry(), bearer_token=token)
