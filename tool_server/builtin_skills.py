"""Small reviewed long skills shipped with the formal server."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .long_skill import LongSkill, StepValue, ToolStep


class RuntimeAuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeAuditOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    python_version: str


class RuntimeEnvironmentAudit(LongSkill):
    """Executable smoke test for the durable long-skill path."""

    id = "runtime.environment.audit"
    version = "1.0.0"
    title = "Runtime environment audit"
    description = "Run a checkpointed audit of the server Python environment."
    status = "approved"
    input_model = RuntimeAuditInput
    output_model = RuntimeAuditOutput
    timeout_seconds = 60
    max_concurrent_runs = 2
    steps = (
        ToolStep(id="python_version", tool="runtime.python.version"),
    )
    outputs = {
        "python_version": StepValue(step="python_version", path="process.stdout"),
    }


__all__ = ["RuntimeEnvironmentAudit"]
