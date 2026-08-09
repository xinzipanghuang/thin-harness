"""Core data types shared across the runtime."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Message:
    """A single model-facing message."""

    role: str  # system | user | assistant | tool
    content: str
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[dict]] = None  # assistant messages: OpenAI-style tool_calls


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    name: str
    arguments: dict[str, Any]
    id: Optional[str] = None  # provider call id, assigned if the provider omits it


@dataclass
class ToolResult:
    """Normalized outcome of a single tool execution."""

    ok: bool
    summary: str
    data: Any = None
    error: Optional[str] = None
    artifact_id: Optional[str] = None
    truncated: bool = False
    timed_out: bool = False
    blocked: bool = False  # harness blocked a duplicate call before execution
    cached: bool = False  # result reused from an identical earlier call (no new evidence)
    preview: Optional[str] = None


@dataclass
class Observation:
    """Compact, model-facing record of one tool execution."""

    tool: str
    status: str  # success | error | timeout | blocked | cached
    summary: str
    preview: str = ""
    arguments: Optional[dict[str, Any]] = None
    truncated: bool = False
    artifact_id: Optional[str] = None
    step: int = 0


@dataclass
class Fact:
    """A verified fact preserved across steps for grounding."""

    id: str
    value: Any
    source: Optional[str] = None
    tool: Optional[str] = None


@dataclass
class Evidence:
    """One numbered piece of authorized evidence for the final answer."""

    index: int
    tool: str
    summary: str
    preview: str = ""
    source: str = ""


@dataclass
class Artifact:
    """Metadata for a persisted, potentially large tool output."""

    id: str
    tool: str
    summary: str
    size: int
    created_at: str = field(default_factory=utcnow)


@dataclass
class DebugEvent:
    """One structured debug record for a run.

    Debug events are collected on every run and persisted to the database
    regardless of the UI debug level; ``on_debug`` only controls live
    rendering. The detail dict already carries ``elapsed_ms`` (and event
    payload, e.g. context or tool arguments) from the loop.
    """

    level: int
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)


@dataclass
class RunState:
    """Structured world state for a run.

    World state deliberately does not live entirely inside model tokens:
    observations, facts, artifacts, and counters are explicit.
    """

    request: str
    observations: list[Observation] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    call_history: dict[tuple[str, str], int] = field(default_factory=dict)
    result_cache: dict[tuple[str, str], ToolResult] = field(default_factory=dict)
    repeated_calls: list[str] = field(default_factory=list)
    harness_notices: list[str] = field(default_factory=list)
    final_text: Optional[str] = None
    answer_hint_sent: bool = False
    stop_hint_sent: bool = False
    last_estimated_tokens: int = 0
    evidence: list[Evidence] = field(default_factory=list)
    _evidence_keys: set = field(default_factory=set, repr=False)


@dataclass
class ModelResponse:
    """Response from a model adapter: either final text or tool calls."""

    text: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Optional[dict[str, Any]] = None
    thinking: Optional[str] = None  # reasoning content, when the provider exposes it
    metrics: Optional[dict[str, Any]] = None  # transport timings, e.g. ttft_ms

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


@dataclass
class RuntimeConfig:
    """Deterministic loop guards and harness behavior."""

    max_steps: int = 8
    max_tool_calls: int = 12
    tool_timeout: float = 60.0
    max_consecutive_failures: int = 3
    max_consecutive_no_gain: int = 2  # tool rounds with no new evidence -> force final
    model_timeout: float = 90.0  # per model call
    request_timeout: float = 300.0  # whole run
    # Synthesize an answer only when a deterministic guard stops the main loop
    # before the model answers; normally completed answers are used directly.
    final_regenerate: bool = True
    experience_enabled: bool = True  # evolution module: inject + record experiences
    workdir: Optional[str] = None
    log_dir: Optional[str] = None


@dataclass
class ContextConfig:
    """Simple deterministic context-management policies."""

    max_tool_result_chars: int = 8000
    keep_recent_observations: int = 5
    # Once useful evidence exists, remind the model that answering is preferred
    # over optional exploration. This is advisory and task-independent.
    answer_hint_after_steps: int = 3
    answer_hint_min_evidence: int = 2
    # answer-now hint: injected when the run has enough steps and non-cached
    # successes, and the most recent tool rounds added no new evidence.
    stop_hint_after_steps: int = 6
    stop_hint_min_successes: int = 3
    stop_hint_stagnant_rounds: int = 2
    token_budget_tokens: int = 0  # 0 = disabled; when exceeded, force a stop hint
    history_recent_turns: int = 3  # most recent turns shown verbatim in context
    history_summary_turns: int = 12  # earlier turns compressed to one line each
    experience_stale_days: int = 7  # time-sensitive experiences older than this are not injected
    max_facts: int = 15  # cap on VERIFIED FACTS injected (newest first)
    max_evidence_items: int = 24  # cap on numbered evidence items for the final answer
    max_chars_per_evidence: int = 2000


@dataclass
class RunResult:
    """Outcome of one agent run."""

    text: str
    state: RunState
    stop_reason: str  # completed | max_steps | max_tool_calls | consecutive_failures | error
    log: Optional[Any] = None
