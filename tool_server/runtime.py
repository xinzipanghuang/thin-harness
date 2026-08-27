"""Safe executable resolution and bounded subprocess execution."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .contracts import ExecutableSpec, ProcessResult, ServerLimits


_CHUNK_BYTES = 64 * 1024
_INHERITED_ENV = {
    "CUDA_VISIBLE_DEVICES",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TZ",
}


class RuntimeEnvironment:
    """Resolve programs from the server Python environment or an explicit path."""

    def __init__(self) -> None:
        # Preserve the environment-facing executable directory even when the
        # Python binary itself is a symlink into a base installation.
        self.python_executable = Path(sys.executable).expanduser().absolute()
        self.prefix = Path(sys.prefix).resolve()
        candidates = [self.python_executable.parent, self.prefix / "bin"]
        self.bin_dirs = tuple(dict.fromkeys(path.resolve() for path in candidates))

    def resolve(self, spec: ExecutableSpec) -> Path:
        if spec.path is not None:
            requested = Path(spec.path).expanduser()
            if not requested.is_absolute():
                raise ValueError(f"explicit executable path must be absolute: {spec.id}")
            executable = requested.resolve(strict=True)
        else:
            executable = self._resolve_environment_name(spec.name or "")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError(f"declared executable is not an executable file: {spec.id}")
        return executable

    def _resolve_environment_name(self, name: str) -> Path:
        if Path(name).name != name:
            raise ValueError("environment executable must be a bare file name")
        for directory in self.bin_dirs:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
        roots = ", ".join(str(path) for path in self.bin_dirs)
        raise ValueError(f"executable {name!r} not found in server Python environment: {roots}")

    def subprocess_environment(self, workdir: Path) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items()
            if key in _INHERITED_ENV or key.startswith("LC_")
        }
        environment["CONDA_PREFIX"] = str(self.prefix)
        environment["HOME"] = str(workdir)
        environment["TMPDIR"] = str(workdir)
        environment["PATH"] = os.pathsep.join(
            [*(str(path) for path in self.bin_dirs), "/usr/bin", "/bin"]
        )
        return environment


class ToolExecutionContext:
    """Per-request execution boundary supplied only to trusted tool code."""

    def __init__(
        self,
        *,
        request_id: str,
        workdir: Path,
        executables: tuple[ExecutableSpec, ...],
        runtime: RuntimeEnvironment,
        limits: ServerLimits,
        timeout_seconds: int,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.request_id = request_id
        self.workdir = workdir.resolve()
        self.runtime = runtime
        self.limits = limits
        self.timeout_seconds = min(timeout_seconds, limits.max_timeout_seconds)
        self.cancel_event = cancel_event
        self._executables = {spec.id: spec for spec in executables}
        if len(self._executables) != len(executables):
            raise ValueError("declared executable ids must be unique")

    def run_process(
        self,
        executable_id: str,
        arguments: list[str] | tuple[str, ...] = (),
    ) -> ProcessResult:
        """Run one declared executable without a shell and with bounded output."""
        try:
            spec = self._executables[executable_id]
        except KeyError:
            raise ValueError(f"tool did not declare executable id {executable_id!r}") from None
        args = [str(value) for value in arguments]
        _validate_arguments(args, self.limits)
        executable = self.runtime.resolve(spec)
        stdout_path = self.workdir / "stdout.log"
        stderr_path = self.workdir / "stderr.log"
        state = {"stdout_truncated": False, "stderr_truncated": False}
        started = time.perf_counter()
        process = subprocess.Popen(
            [str(executable), *args],
            cwd=str(self.workdir),
            env=self.runtime.subprocess_environment(self.workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout_thread = threading.Thread(
            target=_drain,
            args=(process.stdout, stdout_path, self.limits.max_log_bytes, state, "stdout_truncated"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain,
            args=(process.stderr, stderr_path, self.limits.max_log_bytes, state, "stderr_truncated"),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        cancelled = False
        deadline = time.monotonic() + self.timeout_seconds
        while process.poll() is None:
            if self.cancel_event is not None and self.cancel_event.is_set():
                cancelled = True
                _stop_process_group(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _stop_process_group(process)
                break
            try:
                process.wait(timeout=min(0.25, remaining))
            except subprocess.TimeoutExpired:
                pass
        returncode = process.returncode if process.returncode is not None else -9
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        stdout, stdout_preview_truncated = _bounded_text(
            stdout_path, self.limits.max_stream_bytes
        )
        stderr, stderr_preview_truncated = _bounded_text(
            stderr_path, self.limits.max_stream_bytes
        )
        return ProcessResult(
            executable_id=executable_id,
            returncode=returncode,
            timed_out=timed_out,
            cancelled=cancelled,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=state["stdout_truncated"] or stdout_preview_truncated,
            stderr_truncated=state["stderr_truncated"] or stderr_preview_truncated,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )


def _validate_arguments(arguments: list[str], limits: ServerLimits) -> None:
    if len(arguments) > limits.max_argument_count:
        raise ValueError("too many process arguments")
    total = 0
    for value in arguments:
        if "\x00" in value:
            raise ValueError("process argument contains NUL")
        total += len(value.encode("utf-8"))
    if total > limits.max_argument_bytes:
        raise ValueError("process arguments exceed the server byte limit")


def _drain(stream, path: Path, limit: int, state: dict[str, bool], key: str) -> None:
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
    state[key] = truncated


def _bounded_text(path: Path, limit: int) -> tuple[str, bool]:
    size = path.stat().st_size if path.exists() else 0
    with path.open("rb") as handle:
        payload = handle.read(limit)
    return payload.decode("utf-8", errors="replace"), size > limit


def _stop_process_group(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
        except Exception:
            pass
