"""Tool registration, schema generation, and execution.

Tools are ordinary typed Python functions decorated with ``@tool``.  Schemas
are generated from type hints, parameter defaults, and docstrings.  A tool may
declare a ``ctx: ToolContext`` parameter to receive harness-provided context
(artifact store, workdir, request, fact recording); that parameter is excluded
from the model-facing schema.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import pkgutil
import re
import subprocess
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union, get_args, get_origin, get_type_hints

from .artifacts import ArtifactStore
from .types import Fact, ToolCall, ToolResult


@dataclass
class ToolContext:
    """Harness-provided context injected into tools that declare a ctx parameter."""

    artifact_store: ArtifactStore
    workdir: Optional[str] = None
    request: str = ""
    facts: list[Fact] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)  # per-run tool scratchpad
    memory: Optional[Any] = None  # Memory instance, when the run is memory-backed
    session_id: str = ""  # current chat session id, when memory-backed
    environment: dict[str, Any] = field(default_factory=dict)  # OS/terminal/shell/cwd
    _current_tool: str = ""

    def record_fact(self, value: Any, source: Optional[str] = None) -> Fact:
        """Explicitly preserve a verified fact for later context builds."""
        fact = Fact(
            id=f"F{len(self.facts) + 1}",
            value=value,
            source=source,
            tool=self._current_tool or None,
        )
        self.facts.append(fact)
        return fact


TOOL_REGISTRY: dict[str, Tool] = {}


def tool(
    fn: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    serial: bool = False,
    cacheable: bool = False,
):
    """Decorator registering a typed function as a tool.

    The tool name defaults to ``<module>.<function>`` (e.g. ``filesystem.read``).
    ``serial=True`` forces the loop to run this tool alone, never concurrently
    with other calls in the same model response.
    ``cacheable=True`` marks the tool as idempotent/read-only: an exact repeat
    of an earlier call reuses the previous result instead of re-executing.

    Usage::

        @tool
        def read(path: str) -> str: ...

        @tool(serial=True)
        def write(path: str, content: str) -> str: ...
    """

    def decorate(f: Callable) -> Callable:
        module = f.__module__.split(".")[-1]
        tool_name = name or f"{module}.{f.__name__}"
        if tool_name in TOOL_REGISTRY:
            raise ValueError(f"Tool already registered: {tool_name}")
        TOOL_REGISTRY[tool_name] = Tool(
            name=tool_name, fn=f, serial=serial, cacheable=cacheable
        )
        return f

    if fn is not None:
        return decorate(fn)
    return decorate


def agent_tool(
    fn: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    serial: bool = False,
    cacheable: bool = False,
):
    """Decorator for agent-private tools.

    Like ``@tool`` but does NOT register the tool in the global registry —
    the decorated object is a ready-to-use ``Tool`` that a domain agent lists
    in its ``own_tools`` class attribute. Shared tools in ``tools/`` remain
    available to every agent via ``tool_include``; this is for tools that
    belong to one agent only.
    """

    def decorate(f: Callable) -> Tool:
        module = f.__module__.split(".")[-1]
        tool_name = name or f"{module}.{f.__name__}"
        return Tool(name=tool_name, fn=f, serial=serial, cacheable=cacheable)

    if fn is not None:
        return decorate(fn)
    return decorate


@dataclass
class Tool:
    """A registered tool: the Python function plus its generated schema."""

    name: str
    fn: Callable[..., Any]
    description: Optional[str] = None
    parameters: Optional[dict[str, Any]] = None
    serial: bool = False
    cacheable: bool = False  # idempotent/read-only: identical repeats reuse results
    _schema: dict[str, Any] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._schema is None:
            self._schema = build_schema(self.name, self.fn, self.description, self.parameters)

    def to_schema(self) -> dict[str, Any]:
        """OpenAI-style function schema for this tool."""
        return self._schema


def build_schema(
    name: str,
    fn: Callable,
    description: Optional[str],
    parameters: Optional[dict[str, Any]],
) -> dict[str, Any]:
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}
    doc_description, param_descs = parse_docstring(inspect.getdoc(fn) or "")
    params = parameters if parameters is not None else parameters_schema(fn, hints, param_descs)
    desc = description or doc_description or f"Tool {name}."
    return {"name": name, "description": desc, "parameters": params}


def parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """Return (short description, {param_name: description}) from a docstring."""
    if not doc.strip():
        return "", {}
    lines = doc.splitlines()
    paragraph: list[str] = []
    for line in lines:
        if not line.strip():
            break
        paragraph.append(line.strip())
    description = " ".join(paragraph)

    param_descs: dict[str, str] = {}
    in_args = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^args\s*:?\s*$", stripped, re.IGNORECASE):
            in_args = True
            continue
        if not in_args:
            continue
        if not stripped:
            continue
        if re.match(r"^(returns|raises|yields|examples?|notes?|warns?)\s*:?\s*$", stripped, re.IGNORECASE):
            in_args = False
            continue
        m = re.match(r"^(\w+)\s*:\s*(.+)$", stripped)
        if m:
            param_descs[m.group(1)] = m.group(2).strip()
    return description, param_descs


def parameters_schema(fn: Callable, hints: dict[str, Any], param_descs: dict[str, str]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if is_tool_context_param(fn, param, hints):
            continue
        schema = type_schema(hints.get(param_name))
        if param.default is not inspect.Parameter.empty:
            schema["default"] = param.default
        else:
            required.append(param_name)
        if param_name in param_descs:
            schema["description"] = param_descs[param_name]
        properties[param_name] = schema
    return {"type": "object", "properties": properties, "required": required}


def type_schema(annotation: Any) -> dict[str, Any]:
    if annotation is None or annotation is inspect.Parameter.empty:
        return {"type": "string"}
    if get_origin(annotation) is Union or get_origin(annotation) is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        return type_schema(args[0]) if args else {"type": "string"}
    if annotation in (str, Path):
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    origin = get_origin(annotation)
    if origin is not None and origin in (list, set, tuple):
        args = get_args(annotation)
        return {"type": "array", "items": type_schema(args[0]) if args else {"type": "string"}}
    if origin is dict:
        args = get_args(annotation)
        return {
            "type": "object",
            "additionalProperties": type_schema(args[1]) if len(args) > 1 else {"type": "string"},
        }
    if annotation is list:
        return {"type": "array"}
    if annotation is dict:
        return {"type": "object"}
    return {"type": "string"}


def is_tool_context_param(fn: Callable, param: inspect.Parameter, hints: dict[str, Any]) -> bool:
    ann = param.annotation
    if ann is ToolContext:
        return True
    if isinstance(ann, str):
        return hints.get(param.name) is ToolContext
    return False


def discover_tools(package_name: str = "tools") -> dict[str, Tool]:
    """Import every module in the tools package so ``@tool`` registrations land in the registry."""
    pkg = importlib.import_module(package_name)
    for module_info in pkgutil.iter_modules(pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        importlib.import_module(f"{package_name}.{module_info.name}")
    return dict(TOOL_REGISTRY)


def select_tools(spec: dict[str, Any]) -> list[Tool]:
    """Resolve include/exclude patterns against the registry, deterministically."""
    discover_tools()
    include = spec.get("include") or ["*"]
    exclude = spec.get("exclude") or []
    names = sorted(TOOL_REGISTRY)
    selected = [
        name
        for name in names
        if any(_pattern_matches(pattern, name) for pattern in include)
        and not any(_pattern_matches(pattern, name) for pattern in exclude)
    ]
    unmatched = [p for p in include if not any(_pattern_matches(p, name) for name in names)]
    if unmatched:
        raise ValueError(f"No registered tool matches include pattern(s): {', '.join(unmatched)}")
    return [TOOL_REGISTRY[name] for name in selected]


def _pattern_matches(pattern: str, name: str) -> bool:
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return pattern == name


_WIRE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def wire_name(name: str) -> str:
    """Model/API-facing tool name.

    OpenAI-compatible endpoints require function names to match
    ``^[a-zA-Z0-9_-]+$``, while registry names may contain dots
    (``filesystem.read``). This maps them to ``filesystem_read`` for the API;
    the loop maps the returned name back to the registry name.
    """
    return _WIRE_NAME_RE.sub("_", name)


def serialize_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """Clamp a numeric tool argument to [low, high]; fall back to default."""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def summarize_output(value: Any, text: str) -> str:
    if isinstance(value, dict):
        if "exit_code" in value:
            parts = [f"exit code {value.get('exit_code')}"]
            for key in ("stdout", "stderr"):
                chunk = value.get(key)
                if isinstance(chunk, str) and chunk.strip():
                    parts.append(f"{key} {len(chunk)} chars")
            return ", ".join(parts)
        if len(value) <= 4:
            return ", ".join(f"{k}={v}" for k, v in value.items())
        return f"dict with {len(value)} fields"
    if isinstance(value, (list, tuple)):
        return f"{len(value)} items"
    if text and len(text) <= 200:
        return text
    if text:
        return f"{len(text)} chars"
    return "completed"


def _bind_arguments(tool_obj: Tool, ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    sig = inspect.signature(tool_obj.fn)
    try:
        hints = get_type_hints(tool_obj.fn)
    except Exception:
        hints = {}
    kwargs: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if is_tool_context_param(tool_obj.fn, param, hints):
            kwargs[param_name] = ctx
            continue
        if param_name in arguments:
            kwargs[param_name] = arguments[param_name]
        elif param.default is not inspect.Parameter.empty:
            kwargs[param_name] = param.default
        else:
            raise ValueError(f"missing required argument {param_name!r} for tool {tool_obj.name}")
    return kwargs


def _persist_large_output(
    result: ToolResult, store: ArtifactStore, tool_name: str, max_chars: int
) -> ToolResult:
    if result.data is None or result.artifact_id is not None:
        return result
    text = serialize_output(result.data)
    if len(text) > max_chars:
        result.truncated = True
        result.artifact_id = store.put(text, tool=tool_name, summary=result.summary)
        result.preview = text[:max_chars]
    elif result.preview is None and text:
        result.preview = text[:max_chars]
    return result


async def execute_tool(
    tool_obj: Tool,
    ctx: ToolContext,
    call: ToolCall,
    timeout: float,
    max_chars: int,
) -> ToolResult:
    """Run one tool call, capturing exceptions/timeouts and normalizing the result.

    A tool failure is an observation, never a crash: the model sees the error
    in the next context and can recover.
    """
    ctx._current_tool = call.name
    try:
        kwargs = _bind_arguments(tool_obj, ctx, call.arguments or {})
    except Exception as exc:
        return ToolResult(
            ok=False,
            summary="Invalid tool call arguments",
            error=f"{type(exc).__name__}: {exc}",
        )
    try:
        fn = tool_obj.fn
        coro = fn(**kwargs) if inspect.iscoroutinefunction(fn) else asyncio.to_thread(fn, **kwargs)
        value = await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        return ToolResult(
            ok=False,
            summary=f"Tool timed out after {timeout:g}s",
            error=f"timed out after {timeout:g}s",
            timed_out=True,
        )
    except subprocess.TimeoutExpired as exc:
        return ToolResult(
            ok=False,
            summary=f"Tool timed out after {timeout:g}s",
            error=f"process timed out: {exc}",
            timed_out=True,
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            summary="Tool raised an exception",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        ctx._current_tool = ""

    if isinstance(value, ToolResult):
        result = value
    else:
        text = serialize_output(value)
        result = ToolResult(
            ok=True,
            summary=summarize_output(value, text),
            data=value,
            preview=text[:max_chars] if text else None,
        )
        if len(text) > max_chars:
            result.truncated = True
            result.artifact_id = ctx.artifact_store.put(text, tool=call.name, summary=result.summary)
            result.preview = text[:max_chars]
    return _persist_large_output(result, ctx.artifact_store, call.name, max_chars)
