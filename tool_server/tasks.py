"""SQLite-backed durable task state and a fixed long-skill workflow executor."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .long_skill import LongSkill, LongSkillRegistry, resolve_value
from .registry import ServerToolRegistry


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"queued", "running"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TaskNotFoundError(KeyError):
    pass


class TaskConflictError(ValueError):
    pass


class DurableTaskStore:
    """Thread-safe SQLite persistence for task state, checkpoints, and logs."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_result_bytes: int = 256 * 1024,
        max_log_entries: int = 2000,
        max_log_message_bytes: int = 4096,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_result_bytes = max_result_bytes
        self.max_log_entries = max_log_entries
        self.max_log_message_bytes = max_log_message_bytes
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    skill_id TEXT NOT NULL,
                    skill_version TEXT NOT NULL,
                    contract_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_message TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT,
                    error_json TEXT,
                    current_step TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status, created_at);
                CREATE TABLE IF NOT EXISTS task_steps (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    output_json TEXT,
                    error_json TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    PRIMARY KEY(task_id, step_id)
                );
                CREATE TABLE IF NOT EXISTS task_logs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_logs_idx ON task_logs(task_id, sequence);
                """
            )

    def create(
        self,
        skill: LongSkill,
        arguments: dict[str, Any],
        *,
        contract_hash: str,
    ) -> dict[str, Any]:
        task_id = "task_" + uuid.uuid4().hex
        created = _now()
        deadline = (
            datetime.now(timezone.utc) + timedelta(seconds=skill.timeout_seconds)
        ).isoformat(timespec="milliseconds")
        input_json = _bounded_json(arguments, self.max_result_bytes, "task input")
        with self._connect() as db:
            db.execute(
                """INSERT INTO tasks(
                    task_id, skill_id, skill_version, contract_hash, status,
                    status_message, input_json, created_at, updated_at, deadline_at
                ) VALUES (?, ?, ?, ?, 'queued', 'Task accepted', ?, ?, ?, ?)""",
                (
                    task_id,
                    skill.id,
                    skill.version,
                    contract_hash,
                    input_json,
                    created,
                    created,
                    deadline,
                ),
            )
        self.append_log(task_id, "info", "Task accepted")
        return self.get(task_id)

    def get(self, task_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return _decode_task(row)

    def recoverable_ids(self) -> list[str]:
        with self._connect() as db:
            db.execute(
                """UPDATE tasks SET status = 'cancelled', status_message = ?, updated_at = ?
                   WHERE status IN ('queued', 'running') AND cancel_requested = 1""",
                ("Task cancelled during restart recovery", _now()),
            )
            rows = db.execute(
                """SELECT task_id FROM tasks WHERE status IN ('queued', 'running')
                   AND cancel_requested = 0 ORDER BY created_at"""
            ).fetchall()
            db.execute(
                """UPDATE tasks SET status = 'queued', status_message = ?, updated_at = ?
                   WHERE status = 'running' AND cancel_requested = 0""",
                ("Recovered after server restart", _now()),
            )
        return [str(row["task_id"]) for row in rows]

    def completed_steps(self, task_id: str) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT step_id, output_json FROM task_steps WHERE task_id = ? AND status = 'completed'",
                (task_id,),
            ).fetchall()
        return {str(row["step_id"]): json.loads(row["output_json"]) for row in rows}

    def step_attempts(self, task_id: str, step_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT attempts FROM task_steps WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            ).fetchone()
        return int(row["attempts"]) if row is not None else 0

    def set_running(self, task_id: str, step_id: str | None = None) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE tasks SET status = 'running', status_message = ?, current_step = ?,
                   updated_at = ? WHERE task_id = ? AND status IN ('queued', 'running')
                   AND cancel_requested = 0""",
                (f"Running {step_id}" if step_id else "Task started", step_id, _now(), task_id),
            )
        return cursor.rowcount == 1

    def step_started(self, task_id: str, step_id: str, attempts: int) -> None:
        timestamp = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO task_steps(task_id, step_id, status, attempts, started_at)
                   VALUES (?, ?, 'running', ?, ?)
                   ON CONFLICT(task_id, step_id) DO UPDATE SET
                     status = 'running', attempts = excluded.attempts,
                     started_at = COALESCE(task_steps.started_at, excluded.started_at),
                     error_json = NULL""",
                (task_id, step_id, attempts, timestamp),
            )
            db.execute(
                "UPDATE tasks SET current_step = ?, status_message = ?, updated_at = ? WHERE task_id = ?",
                (step_id, f"Running step {step_id} (attempt {attempts})", timestamp, task_id),
            )

    def step_completed(self, task_id: str, step_id: str, output: dict[str, Any]) -> None:
        payload = _bounded_json(output, self.max_result_bytes, "step result")
        timestamp = _now()
        with self._connect() as db:
            db.execute(
                """UPDATE task_steps SET status = 'completed', output_json = ?, error_json = NULL,
                   completed_at = ? WHERE task_id = ? AND step_id = ?""",
                (payload, timestamp, task_id, step_id),
            )
            db.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (timestamp, task_id))

    def step_failed(self, task_id: str, step_id: str, error: dict[str, Any]) -> None:
        payload = _bounded_json(error, 16 * 1024, "step error")
        with self._connect() as db:
            db.execute(
                """UPDATE task_steps SET status = 'failed', error_json = ?, completed_at = ?
                   WHERE task_id = ? AND step_id = ?""",
                (payload, _now(), task_id, step_id),
            )

    def complete(self, task_id: str, output: dict[str, Any]) -> None:
        payload = _bounded_json(output, self.max_result_bytes, "task result")
        self._terminal_transition(task_id, "completed", "Task completed", output_json=payload)

    def fail(self, task_id: str, code: str, message: str) -> None:
        error = _bounded_json(
            {"code": code[:100], "message": message[:4000]}, 16 * 1024, "task error"
        )
        self._terminal_transition(task_id, "failed", "Task failed", error_json=error)

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task["status"] in TERMINAL_STATUSES:
            raise TaskConflictError("terminal tasks cannot be cancelled")
        timestamp = _now()
        with self._connect() as db:
            if task["status"] == "queued":
                cursor = db.execute(
                    """UPDATE tasks SET status = 'cancelled', cancel_requested = 1,
                       status_message = 'Task cancelled', updated_at = ?
                       WHERE task_id = ? AND status = 'queued'""",
                    (timestamp, task_id),
                )
            else:
                cursor = db.execute(
                    """UPDATE tasks SET cancel_requested = 1,
                       status_message = 'Cancellation requested', updated_at = ?
                       WHERE task_id = ? AND status = 'running'""",
                    (timestamp, task_id),
                )
        if cursor.rowcount != 1:
            raise TaskConflictError("task reached a terminal state before cancellation")
        self.append_log(task_id, "warning", "Cancellation requested")
        return self.get(task_id)

    def cancellation_requested(self, task_id: str) -> bool:
        return bool(self.get(task_id)["cancel_requested"])

    def cancel_at_checkpoint(self, task_id: str) -> None:
        self._terminal_transition(task_id, "cancelled", "Task cancelled")

    def append_log(self, task_id: str, level: str, message: str) -> None:
        encoded = message.encode("utf-8")
        if len(encoded) > self.max_log_message_bytes:
            message = encoded[: self.max_log_message_bytes].decode("utf-8", errors="ignore")
        with self._connect() as db:
            db.execute(
                "INSERT INTO task_logs(task_id, timestamp, level, message) VALUES (?, ?, ?, ?)",
                (task_id, _now(), level[:20], message),
            )
            db.execute(
                """DELETE FROM task_logs WHERE task_id = ? AND sequence NOT IN (
                     SELECT sequence FROM task_logs WHERE task_id = ?
                     ORDER BY sequence DESC LIMIT ?
                   )""",
                (task_id, task_id, self.max_log_entries),
            )

    def logs(self, task_id: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        self.get(task_id)
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        with self._connect() as db:
            rows = db.execute(
                """SELECT sequence, timestamp, level, message FROM task_logs
                   WHERE task_id = ? AND sequence > ? ORDER BY sequence LIMIT ?""",
                (task_id, offset, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        kept = rows[:limit]
        entries = [dict(row) for row in kept]
        return {
            "task_id": task_id,
            "entries": entries,
            "next_offset": int(kept[-1]["sequence"]) if kept else offset,
            "has_more": has_more,
        }

    def _terminal_transition(
        self,
        task_id: str,
        status: str,
        message: str,
        *,
        output_json: str | None = None,
        error_json: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE tasks SET status = ?, status_message = ?, output_json = ?,
                   error_json = ?, current_step = NULL, updated_at = ?
                   WHERE task_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')""",
                (status, message, output_json, error_json, _now(), task_id),
            )


