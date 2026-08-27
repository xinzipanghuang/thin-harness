"""Production entry point and explicitly registered built-in server tools."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ConfigDict

from .builtin_skills import RuntimeEnvironmentAudit
from .contracts import ExecutableSpec, ProcessResult, ServerLimits
from .long_skill import LongSkillRegistry
from .mcp import serve
from .registry import ServerTool, ServerToolRegistry
from .runtime import ToolExecutionContext
from .task_tools import register_task_tools
from .tasks import DurableTaskStore, LongSkillExecutor


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process: ProcessResult


class RuntimePythonVersion(ServerTool):
    """Report the Python runtime used by the tool server."""

    name = "runtime.python.version"
    version = "1.0.0"
    description = "Return the bounded version of the Python that started this server."
    input_model = NoArguments
    output_model = VersionOutput
    executables = (ExecutableSpec(id="python", name=Path(sys.executable).name),)
    timeout_seconds = 10
    read_only = True
    destructive = False
    idempotent = True

    def run(self, ctx: ToolExecutionContext, arguments: NoArguments) -> VersionOutput:
        process = ctx.run_process("python", ["--version"])
        if process.timed_out or process.returncode != 0:
            raise RuntimeError("Python version probe failed")
        return VersionOutput(process=process)


class SamtoolsVersion(ServerTool):
    """Report samtools when the deployment explicitly enables it."""

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


def build_registry(
    *,
    work_root: str | Path = "server_runs",
    enable_samtools: bool = False,
    retain_run_files: bool = False,
) -> ServerToolRegistry:
    """Build the complete, reviewed tool exposure boundary for this server."""

    registry = ServerToolRegistry(
        work_root=work_root,
        limits=ServerLimits(
            max_request_bytes=64 * 1024,
            max_response_bytes=64 * 1024,
            max_stream_bytes=8 * 1024,
            max_concurrent_runs=2,
        ),
        retain_run_files=retain_run_files,
    )
    registry.register(RuntimePythonVersion())
    if enable_samtools:
        registry.register(SamtoolsVersion())

    long_skills = LongSkillRegistry(registry)
    long_skills.register(RuntimeEnvironmentAudit())
    task_store = DurableTaskStore(
        registry.work_root / "tasks.sqlite3",
        max_result_bytes=48 * 1024,
        max_log_message_bytes=1024,
    )
    executor = LongSkillExecutor(
        long_skills,
        registry,
        task_store,
        worker_count=4,
        autostart=False,
    )
    register_task_tools(registry, long_skills, executor, task_store)
    executor.start()
    # Keep the durable layer inspectable without making it part of MCP transport.
    registry.long_skills = long_skills
    registry.task_store = task_store
    registry.long_skill_executor = executor
    return registry


def _environment_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: 1, 0, true, false, yes, no, on, off")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tool_server",
        description="Run the authenticated thin-harness MCP tool server.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("TOOL_SERVER_HOST", "127.0.0.1"),
        help="listen address (default: TOOL_SERVER_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("TOOL_SERVER_PORT", "8765"),
        help="listen port (default: TOOL_SERVER_PORT or 8765)",
    )
    parser.add_argument(
        "--work-root",
        default=os.environ.get("TOOL_SERVER_WORK_ROOT", "server_runs"),
        help="private per-request working directory root",
    )
    parser.add_argument(
        "--enable-samtools",
        action="store_true",
        default=_environment_flag("TOOL_SERVER_ENABLE_SAMTOOLS"),
        help="register bio.samtools.version (samtools must be in the server Python environment)",
    )
    parser.add_argument(
        "--retain-run-files",
        action="store_true",
        default=_environment_flag("TOOL_SERVER_RETAIN_RUN_FILES"),
        help="retain request directories; requires an external retention policy",
    )
    parser.add_argument(
        "--allow-insecure-remote",
        action="store_true",
        default=_environment_flag("TOOL_SERVER_ALLOW_INSECURE_REMOTE"),
        help="allow plaintext binding outside loopback (not recommended)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    # token = os.environ.get("TOOL_SERVER_TOKEN", "")
    token="test_token_123456789-test_token_123456789"
    if len(token.encode("utf-8")) < 32:
        parser.error("TOOL_SERVER_TOKEN must contain at least 32 UTF-8 bytes")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    registry = build_registry(
        work_root=args.work_root,
        enable_samtools=args.enable_samtools,
        retain_run_files=args.retain_run_files,
    )
    serve(
        registry,
        bearer_token=token,
        host=args.host,
        port=args.port,
        allow_insecure_remote=args.allow_insecure_remote,
    )


__all__ = [
    "RuntimePythonVersion",
    "SamtoolsVersion",
    "build_registry",
    "main",
]
