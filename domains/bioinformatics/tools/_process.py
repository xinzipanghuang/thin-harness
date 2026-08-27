"""Bounded subprocess logging shared by pilot and detached jobs."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable


MAX_LOG_BYTES = 100 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


def _drain(stream, path: Path, limit: int, result: dict, key: str) -> None:
    written = 0
    truncated = False
    with path.open("wb") as handle:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            remaining = limit - written
            if remaining > 0:
                kept = chunk[:remaining]
                handle.write(kept)
                written += len(kept)
            if len(chunk) > max(0, remaining):
                truncated = True
        if truncated:
            handle.write(b"\n[thin-harness: log truncated; process output continued]\n")
    result[key] = truncated


def _stop_process_group(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=10)
    except Exception:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            pass


def run_file_backed_process(
    *,
    executable: str,
    arguments: list[str],
    cwd: str,
    stdout_path: str,
    stderr_path: str,
    timeout_seconds: int,
    on_start: Callable[[int], None] | None = None,
    max_log_bytes: int = MAX_LOG_BYTES,
) -> dict:
    """Run without a shell, continuously draining logs with hard disk caps."""
    process = subprocess.Popen(
        [executable, *arguments],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if on_start is not None:
        on_start(process.pid)
    state = {"stdout_truncated": False, "stderr_truncated": False}
    stdout_thread = threading.Thread(
        target=_drain,
        args=(process.stdout, Path(stdout_path), max_log_bytes, state, "stdout_truncated"),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain,
        args=(process.stderr, Path(stderr_path), max_log_bytes, state, "stderr_truncated"),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=max(1, int(timeout_seconds)))
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process_group(process)
        returncode = process.returncode if process.returncode is not None else -9
    stdout_thread.join(timeout=10)
    stderr_thread.join(timeout=10)
    return {
        "returncode": returncode,
        "timed_out": timed_out,
        **state,
    }