class LongSkillExecutor:
    """Daemon-worker executor that resumes workflows from durable step checkpoints."""

    def __init__(
        self,
        skills: LongSkillRegistry,
        tools: ServerToolRegistry,
        store: DurableTaskStore,
        *,
        worker_count: int = 4,
        autostart: bool = True,
    ) -> None:
        self.skills = skills
        self.tools = tools
        self.store = store
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._scheduled: set[str] = set()
        self._scheduled_lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._skill_slots = {
            (skill.id, skill.version): threading.BoundedSemaphore(skill.max_concurrent_runs)
            for skill in skills.list()
        }
        self._workers = [
            threading.Thread(target=self._worker, name=f"long-skill-{index}", daemon=True)
            for index in range(max(1, worker_count))
        ]
        self._started = False
        if autostart:
            self.start()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for worker in self._workers:
            worker.start()
        for task_id in self.store.recoverable_ids():
            self.enqueue(task_id)

    def submit(self, skill_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        skill = self.skills.get(skill_id)
        validated = skill.input_model.model_validate(arguments).model_dump(mode="json")
        task = self.store.create(
            skill,
            validated,
            contract_hash=self.skills.contract_hash(skill.id, skill.version),
        )
        self.enqueue(task["task_id"])
        return task

    def enqueue(self, task_id: str) -> None:
        with self._scheduled_lock:
            if task_id in self._scheduled:
                return
            self._scheduled.add(task_id)
        self._queue.put(task_id)

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        task = self.store.request_cancel(task_id)
        with self._scheduled_lock:
            event = self._cancel_events.get(task_id)
        if event is not None:
            event.set()
        return task

    def _worker(self) -> None:
        while True:
            task_id = self._queue.get()
            if task_id is None:
                return
            try:
                cancel_event = threading.Event()
                with self._scheduled_lock:
                    self._cancel_events[task_id] = cancel_event
                self._execute(task_id, cancel_event)
            finally:
                with self._scheduled_lock:
                    self._scheduled.discard(task_id)
                    self._cancel_events.pop(task_id, None)
                self._queue.task_done()

    def _execute(self, task_id: str, cancel_event: threading.Event) -> None:
        try:
            task = self.store.get(task_id)
            if task["status"] in TERMINAL_STATUSES:
                return
            skill = self.skills.get(task["skill_id"], task["skill_version"])
            if task["contract_hash"] != self.skills.contract_hash(skill.id, skill.version):
                self.store.fail(task_id, "contract_changed", "Registered skill contract changed")
                return
            slot = self._skill_slots[(skill.id, skill.version)]
            with slot:
                self._run_steps(task, skill, cancel_event)
        except Exception as exc:
            try:
                self.store.append_log(task_id, "error", f"Executor failure: {type(exc).__name__}")
                self.store.fail(task_id, "executor_failure", "Long skill execution failed")
            except Exception:
                pass

    def _run_steps(
        self, task: dict[str, Any], skill: LongSkill, cancel_event: threading.Event
    ) -> None:
        task_id = task["task_id"]
        if not self.store.set_running(task_id):
            return
        self.store.append_log(task_id, "info", f"Starting {skill.id}@{skill.version}")
        step_outputs = self.store.completed_steps(task_id)
        inputs = task["input"]
        deadline = datetime.fromisoformat(task["deadline_at"])

        for step in skill.steps:
            if step.id in step_outputs:
                self.store.append_log(task_id, "info", f"Resuming after checkpoint {step.id}")
                continue
            if self._stop_if_requested(task_id, deadline):
                return
            arguments = {
                key: resolve_value(value, inputs, step_outputs)
                for key, value in step.arguments.items()
            }
            response = None
            previous_attempts = self.store.step_attempts(task_id, step.id)
            if previous_attempts >= step.max_attempts:
                self.store.fail(task_id, "retry_exhausted", f"Step {step.id} retries exhausted")
                return
            for attempt in range(previous_attempts + 1, step.max_attempts + 1):
                self.store.step_started(task_id, step.id, attempt)
                self.store.append_log(
                    task_id, "info", f"Step {step.id}: calling {step.tool} (attempt {attempt})"
                )
                remaining_seconds = max(
                    1, int((deadline - datetime.now(timezone.utc)).total_seconds())
                )
                response = self.tools.execute(
                    step.tool,
                    arguments,
                    cancel_event=cancel_event,
                    timeout_seconds=remaining_seconds,
                )
                if response.ok:
                    break
                self.store.append_log(task_id, "warning", f"Step {step.id} attempt failed")
                if self._stop_if_requested(task_id, deadline):
                    return
            if response is None or not response.ok or not isinstance(response.result, dict):
                error = {
                    "code": response.error.code if response and response.error else "step_failed",
                    "message": response.error.message if response and response.error else "Step failed",
                }
                self.store.step_failed(task_id, step.id, error)
                self.store.fail(task_id, "step_failed", f"Step {step.id} failed")
                return
            self.store.step_completed(task_id, step.id, response.result)
            step_outputs[step.id] = response.result
            self.store.append_log(task_id, "info", f"Step {step.id} completed")
            if self._stop_if_requested(task_id, deadline):
                return

        output_values = {
            name: resolve_value(binding, inputs, step_outputs)
            for name, binding in skill.outputs.items()
        }
        output = skill.output_model.model_validate(output_values).model_dump(mode="json")
        self.store.complete(task_id, output)
        self.store.append_log(task_id, "info", "Task completed")

    def _stop_if_requested(self, task_id: str, deadline: datetime) -> bool:
        if self.store.cancellation_requested(task_id):
            self.store.cancel_at_checkpoint(task_id)
            self.store.append_log(task_id, "warning", "Task cancelled at workflow checkpoint")
            return True
        if datetime.now(timezone.utc) >= deadline:
            self.store.fail(task_id, "timeout", "Long skill deadline exceeded")
            self.store.append_log(task_id, "error", "Task deadline exceeded")
            return True
        return False


def _decode_task(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": str(row["task_id"]),
        "skill_id": str(row["skill_id"]),
        "skill_version": str(row["skill_version"]),
        "contract_hash": str(row["contract_hash"]),
        "status": str(row["status"]),
        "status_message": str(row["status_message"]),
        "input": json.loads(row["input_json"]),
        "result": json.loads(row["output_json"]) if row["output_json"] else None,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
        "current_step": row["current_step"],
        "cancel_requested": bool(row["cancel_requested"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "deadline_at": str(row["deadline_at"]),
    }


def _bounded_json(value: Any, limit: int, label: str) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if len(payload.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes; persist it and return an artifact URI")
    return payload
