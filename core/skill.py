"""Typed, closed skill definitions and explicit discovery.

A skill is trusted Python configuration, not executable Python supplied at
runtime.  Skill modules may construct the Pydantic models in this file and
register them, while the workflow engine remains the only component allowed
to interpret the resulting steps.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import re
from hashlib import sha256
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    field_validator,
    model_validator,
)


_IDENTIFIER = r"^[a-zA-Z][a-zA-Z0-9_.-]*$"
_VERSION = r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][a-zA-Z0-9.-]+)?$"
_PATH = r"^[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)*$"


class _ClosedModel(BaseModel):
    """Base for protocol objects: immutable and closed to unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolReference(_ClosedModel):
    """An immutable reference to one exact tool contract."""

    name: str = Field(pattern=_IDENTIFIER)
    version: str = Field(pattern=_VERSION)
    provider: str | None = Field(default=None, pattern=_IDENTIFIER)


class InputReference(_ClosedModel):
    """Read a value from the validated skill input."""

    kind: Literal["input"] = "input"
    path: str = Field(pattern=_PATH)


class StepOutputReference(_ClosedModel):
    """Read a value produced by an earlier step."""

    kind: Literal["step_output"] = "step_output"
    step: str = Field(pattern=_IDENTIFIER)
    path: str = Field(pattern=_PATH)


ValueReference = Annotated[
    InputReference | StepOutputReference,
    Field(discriminator="kind"),
]
ArgumentValue = ValueReference | JsonValue


class ToolStep(_ClosedModel):
    """A deterministic invocation of a registered tool."""

    kind: Literal["tool"] = "tool"
    id: str = Field(pattern=_IDENTIFIER)
    tool: ToolReference
    needs: tuple[str, ...] = ()
    arguments: dict[str, ArgumentValue] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, gt=0, le=7 * 24 * 60 * 60)

    @field_validator("needs")
    @classmethod
    def _unique_needs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_dependencies(value)


class QualityGateStep(_ClosedModel):
    """A fixed numeric gate evaluated by the workflow engine, never by eval."""

    kind: Literal["quality_gate"] = "quality_gate"
    id: str = Field(pattern=_IDENTIFIER)
    needs: tuple[str, ...] = ()
    value: ValueReference
    operator: Literal["gt", "gte", "lt", "lte", "eq", "ne"]
    threshold: float
    on_failure: Literal["stop", "review"] = "stop"

    @field_validator("needs")
    @classmethod
    def _unique_needs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_dependencies(value)


SkillStep = Annotated[ToolStep | QualityGateStep, Field(discriminator="kind")]


class SkillDefinition(_ClosedModel):
    """Complete, serializable contract for one approved workflow version."""

    id: str = Field(pattern=_IDENTIFIER)
    version: str = Field(pattern=_VERSION)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    status: Literal["draft", "approved", "deprecated"] = "draft"
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    steps: tuple[SkillStep, ...] = Field(min_length=1)
    outputs: dict[str, ValueReference]
    permissions: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    _input_model: type[BaseModel] | None = PrivateAttr(default=None)
    _output_model: type[BaseModel] | None = PrivateAttr(default=None)

    @classmethod
    def from_models(
        cls,
        *,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        **definition: Any,
    ) -> "SkillDefinition":
        """Build a skill contract from explicit Pydantic input/output models."""
        if (
            not isinstance(input_model, type)
            or not issubclass(input_model, BaseModel)
            or not isinstance(output_model, type)
            or not issubclass(output_model, BaseModel)
        ):
            raise TypeError("input_model and output_model must be Pydantic BaseModel classes")
        for role, model in (("input_model", input_model), ("output_model", output_model)):
            if model.model_config.get("extra") != "forbid":
                raise TypeError(f"{role} must set ConfigDict(extra='forbid')")
        result = cls(
            input_schema=input_model.model_json_schema(),
            output_schema=output_model.model_json_schema(),
            **definition,
        )
        result._input_model = input_model
        result._output_model = output_model
        return result

    @model_validator(mode="after")
    def _validate_graph(self) -> "SkillDefinition":
        try:
            json.dumps(self.input_schema, sort_keys=True, allow_nan=False)
            json.dumps(self.output_schema, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"input_schema and output_schema must be strict JSON: {exc}") from exc
        input_fields = set(self.input_schema.get("properties", {}))
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id: {step.id}")
            missing = set(step.needs) - seen
            if missing:
                names = ", ".join(sorted(missing))
                raise ValueError(
                    f"step {step.id!r} depends on missing or later step(s): {names}"
                )
            for reference in _references_in_step(step):
                if isinstance(reference, InputReference):
                    root = reference.path.split(".", 1)[0]
                    if root not in input_fields:
                        raise ValueError(
                            f"step {step.id!r} references unknown input {root!r}"
                        )
                elif reference.step not in seen:
                    raise ValueError(
                        f"step {step.id!r} references missing or later step "
                        f"{reference.step!r}"
                    )
                elif reference.step not in step.needs:
                    raise ValueError(
                        f"step {step.id!r} reads {reference.step!r} but does not "
                        "declare it in needs"
                    )
            seen.add(step.id)
        output_fields = set(self.output_schema.get("properties", {}))
        unknown_outputs = set(self.outputs) - output_fields
        if unknown_outputs:
            names = ", ".join(sorted(unknown_outputs))
            raise ValueError(f"outputs contain fields absent from output_schema: {names}")
        missing_outputs = set(self.output_schema.get("required", ())) - set(self.outputs)
        if missing_outputs:
            names = ", ".join(sorted(missing_outputs))
            raise ValueError(f"required output fields have no binding: {names}")
        for name, reference in self.outputs.items():
            if isinstance(reference, StepOutputReference) and reference.step not in seen:
                raise ValueError(f"output {name!r} references unknown step {reference.step!r}")
        return self

    def to_schema(self) -> dict[str, Any]:
        """Return an OpenAI-style discovery schema, parallel to ``Tool``."""
        return {
            "name": self.id,
            "description": self.description,
            "parameters": self.input_schema,
            "x-skill-version": self.version,
            "x-output-schema": self.output_schema,
        }

    def contract_hash(self) -> str:
        """Stable digest suitable for review records and persisted run provenance."""
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    def validate_input(self, value: Any) -> BaseModel:
        """Validate runtime input with the trusted Python model that defined it."""
        if self._input_model is None:
            raise RuntimeError(
                "this skill was restored from schema only; import its trusted Python "
                "definition before execution"
            )
        return self._input_model.model_validate(value)

    def validate_output(self, value: Any) -> BaseModel:
        """Validate workflow output with the trusted Python model that defined it."""
        if self._output_model is None:
            raise RuntimeError(
                "this skill was restored from schema only; import its trusted Python "
                "definition before execution"
            )
        return self._output_model.model_validate(value)


