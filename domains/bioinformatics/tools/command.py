"""Structured execution of local bioinformatics command-line programs."""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.tool import ToolContext, ToolResult, tool


@tool(name="bio.command.run", serial=True)
def run_command(
    ctx: ToolContext,
    program: str,
    arguments: list[str],
    timeout: float = 120.0,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
) -> ToolResult:
    """Run one local bioinformatics program without a shell.

    Args:
        program: Executable name or absolute path, such as samtools.
        arguments: Argument vector passed directly to the executable.
        timeout: Maximum execution time in seconds.
        inputs: Input files used by the command, for provenance.
        outputs: Output files expected from the command, for provenance.
    """
    cwd = str(Path(ctx.workdir).resolve()) if ctx.workdir else None
    command = [program, *[str(item) for item in arguments]]
    try:
        version_probe = subprocess.run(
            [program, "--version"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=min(5.0, max(1.0, float(timeout))),
            check=False,
        )
        version_text = (version_probe.stdout or version_probe.stderr).splitlines()
        tool_version = version_text[0].strip() if version_text else "unknown"
    except Exception:
        tool_version = "unknown"

    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=max(1.0, float(timeout)),
        check=False,
    )
    command_text = subprocess.list2cmdline(command)
    def provenance_paths(values: list[str] | None) -> list[str]:
        resolved: list[str] = []
        for value in values or []:
            candidate = Path(value)
            if cwd and not candidate.is_absolute():
                candidate = Path(cwd) / candidate
            resolved.append(str(candidate.resolve()))
        return resolved

    input_paths = provenance_paths(inputs)
    output_paths = provenance_paths(outputs)
    return ToolResult(
        ok=completed.returncode == 0,
        summary=f"{program} exited with code {completed.returncode}",
        data={
            "command": command_text,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
        error=completed.stderr.strip() if completed.returncode else None,
        provenance={
            "tool": program,
            "tool_version": tool_version,
            "command": command_text,
            "workdir": cwd or "",
            "inputs": input_paths,
            "outputs": output_paths,
        },
    )
