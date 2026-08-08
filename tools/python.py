"""In-process-free Python execution tool (subprocess)."""

import subprocess
import sys

from core.tool import ToolContext, tool


@tool(serial=True)
def run(ctx: ToolContext, code: str, timeout: float = 30.0) -> dict:
    """Execute Python code in a subprocess using the same interpreter.

    Runs in a separate process but with the current user's full privileges:
    this is a local development tool, not a security sandbox. Only execute
    trusted code. stdout/stderr are captured; cwd is the run workdir.

    Args:
        code: Python source to execute (e.g. print(sum(range(10)))).
        timeout: Seconds before the process is killed.
    """
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=ctx.workdir,
    )
    return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
