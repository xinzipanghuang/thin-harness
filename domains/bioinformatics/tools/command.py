"""Pilot-first, file-backed execution of bioinformatics programs."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.tool import ToolContext, ToolResult, clamp_int, tool

from ._io import (
    BoundedTextReader,
    DEFAULT_SCAN_BYTES,
    atomic_write_json,
    bounded_head,
    is_gzip,
    output_root,
    resolve_path,
    safe_text,
    sidecar_indexes,
)
from ._process import run_file_backed_process


ALLOWED_PROGRAMS = {
    "STAR", "bamtools", "bcftools", "bedtools", "bgzip", "bowtie2", "bwa",
    "fastp", "fastqc", "featureCounts", "gatk", "hisat2", "kallisto", "minimap2",
    "multiqc", "salmon", "samtools", "seqkit", "tabix",
}
_JOB_ID = re.compile(r"^(?:plan|pilot|job)_[a-f0-9]{12}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_program(program: str) -> Path:
    requested = Path(program).expanduser()
    if requested.is_absolute() or requested.parent != Path("."):
        raise ValueError(
            "program must be an allowlisted executable name resolved from PATH, not a path"
        )
    name = requested.name
    if name not in ALLOWED_PROGRAMS:
        raise ValueError(
            f"Program {name!r} is not allowlisted; allowed: {', '.join(sorted(ALLOWED_PROGRAMS))}"
        )
    found = shutil.which(program)
    if not found:
        raise ValueError(f"Executable not found: {program}")
    executable = Path(found).resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"Not an executable file: {executable}")
    return executable


def _input_metadata(ctx: ToolContext, values: list[str]) -> list[dict]:
    if not values:
        raise ValueError("At least one declared input file is required")
    if len(values) > 100:
        raise ValueError("At most 100 input files may be declared")
    records = []
    for value in values:
        path = resolve_path(ctx, value)
        stat = path.stat()
        records.append({
            "path": str(path),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "compressed": is_gzip(path),
            "indexes": sidecar_indexes(path),
            "bounded_head": bounded_head(path, max_bytes=4096, max_lines=2),
        })
    return records


def _assert_inputs_unchanged(records: list[dict]) -> None:
    for item in records:
        path = Path(item["path"])
        if not path.is_file():
            raise ValueError(f"Pilot input disappeared: {path}")
        stat = path.stat()
        if stat.st_size != item["size_bytes"] or stat.st_mtime_ns != item["mtime_ns"]:
            raise ValueError(f"Pilot input changed after planning: {path}")


def _compact_inputs(records: list[dict], limit: int = 10) -> list[dict]:
    return [
        {
            "path": item["path"],
            "size_bytes": item["size_bytes"],
            "compressed": item["compressed"],
            "first_indexes": item["indexes"][:5],
        }
        for item in records[:limit]
    ]


def _provenance_inputs(records: list[dict]) -> list[dict]:
    return [
        {key: item[key] for key in ("path", "size_bytes", "mtime_ns")}
        for item in records
    ]


def _format_key(path: Path) -> str:
    suffixes = [value.lower() for value in path.suffixes]
    if suffixes and suffixes[-1] in {".gz", ".bgz", ".bgzf"}:
        suffixes = suffixes[:-1]
    return suffixes[-1] if suffixes else ""


def _explicit_region(arguments: list[str]) -> bool:
    region_flags = {"-r", "--regions", "-R", "--regions-file", "-L", "--targets"}
    for index, value in enumerate(arguments):
        if value in region_flags and index + 1 < len(arguments):
            return True
        if re.fullmatch(r"[^/\s:]+:\d+(?:-\d+)?", str(value)):
            return True
    return False


def _write_pilot_slice(source: Path, target: Path, kind: str) -> tuple[int, int]:
    """Write complete text records only, capped at 1000 records / 4 MiB."""
    record_limit = 1000
    byte_limit = DEFAULT_SCAN_BYTES
    payload = bytearray()
    records = 0
    with BoundedTextReader(source, max_bytes=byte_limit) as reader:
        if kind == "fasta":
            current = bytearray()
            for raw in reader:
                encoded = raw.encode("utf-8")
                if raw.startswith(">") and current:
                    if len(payload) + len(current) > byte_limit:
                        break
                    payload.extend(current)
                    records += 1
                    if records >= record_limit:
                        break
                    current = bytearray()
                current.extend(encoded)
            if current and not reader.truncated and records < record_limit:
                if len(payload) + len(current) <= byte_limit:
                    payload.extend(current)
                    records += 1
        elif kind == "fastq":
            while records < record_limit:
                parts = [reader.readline() for _ in range(4)]
                if not parts[0]:
                    break
                if any(not part for part in parts[1:]):
                    if reader.truncated:
                        break
                    raise ValueError(f"Malformed FASTQ while creating pilot slice: {source}")
                record = "".join(parts).encode("utf-8")
                if len(payload) + len(record) > byte_limit:
                    break
                payload.extend(record)
                records += 1
        elif kind == "vcf":
            for raw in reader:
                encoded = raw.encode("utf-8")
                is_record = not raw.startswith("#") and bool(raw.strip())
                if len(payload) + len(encoded) > byte_limit:
                    break
                payload.extend(encoded)
                if is_record:
                    records += 1
                    if records >= record_limit:
                        break
        else:
            raise ValueError(f"Unsupported automatic pilot slice format: {kind}")
    if records == 0:
        raise ValueError(
            f"Could not create a complete bounded {kind.upper()} pilot record from {source}"
        )
    temporary = target.with_name(target.name + ".partial")
    temporary.write_bytes(payload)
    os.replace(temporary, target)
    return records, len(payload)


def _prepare_pilot_inputs(
    ctx: ToolContext,
    originals: list[dict],
    directory: Path,
    arguments: list[str],
) -> tuple[list[dict], list[dict]]:
    pilot_records: list[dict] = []
    slice_info: list[dict] = []
    for index, item in enumerate(originals):
        source = Path(item["path"])
        suffix = _format_key(source)
        kind = {
            ".fa": "fasta", ".fasta": "fasta", ".fna": "fasta",
            ".fq": "fastq", ".fastq": "fastq", ".vcf": "vcf",
        }.get(suffix)
        if kind:
            extension = {"fasta": ".fa", "fastq": ".fastq", "vcf": ".vcf"}[kind]
            target = directory / f"pilot_input_{index}{extension}"
            records, size = _write_pilot_slice(source, target, kind)
            stat = target.stat()
            pilot_records.append({
                "path": str(target.resolve()), "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns, "compressed": False,
                "indexes": [], "bounded_head": bounded_head(target, 4096, 2),
            })
            slice_info.append({
                "source": str(source), "pilot_path": str(target.resolve()),
                "format": kind.upper(), "records": records, "bytes": size,
            })
            continue
        if suffix in {".bam", ".cram", ".bcf"}:
            if not item["indexes"]:
                raise ValueError(f"Indexed region pilot required, but no index was found for {source}")
            if not _explicit_region(arguments):
                raise ValueError(
                    f"Binary input {source.name} requires an explicit region argument for the pilot"
                )
            pilot_records.append(item)
            slice_info.append({
                "source": str(source), "pilot_path": str(source),
                "format": suffix[1:].upper(), "mode": "indexed_region",
            })
            continue
        raise ValueError(
            f"No safe automatic pilot strategy for {source}; supported text formats are "
            "FASTA/FASTQ/VCF, while BAM/CRAM/BCF require an index and explicit region"
        )
    return pilot_records, slice_info


def _output_paths(directory: Path, names: list[str] | None) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    reserved = {
        "manifest.json", "stdout.log", "stderr.log",
        "version.stdout.log", "version.stderr.log",
    }
    for name in names or []:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.name:
            raise ValueError(f"Output must be a safe path relative to the job directory: {name!r}")
        if relative.name in reserved or relative.name.startswith("pilot_input_"):
            raise ValueError(f"Output name is reserved by the job harness: {name!r}")
        target = (directory / relative).resolve()
        if directory.resolve() not in target.parents:
            raise ValueError(f"Output escapes the job directory: {name!r}")
        if target.exists():
            raise ValueError(f"Refusing to overwrite existing output: {target}")
        if str(target) in seen:
            raise ValueError(f"Duplicate expected output: {target}")
        seen.add(str(target))
        paths.append(str(target))
    return paths


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


def _expand_arguments(arguments: list[str], inputs: list[dict], directory: Path) -> list[str]:
    if len(arguments) > 500:
        raise ValueError("At most 500 command arguments are allowed")
    expanded: list[str] = []
    for raw in arguments:
        value = str(raw).replace("{output_dir}", str(directory.resolve()))
        for index, item in enumerate(inputs):
            value = value.replace(f"{{input{index}}}", item["path"])
        if "{input" in value or "{output_dir}" in value:
            raise ValueError(f"Unresolved command placeholder: {raw!r}")
        expanded.append(value)

    input_paths = {item["path"] for item in inputs}
    root = directory.resolve()
    for value in expanded:
        candidate_text = value.split("=", 1)[1] if "=" in value else value
        candidate = Path(candidate_text).expanduser()
        if not candidate.is_absolute():
            continue
        resolved = str(candidate.resolve(strict=False))
        if resolved in input_paths:
            continue
        resolved_path = Path(resolved)
        if root != resolved_path and root not in resolved_path.parents:
            raise ValueError(
                "Absolute command paths must be declared inputs or live under {output_dir}: "
                f"{candidate_text}"
            )
    return expanded


def _jobs_root(ctx: ToolContext) -> Path:
    root = output_root(ctx) / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _manifest_for(ctx: ToolContext, job_id: str) -> Path:
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError(f"Invalid job id: {job_id!r}")
    path = (_jobs_root(ctx) / job_id / "manifest.json").resolve()
    if _jobs_root(ctx).resolve() not in path.parents:
        raise ValueError("Job path escaped the jobs root")
    return path


def _read_manifest(ctx: ToolContext, job_id: str) -> tuple[Path, dict]:
    path = _manifest_for(ctx, job_id)
    if not path.is_file():
        raise ValueError(f"Unknown job: {job_id}")
    return path, json.loads(path.read_text(encoding="utf-8"))


def _read_log_fragment(path: str, *, tail: bool, max_lines: int = 20, max_chars: int = 4000) -> list[str]:
    source = Path(path)
    if not source.is_file():
        return []
    # Job logs are bounded by disk, not model context. Reading the tail avoids
    # loading a potentially huge log into memory.
    if tail:
        block = 8192
        chunks: list[bytes] = []
        remaining = max_chars * 4
        with source.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            while position > 0 and remaining > 0:
                size = min(block, position, remaining)
                position -= size
                handle.seek(position)
                chunks.append(handle.read(size))
                remaining -= size
        text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        lines = text.splitlines()[-max_lines:]
    else:
        lines = []
        chars = 0
        with source.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                lines.append(raw.rstrip("\r\n"))
                chars += len(raw)
                if len(lines) >= max_lines or chars >= max_chars:
                    break
    return [safe_text(line, max(80, max_chars // max(1, max_lines))) for line in lines]


def _compact_manifest(manifest: dict, *, include_logs: bool = True) -> dict:
    compact = {
        key: manifest.get(key)
        for key in (
            "job_id", "phase", "status", "program", "tool_version", "command", "pilot_scope",
            "created_at", "started_at", "finished_at", "returncode", "timed_out",
            "stdout_truncated", "stderr_truncated",
            "manifest_path", "job_dir", "stdout_path", "stderr_path",
            "pilot_job_id", "error",
        )
        if manifest.get(key) is not None
    }
    if "command" in compact:
        compact["command_preview"] = safe_text(compact.pop("command"), 500)
    outputs = list(manifest.get("expected_outputs") or [])
    compact["output_count"] = len(outputs)
    compact["first_outputs"] = outputs[:10]
    output_records = list(manifest.get("output_metadata") or [])
    if output_records:
        compact["first_output_metadata"] = output_records[:10]
    if include_logs:
        compact["stdout_head"] = _read_log_fragment(manifest["stdout_path"], tail=False)
        compact["stderr_tail"] = _read_log_fragment(manifest["stderr_path"], tail=True)
    return compact


def _probe_version(executable: Path, directory: Path) -> str:
    stdout_path = directory / "version.stdout.log"
    stderr_path = directory / "version.stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            [str(executable), "--version"],
            cwd=str(directory),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
    lines = _read_log_fragment(str(stdout_path), tail=False, max_lines=1, max_chars=500)
    if not lines:
        lines = _read_log_fragment(str(stderr_path), tail=False, max_lines=1, max_chars=500)
    return lines[0] if lines else "unknown"


@tool(name="bio.command.plan", serial=True)
def plan_command(
    ctx: ToolContext,
    program: str,
    arguments: list[str],
    inputs: list[str],
    pilot_scope: str,
    expected_outputs: list[str] | None = None,
) -> ToolResult:
    """Create a pilot plan for an allowlisted program without running the workload.

    Use ``{input0}``, ``{input1}``, ... for declared inputs and
    ``{output_dir}`` for every absolute output path. Only a bounded five-second
    ``--version`` probe is run; the planned workload is not.

    Args:
        program: Allowlisted bioinformatics executable.
        arguments: Pilot-sized argument vector, using input/output placeholders.
        inputs: All input files used by the command.
        pilot_scope: Exact small scope, such as 1000 reads or chr1:1-100000.
        expected_outputs: Relative output names expected inside the pilot directory.
    """
    if not pilot_scope.strip():
        raise ValueError("pilot_scope is required")
    executable = _resolve_program(program)
    input_records = _input_metadata(ctx, inputs)
    plan_id = "plan_" + uuid.uuid4().hex[:12]
    directory = _jobs_root(ctx) / plan_id
    directory.mkdir(parents=True, exist_ok=False)
    try:
        outputs = _output_paths(directory, expected_outputs)
        pilot_inputs, pilot_slices = _prepare_pilot_inputs(
            ctx, input_records, directory, arguments
        )
        expanded = _expand_arguments(arguments, pilot_inputs, directory)
        tool_version = _probe_version(executable, directory)
    except Exception:
        # This directory was created by this call and has never hosted a job.
        shutil.rmtree(directory, ignore_errors=True)
        raise
    manifest_path = directory / "manifest.json"
    manifest = {
        "job_id": plan_id,
        "phase": "plan",
        "status": "planned",
        "program": executable.name,
        "tool_version": tool_version,
        "executable": str(executable),
        "argument_templates": [str(value) for value in arguments],
        "arguments": expanded,
        "command": subprocess.list2cmdline([str(executable), *expanded]),
        "pilot_scope": safe_text(pilot_scope, 500),
        "inputs": pilot_inputs,
        "original_inputs": input_records,
        "pilot_slices": pilot_slices,
        "expected_outputs": outputs,
        "expected_output_names": [str(value) for value in (expected_outputs or [])],
        "job_dir": str(directory.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "stdout_path": str((directory / "stdout.log").resolve()),
        "stderr_path": str((directory / "stderr.log").resolve()),
        "created_at": _now(),
    }
    atomic_write_json(manifest_path, manifest)
    compact = _compact_manifest(manifest, include_logs=False)
    compact["input_count"] = len(input_records)
    compact["first_inputs"] = _compact_inputs(input_records)
    compact["pilot_slices"] = pilot_slices[:10]
    compact["tool_version"] = tool_version
    compact["next_action"] = f"Run bio.command.pilot with plan_id={plan_id}"
    return ToolResult(
        ok=True,
        summary=f"Planned bounded pilot {plan_id}; workload not executed",
        data=compact,
        preview=json.dumps(compact, ensure_ascii=False, default=str),
        provenance={"operation": "command_plan", "manifest": str(manifest_path.resolve())},
    )


def _kill_process_group(process: subprocess.Popen) -> None:
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
        except Exception:
            pass


@tool(name="bio.command.pilot", serial=True)
def run_pilot(ctx: ToolContext, plan_id: str, timeout: int = 60) -> ToolResult:
    """Execute one previously planned small pilot, with logs written to files.

    Args:
        plan_id: Identifier returned by bio.command.plan.
        timeout: Hard pilot timeout in seconds (1-60).
    """
    manifest_path, manifest = _read_manifest(ctx, plan_id)
    if manifest.get("phase") != "plan" or manifest.get("status") != "planned":
        raise ValueError("Pilot requires an unexecuted bio.command.plan manifest")
    _assert_inputs_unchanged(manifest["original_inputs"])
    _assert_inputs_unchanged(manifest["inputs"])
    pilot_id = "pilot_" + plan_id.removeprefix("plan_")
    pilot_dir = _jobs_root(ctx) / pilot_id
    if pilot_dir.exists():
        raise ValueError(f"Pilot output already exists: {pilot_dir}")
    pilot_dir.mkdir(parents=True)
    arguments = _expand_arguments(
        manifest["argument_templates"], manifest["inputs"], pilot_dir
    )
    expected_outputs = _output_paths(
        pilot_dir, manifest.get("expected_output_names")
    )
    pilot_manifest_path = pilot_dir / "manifest.json"
    pilot = {
        **manifest,
        "job_id": pilot_id,
        "phase": "pilot",
        "status": "running",
        "arguments": arguments,
        "command": subprocess.list2cmdline([manifest["executable"], *arguments]),
        "expected_outputs": expected_outputs,
        "job_dir": str(pilot_dir.resolve()),
        "manifest_path": str(pilot_manifest_path.resolve()),
        "stdout_path": str((pilot_dir / "stdout.log").resolve()),
        "stderr_path": str((pilot_dir / "stderr.log").resolve()),
        "started_at": _now(),
        "timeout_seconds": clamp_int(timeout, 1, 60, 60),
    }
    atomic_write_json(pilot_manifest_path, pilot)
    timed_out = False
    def record_start(pid: int) -> None:
        pilot["child_pid"] = pid
        atomic_write_json(pilot_manifest_path, pilot)

    try:
        outcome = run_file_backed_process(
            executable=pilot["executable"],
            arguments=arguments,
            cwd=pilot["job_dir"],
            stdout_path=pilot["stdout_path"],
            stderr_path=pilot["stderr_path"],
            timeout_seconds=pilot["timeout_seconds"],
            on_start=record_start,
        )
    except Exception as exc:
        pilot.update({
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": _now(),
        })
        atomic_write_json(pilot_manifest_path, pilot)
        compact = _compact_manifest(pilot)
        return ToolResult(
            ok=False,
            summary=f"Pilot {pilot_id} failed to start; details saved under {pilot_dir}",
            data=compact,
            preview=json.dumps(compact, ensure_ascii=False, default=str),
            error=pilot["error"],
            provenance={"operation": "command_pilot", "manifest": str(pilot_manifest_path)},
        )
    returncode = outcome["returncode"]
    timed_out = outcome["timed_out"]
    pilot.update({
        "status": "timed_out" if timed_out else ("succeeded" if returncode == 0 else "failed"),
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_truncated": outcome["stdout_truncated"],
        "stderr_truncated": outcome["stderr_truncated"],
        "finished_at": _now(),
    })
    missing_outputs = [path for path in expected_outputs if not Path(path).exists()]
    if pilot["status"] == "succeeded" and missing_outputs:
        pilot["status"] = "failed"
        pilot["error"] = f"expected output(s) missing: {missing_outputs}"
    pilot["output_metadata"] = _output_metadata(expected_outputs)
    atomic_write_json(pilot_manifest_path, pilot)
    compact = _compact_manifest(pilot)
    return ToolResult(
        ok=pilot["status"] == "succeeded",
        summary=f"Pilot {pilot_id} {pilot['status']}; logs saved under {pilot_dir}",
        data=compact,
        preview=json.dumps(compact, ensure_ascii=False, default=str),
        error=(f"pilot {pilot['status']}; inspect {pilot['stderr_path']}" if pilot["status"] != "succeeded" else None),
        timed_out=timed_out,
        provenance={
            "operation": "command_pilot", "command": pilot["command"],
            "manifest": str(pilot_manifest_path.resolve()),
            "inputs": _provenance_inputs(pilot["original_inputs"]),
            "pilot_inputs": _provenance_inputs(pilot["inputs"]),
            "outputs": expected_outputs,
        },
    )


@tool(name="bio.job.submit", serial=True)
def submit_job(
    ctx: ToolContext,
    pilot_job_id: str,
    arguments: list[str],
    inputs: list[str],
    expected_outputs: list[str] | None = None,
    timeout_hours: int = 24,
) -> ToolResult:
    """Submit a background full-scale job only after a successful pilot.

    The executable and declared inputs must match the successful pilot. Logs
    and products remain in the new job directory; this call returns immediately.

    Args:
        pilot_job_id: Successful pilot id returned by bio.command.pilot.
        arguments: Full-scale arguments using {inputN} and {output_dir} placeholders.
        inputs: Same declared inputs used by the pilot.
        expected_outputs: Relative output names expected in the job directory.
        timeout_hours: Background job timeout in hours (1-168).
    """
    _, pilot = _read_manifest(ctx, pilot_job_id)
    if pilot.get("phase") != "pilot" or pilot.get("status") != "succeeded":
        raise ValueError("A successful pilot is required before full-scale submission")
    input_records = _input_metadata(ctx, inputs)
    input_identity = [(item["path"], item["size_bytes"], item["mtime_ns"]) for item in input_records]
    pilot_identity = [
        (item["path"], item["size_bytes"], item["mtime_ns"])
        for item in pilot["original_inputs"]
    ]
    if input_identity != pilot_identity:
        raise ValueError("Full-scale inputs must match the pilot paths, sizes, and mtimes")
    job_id = "job_" + uuid.uuid4().hex[:12]
    directory = _jobs_root(ctx) / job_id
    outputs = _output_paths(directory, expected_outputs)
    expanded = _expand_arguments(arguments, input_records, directory)
    directory.mkdir(parents=True, exist_ok=False)
    manifest_path = directory / "manifest.json"
    manifest = {
        "job_id": job_id,
        "phase": "full",
        "status": "queued",
        "program": pilot["program"],
        "executable": pilot["executable"],
        "arguments": expanded,
        "command": subprocess.list2cmdline([pilot["executable"], *expanded]),
        "inputs": input_records,
        "expected_outputs": outputs,
        "pilot_job_id": pilot_job_id,
        "job_dir": str(directory.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "stdout_path": str((directory / "stdout.log").resolve()),
        "stderr_path": str((directory / "stderr.log").resolve()),
        "timeout_seconds": clamp_int(timeout_hours, 1, 168, 24) * 3600,
        "created_at": _now(),
    }
    atomic_write_json(manifest_path, manifest)
    runner_script = Path(__file__).with_name("_job_runner.py").resolve()
    runner = subprocess.Popen(
        [sys.executable, str(runner_script), str(manifest_path.resolve())],
        cwd=str(Path(ctx.workdir).resolve()) if ctx.workdir else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    manifest["runner_pid"] = runner.pid
    manifest["status"] = "starting"
    # The detached runner owns manifest updates after launch. Writing here can
    # race with its transition to ``running`` and move status backwards.
    compact = _compact_manifest(manifest, include_logs=False)
    compact["next_action"] = f"Poll with bio.job.status job_id={job_id}"
    return ToolResult(
        ok=True,
        summary=f"Submitted background job {job_id}; monitor it instead of waiting",
        data=compact,
        preview=json.dumps(compact, ensure_ascii=False, default=str),
        provenance={
            "operation": "background_submit", "command": manifest["command"],
            "manifest": str(manifest_path.resolve()),
            "inputs": _provenance_inputs(input_records),
            "outputs": outputs,
        },
    )


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _age_seconds(timestamp: str | None) -> float:
    try:
        value = datetime.fromisoformat(str(timestamp))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - value).total_seconds())
    except (TypeError, ValueError):
        return 999999.0


@tool(name="bio.job.status", cacheable=False)
def job_status(ctx: ToolContext, job_id: str) -> ToolResult:
    """Read a background job manifest and return compact status plus log head/tail."""
    manifest_path, manifest = _read_manifest(ctx, job_id)
    if manifest.get("status") in {"queued", "starting", "running"}:
        launch_grace = (
            manifest.get("status") in {"queued", "starting"}
            and _age_seconds(manifest.get("created_at")) < 10
        )
        if not launch_grace and not _pid_alive(manifest.get("runner_pid")) and not _pid_alive(manifest.get("child_pid")):
            manifest["status"] = "orphaned"
            manifest["error"] = "job processes are no longer running"
            manifest["finished_at"] = _now()
            atomic_write_json(manifest_path, manifest)
    compact = _compact_manifest(manifest)
    return ToolResult(
        ok=manifest.get("status") not in {"failed", "timed_out", "orphaned", "cancelled"},
        summary=f"Job {job_id}: {manifest.get('status', 'unknown')}",
        data=compact,
        preview=json.dumps(compact, ensure_ascii=False, default=str),
        error=manifest.get("error"),
        provenance={"operation": "job_status", "manifest": str(manifest_path)},
    )


@tool(name="bio.job.tail", cacheable=False)
def job_tail(
    ctx: ToolContext,
    job_id: str,
    stream: str = "stderr",
    max_lines: int = 20,
    max_chars: int = 4000,
) -> ToolResult:
    """Return a strictly bounded, sanitized tail from one job log.

    Args:
        job_id: Job identifier.
        stream: stderr or stdout.
        max_lines: Maximum lines returned (1-50).
        max_chars: Approximate maximum returned characters (200-8000).
    """
    _, manifest = _read_manifest(ctx, job_id)
    if stream not in {"stdout", "stderr"}:
        raise ValueError("stream must be stdout or stderr")
    line_limit = clamp_int(max_lines, 1, 50, 20)
    char_limit = clamp_int(max_chars, 200, 8000, 4000)
    path = manifest[f"{stream}_path"]
    lines = _read_log_fragment(path, tail=True, max_lines=line_limit, max_chars=char_limit)
    data = {"job_id": job_id, "stream": stream, "path": path, "tail": lines, "truncated": True}
    return ToolResult(
        ok=True,
        summary=f"Read bounded {stream} tail for {job_id} from {path}",
        data=data,
        preview=json.dumps(data, ensure_ascii=False, default=str),
        truncated=True,
        provenance={"operation": "bounded_log_tail", "source": path},
    )


@tool(name="bio.job.cancel", serial=True)
def cancel_job(ctx: ToolContext, job_id: str, confirm_job_id: str) -> ToolResult:
    """Cancel a running job only when the exact id is repeated as confirmation.

    Only call this after the user explicitly requests cancellation.

    Args:
        job_id: Running job identifier.
        confirm_job_id: Must exactly repeat job_id.
    """
    if confirm_job_id != job_id:
        raise ValueError("confirm_job_id must exactly match job_id")
    manifest_path, manifest = _read_manifest(ctx, job_id)
    if manifest.get("status") not in {"queued", "starting", "running"}:
        raise ValueError(f"Job is not running: {manifest.get('status')}")
    for key in ("child_pid", "runner_pid"):
        pid = manifest.get(key)
        if not _pid_alive(pid):
            continue
        try:
            if os.name == "posix":
                os.killpg(int(pid), signal.SIGTERM)
            else:
                os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.1)
    manifest.update({"status": "cancelled", "finished_at": _now(), "error": "cancelled by request"})
    atomic_write_json(manifest_path, manifest)
    compact = _compact_manifest(manifest, include_logs=False)
    return ToolResult(
        ok=True,
        summary=f"Cancelled job {job_id}",
        data=compact,
        preview=json.dumps(compact, ensure_ascii=False, default=str),
        provenance={"operation": "job_cancel", "manifest": str(manifest_path)},
    )
