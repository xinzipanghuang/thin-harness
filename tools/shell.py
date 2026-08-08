"""Shell execution tool."""

import subprocess

from core.tool import ToolContext, tool


@tool
def run(ctx: ToolContext, command: str, timeout: float = 30.0) -> dict:
    """Run a shell command and capture its output.

    Runs through the platform shell (cmd.exe on Windows, /bin/sh elsewhere)
    with the current user's privileges; this is a local development tool, not
    a sandbox. Only execute trusted commands. Large outputs are saved as
    artifacts automatically.

    Args:
        command: Shell command to execute.
        timeout: Seconds before the process is killed.
    """
    proc = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=ctx.workdir,
    )
    return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
