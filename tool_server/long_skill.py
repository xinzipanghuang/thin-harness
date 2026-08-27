"""Reviewed long-running skill contracts and their explicit registry."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from .registry import ServerToolRegistry, _require_closed_model


_IDENTIFIER = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][a-zA-Z0-9.-]+)?$")
_PATH = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)*$")


@dataclass(frozen=True)
class InputValue:
    """Read one value from the validated long-skill input."""

    path: str

    def __post_init__(self) -> None:
        if not _PATH.fullmatch(self.path):
            raise ValueError(f"invalid input reference path: {self.path}")


@dataclass(frozen=True)
class StepValue:
    """Read one value from a completed step output."""

    step: str
    path: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.step):
            raise ValueError(f"invalid referenced step id: {self.step}")
        if not _PATH.fullmatch(self.path):
            raise ValueError(f"invalid step output reference path: {self.path}")


BoundValue = InputValue | StepValue | str | int | float | bool | None | list[Any] | dict[str, Any]


@dataclass(frozen=True)
class ToolStep:
    """One fixed invocation of an explicitly registered atomic tool."""

    id: str
    tool: str
    needs: tuple[str, ...] = ()
    arguments: dict[str, BoundValue] = field(default_factory=dict)
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.id):
            raise ValueError(f"invalid step id: {self.id}")
        if not _IDENTIFIER.fullmatch(self.tool):
            raise ValueError(f"invalid tool name: {self.tool}")
        if len(self.needs) != len(set(self.needs)):
            raise ValueError(f"step {self.id} has duplicate dependencies")
        if self.max_attempts < 1 or self.max_attempts > 10:
            raise ValueError("max_attempts must be between 1 and 10")


class LongSkill:
    """Trusted Python definition of a durable, deterministic workflow."""

    id: ClassVar[str]
    version: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    steps: ClassVar[tuple[ToolStep, ...]]
    outputs: ClassVar[dict[str, InputValue | StepValue]]
    status: ClassVar[Literal["draft", "approved", "deprecated"]] = "draft"
    timeout_seconds: ClassVar[int] = 6 * 3600
    max_concurrent_runs: ClassVar[int] = 1
    environment: ClassVar[str] = "registered_server_tools"

    def contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
            "timeout_seconds": self.timeout_seconds,
            "max_concurrent_runs": self.max_concurrent_runs,
            "environment": self.environment,
            "steps": [
                {
                    "id": step.id,
                    "tool": step.tool,
                    "needs": list(step.needs),
                    "arguments": _serialize_bindings(step.arguments),
                    "max_attempts": step.max_attempts,
                }
                for step in self.steps
            ],
            "outputs": _serialize_bindings(self.outputs),
        }

    def contract_hash(self) -> str:
        payload = json.dumps(
            self.contract(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(payload).hexdigest()


class LongSkillRegistry:
    """Explicit registry containing only reviewed long-skill definitions."""

    def __init__(self, tools: ServerToolRegistry) -> None:
        self.tools = tools
        self._skills: dict[tuple[str, str], LongSkill] = {}
        self._contract_hashes: dict[tuple[str, str], str] = {}

    def register(self, skill: LongSkill) -> LongSkill:
        if not isinstance(skill, LongSkill):
            raise TypeError("only LongSkill instances can be registered")
        _validate_skill(skill, self.tools)
        key = (skill.id, skill.version)
        if key in self._skills:
            raise ValueError(f"long skill already registered: {skill.id}@{skill.version}")
        self._skills[key] = skill
        self._contract_hashes[key] = _resolved_contract_hash(skill, self.tools)
        return skill

    def get(self, skill_id: str, version: str | None = None) -> LongSkill:
        candidates = [
            skill
            for (name, item_version), skill in self._skills.items()
            if name == skill_id
            and (version is None or item_version == version)
            and skill.status == "approved"
        ]
        if not candidates:
            suffix = f"@{version}" if version else ""
            raise KeyError(f"unknown or unavailable long skill: {skill_id}{suffix}")
        return max(candidates, key=lambda item: _version_key(item.version))

    def list(self) -> list[LongSkill]:
        return sorted(
            (skill for skill in self._skills.values() if skill.status == "approved"),
            key=lambda item: (item.id, _version_key(item.version)),
        )

    def contract_hash(self, skill_id: str, version: str) -> str:
        try:
            return self._contract_hashes[(skill_id, version)]
        except KeyError:
            raise KeyError(f"unknown long skill contract: {skill_id}@{version}") from None


def resolve_value(value: BoundValue, inputs: dict[str, Any], steps: dict[str, Any]) -> Any:
    if isinstance(value, InputValue):
        return _read_path(inputs, value.path)
    if isinstance(value, StepValue):
        return _read_path(steps[value.step], value.path)
    if isinstance(value, list):
        return [resolve_value(item, inputs, steps) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, inputs, steps) for key, item in value.items()}
    return value


def _validate_skill(skill: LongSkill, tools: ServerToolRegistry) -> None:
    if not _IDENTIFIER.fullmatch(skill.id):
        raise ValueError(f"invalid long skill id: {skill.id}")
    if not _VERSION.fullmatch(skill.version):
        raise ValueError(f"invalid long skill version: {skill.version}")
    if skill.status != "approved":
        raise ValueError("only explicitly approved long skills may be registered")
    if not skill.title or not skill.description:
        raise ValueError("long skill title and description are required")
    _require_closed_model(skill.input_model, "input_model")
    _require_closed_model(skill.output_model, "output_model")
    if not 1 <= skill.timeout_seconds <= 7 * 24 * 60 * 60:
        raise ValueError("long skill timeout must be between 1 second and 7 days")
    if not 1 <= skill.max_concurrent_runs <= 256:
        raise ValueError("long skill concurrency must be between 1 and 256")
    if not skill.steps:
        raise ValueError("long skill must contain at least one step")

    input_fields = set(skill.input_model.model_fields)
    output_fields = set(skill.output_model.model_fields)
    if set(skill.outputs) != output_fields:
        raise ValueError("long skill outputs must bind every output model field exactly")
    seen: set[str] = set()
    available_tools = {manifest.name for manifest in tools.manifests()}
    for step in skill.steps:
        if step.id in seen:
            raise ValueError(f"duplicate long skill step id: {step.id}")
        missing = set(step.needs) - seen
        if missing:
            raise ValueError(f"step {step.id} depends on missing or later steps: {sorted(missing)}")
        if step.tool not in available_tools:
            raise ValueError(f"step {step.id} references unregistered tool: {step.tool}")
        for binding in _walk_bindings(step.arguments):
            if isinstance(binding, InputValue) and binding.path.split(".", 1)[0] not in input_fields:
                raise ValueError(f"step {step.id} references unknown input: {binding.path}")
            if isinstance(binding, StepValue):
                if binding.step not in seen or binding.step not in step.needs:
                    raise ValueError(
                        f"step {step.id} must depend on referenced step: {binding.step}"
                    )
        seen.add(step.id)
    for binding in skill.outputs.values():
        if isinstance(binding, InputValue) and binding.path.split(".", 1)[0] not in input_fields:
            raise ValueError(f"output references unknown input: {binding.path}")
        if isinstance(binding, StepValue) and binding.step not in seen:
            raise ValueError(f"output references unknown step: {binding.step}")


def _walk_bindings(value: Any) -> list[InputValue | StepValue]:
    if isinstance(value, (InputValue, StepValue)):
        return [value]
    if isinstance(value, list):
        return [binding for item in value for binding in _walk_bindings(item)]
    if isinstance(value, dict):
        return [binding for item in value.values() for binding in _walk_bindings(item)]
    return []


def _read_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"workflow reference path is unavailable: {path}")
        current = current[part]
    return current


def _serialize_bindings(value: Any) -> Any:
    if isinstance(value, InputValue):
        return {"kind": "input", "path": value.path}
    if isinstance(value, StepValue):
        return {"kind": "step_output", "step": value.step, "path": value.path}
    if isinstance(value, tuple):
        return [_serialize_bindings(item) for item in value]
    if isinstance(value, list):
        return [_serialize_bindings(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_bindings(item) for key, item in value.items()}
    return value


def _resolved_contract_hash(skill: LongSkill, tools: ServerToolRegistry) -> str:
    manifests = {manifest.name: manifest for manifest in tools.manifests()}
    payload = {
        "skill": skill.contract(),
        "resolved_tools": {
            name: manifests[name].model_dump(mode="json")
            for name in sorted({step.tool for step in skill.steps})
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _version_key(version: str) -> tuple[tuple[int, int, int], bool, str]:
    without_build = version.split("+", 1)[0]
    numeric, separator, prerelease = without_build.partition("-")
    parts = tuple(int(part) for part in numeric.split("."))
    return parts + (0,) * (3 - len(parts)), not bool(separator), prerelease
