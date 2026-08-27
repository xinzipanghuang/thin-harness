"""Detached runner for file-backed bioinformatics jobs; not a model tool."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from ._io import atomic_write_json
    from ._process import run_file_backed_process
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from domains.bioinformatics.tools._io import atomic_write_json
    from domains.bioinformatics.tools._process import run_file_backed_process


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output_metadata(paths: list[str]) -> list[dict]:
    records = []
    for value in paths:
        path = Path(value)
        if not path.exists():
            continue
        stat = path.stat()
        records.append({
            "path": str(path.resolve()),
            "type": "file" if path.is_file() else "directory",
            "size_bytes": stat.st_size if path.is_file() else None,
            "mtime_ns": stat.st_mtime_ns,
        })
    return records


def run(manifest_path: Path) -> int:
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"status": "running", "runner_pid": os.getpid(), "started_at": _now()})
    atomic_write_json(manifest_path, manifest)
    timed_out = False
    returncode = -1
    try:
        def record_start(pid: int) -> None:
            manifest["child_pid"] = pid
            atomic_write_json(manifest_path, manifest)

        outcome = run_file_backed_process(
            executable=manifest["executable"],
            arguments=manifest["arguments"],
            cwd=manifest["job_dir"],
            stdout_path=manifest["stdout_path"],
            stderr_path=manifest["stderr_path"],
            timeout_seconds=manifest["timeout_seconds"],
            on_start=record_start,
        )
        returncode = outcome["returncode"]
        timed_out = outcome["timed_out"]
        manifest.update({
            "status": "timed_out" if timed_out else ("succeeded" if returncode == 0 else "failed"),
            "returncode": returncode,
            "timed_out": timed_out,
            "stdout_truncated": outcome["stdout_truncated"],
            "stderr_truncated": outcome["stderr_truncated"],
            "finished_at": _now(),
        })
        missing_outputs = [
            path for path in manifest.get("expected_outputs", []) if not Path(path).exists()
        ]
        if manifest["status"] == "succeeded" and missing_outputs:
            manifest["status"] = "failed"
            manifest["error"] = f"expected output(s) missing: {missing_outputs}"
        manifest["output_metadata"] = _output_metadata(
            manifest.get("expected_outputs", [])
        )
    except BaseException as exc:
        manifest.update({
            "status": "failed",
            "returncode": returncode,
            "finished_at": _now(),
            "error": f"{type(exc).__name__}: {exc}",
        })
    atomic_write_json(manifest_path, manifest)
    return 0 if manifest["status"] == "succeeded" else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m domains.bioinformatics.tools._job_runner MANIFEST")
    raise SystemExit(run(Path(sys.argv[1])))