def _references_in_step(step: SkillStep) -> list[ValueReference]:
    if isinstance(step, QualityGateStep):
        return [step.value]
    return [value for value in step.arguments.values() if isinstance(value, (InputReference, StepOutputReference))]


def _validate_dependencies(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) != len(set(value)):
        raise ValueError("step dependencies must be unique")
    invalid = [name for name in value if re.fullmatch(_IDENTIFIER, name) is None]
    if invalid:
        raise ValueError(f"invalid step dependency id: {invalid[0]}")
    return value


class SkillRegistry:
    """In-memory index of immutable skill contracts, keyed by id and version."""

    def __init__(self) -> None:
        self._skills: dict[tuple[str, str], SkillDefinition] = {}
        self._sources: dict[tuple[str, str], str] = {}

    def register(self, definition: SkillDefinition, *, source: str = "") -> SkillDefinition:
        if not isinstance(definition, SkillDefinition):
            raise TypeError("only SkillDefinition instances can be registered")
        key = (definition.id, definition.version)
        existing = self._skills.get(key)
        if existing is not None and existing.model_dump() != definition.model_dump():
            raise ValueError(f"Skill already registered with different content: {definition.id}@{definition.version}")
        self._skills[key] = definition
        self._sources[key] = source
        return definition

    def get(
        self,
        skill_id: str,
        version: str | None = None,
        *,
        approved_only: bool = True,
    ) -> SkillDefinition:
        candidates = [
            skill for (name, _), skill in self._skills.items()
            if name == skill_id and (version is None or skill.version == version)
            and (not approved_only or skill.status == "approved")
        ]
        if not candidates:
            suffix = f"@{version}" if version else ""
            raise KeyError(f"Unknown or unavailable skill: {skill_id}{suffix}")
        return max(candidates, key=lambda item: _version_key(item.version))

    def list(self, *, approved_only: bool = True) -> list[SkillDefinition]:
        skills = [
            skill for skill in self._skills.values()
            if not approved_only or skill.status == "approved"
        ]
        return sorted(skills, key=lambda item: (item.id, _version_key(item.version)))

    def source(self, skill_id: str, version: str) -> str:
        return self._sources.get((skill_id, version), "")


def _version_key(version: str) -> tuple[tuple[int, int, int], bool, str]:
    without_build = version.split("+", 1)[0]
    numeric, separator, prerelease = without_build.partition("-")
    parts = tuple(int(part) for part in numeric.split("."))
    return parts + (0,) * (3 - len(parts)), not bool(separator), prerelease


SKILL_REGISTRY = SkillRegistry()


def register_skill(definition: SkillDefinition) -> SkillDefinition:
    """Explicitly register one trusted, model-validated skill definition."""
    frame = inspect.currentframe()
    caller = frame.f_back.f_globals.get("__name__", "") if frame and frame.f_back else ""
    return SKILL_REGISTRY.register(definition, source=caller)


def discover_skills(
    package_names: str | list[str] | tuple[str, ...],
) -> list[SkillDefinition]:
    """Import only explicitly configured trusted packages and return their skills."""
    names = [package_names] if isinstance(package_names, str) else list(package_names)
    for package_name in names:
        package = importlib.import_module(package_name)
        package_path = getattr(package, "__path__", None)
        if package_path is None:
            continue
        prefix = f"{package.__name__}."
        for module_info in pkgutil.walk_packages(package_path, prefix=prefix):
            relative = module_info.name[len(prefix):]
            if any(part.startswith("_") for part in relative.split(".")):
                continue
            importlib.import_module(module_info.name)
    prefixes = tuple(names)
    return [
        skill for skill in SKILL_REGISTRY.list(approved_only=False)
        if any(
            SKILL_REGISTRY.source(skill.id, skill.version) == prefix
            or SKILL_REGISTRY.source(skill.id, skill.version).startswith(f"{prefix}.")
            for prefix in prefixes
        )
    ]
