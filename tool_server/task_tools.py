"""Stable MCP Tool adapters for the internal durable long-skill task layer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .long_skill import LongSkill, LongSkillRegistry
from .registry import ServerTool, ServerToolRegistry
from .runtime import ToolExecutionContext
from .tasks import DurableTaskStore, LongSkillExecutor


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskIdInput(_ClosedModel):
    task_id: str = Field(pattern=r"^task_[a-f0-9]{32}$")


class TaskLogsInput(TaskIdInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class TaskView(_ClosedModel):
    task_id: str
    skill_id: str
    skill_version: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    status_message: str
    current_step: str | None
    cancel_requested: bool
    created_at: str
    updated_at: str
    deadline_at: str


class TaskSubmitOutput(_ClosedModel):
    task: TaskView
    poll_after_ms: int = 1000


class TaskGetOutput(_ClosedModel):
    task: TaskView


class TaskResultOutput(_ClosedModel):
    task_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    ready: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class TaskCancelOutput(_ClosedModel):
    task: TaskView


class TaskLogEntry(_ClosedModel):
    sequence: int
    timestamp: str
    level: str
    message: str


class TaskLogsOutput(_ClosedModel):
    task_id: str
    entries: list[TaskLogEntry]
    next_offset: int
    has_more: bool


def _view(task: dict[str, Any]) -> TaskView:
    return TaskView.model_validate(
        {
            key: task[key]
            for key in (
                "task_id",
                "skill_id",
                "skill_version",
                "status",
                "status_message",
                "current_step",
                "cancel_requested",
                "created_at",
                "updated_at",
                "deadline_at",
            )
        }
    )


class LongSkillSubmitTool(ServerTool):
    """One generated submit Tool for one explicitly registered LongSkill."""

    output_model = TaskSubmitOutput
    timeout_seconds = 10
    read_only = False
    destructive = False
    idempotent = False

    def __init__(self, skill: LongSkill, executor: LongSkillExecutor) -> None:
        self.skill = skill
        self.executor = executor
        self.name = f"skill.{skill.id}.submit"
        self.version = skill.version
        self.description = f"Submit approved long skill: {skill.description}"
        self.input_model = skill.input_model

    def run(self, ctx: ToolExecutionContext, arguments: BaseModel) -> TaskSubmitOutput:
        task = self.executor.submit(self.skill.id, arguments.model_dump(mode="json"))
        return TaskSubmitOutput(task=_view(task))


class TaskGetTool(ServerTool):
    name = "task.get"
    version = "1.0.0"
    description = "Get durable status for one long-skill task."
    input_model = TaskIdInput
    output_model = TaskGetOutput
    timeout_seconds = 10
    read_only = True
    destructive = False
    idempotent = True

    def __init__(self, store: DurableTaskStore) -> None:
        self.store = store

    def run(self, ctx: ToolExecutionContext, arguments: TaskIdInput) -> TaskGetOutput:
        return TaskGetOutput(task=_view(self.store.get(arguments.task_id)))


class TaskResultTool(ServerTool):
    name = "task.result"
    version = "1.0.0"
    description = "Return a bounded result or error when a long-skill task is terminal."
    input_model = TaskIdInput
    output_model = TaskResultOutput
    timeout_seconds = 10
    read_only = True
    destructive = False
    idempotent = True

    def __init__(self, store: DurableTaskStore) -> None:
        self.store = store

    def run(self, ctx: ToolExecutionContext, arguments: TaskIdInput) -> TaskResultOutput:
        task = self.store.get(arguments.task_id)
        return TaskResultOutput(
            task_id=task["task_id"],
            status=task["status"],
            ready=task["status"] in {"completed", "failed", "cancelled"},
            result=task["result"],
            error=task["error"],
        )


class TaskCancelTool(ServerTool):
    name = "task.cancel"
    version = "1.0.0"
    description = "Request cancellation of a queued or running long-skill task."
    input_model = TaskIdInput
    output_model = TaskCancelOutput
    timeout_seconds = 10
    read_only = False
    destructive = True
    idempotent = False

    def __init__(self, executor: LongSkillExecutor) -> None:
        self.executor = executor

    def run(self, ctx: ToolExecutionContext, arguments: TaskIdInput) -> TaskCancelOutput:
        return TaskCancelOutput(task=_view(self.executor.request_cancel(arguments.task_id)))


class TaskLogsTool(ServerTool):
    name = "task.logs"
    version = "1.0.0"
    description = "Read a bounded page of durable long-skill task logs."
    input_model = TaskLogsInput
    output_model = TaskLogsOutput
    timeout_seconds = 10
    read_only = True
    destructive = False
    idempotent = True

    def __init__(self, store: DurableTaskStore) -> None:
        self.store = store

    def run(self, ctx: ToolExecutionContext, arguments: TaskLogsInput) -> TaskLogsOutput:
        return TaskLogsOutput.model_validate(
            self.store.logs(arguments.task_id, arguments.offset, arguments.limit)
        )


def register_task_tools(
    tools: ServerToolRegistry,
    skills: LongSkillRegistry,
    executor: LongSkillExecutor,
    store: DurableTaskStore,
) -> None:
    """Expose reviewed long skills and shared task controls as ordinary MCP Tools."""

    for skill in skills.list():
        tools.register(LongSkillSubmitTool(skill, executor))
    tools.register(TaskGetTool(store))
    tools.register(TaskResultTool(store))
    tools.register(TaskCancelTool(executor))
    tools.register(TaskLogsTool(store))


__all__ = ["register_task_tools"]
