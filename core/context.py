"""Compact model-facing context construction from structured run state.

Message history is append-only, Codex-style: the system prompt and the first
user message are built once per run; everything that changes afterwards
(assistant tool calls, tool results, new facts, stop hints) is appended as new
messages. This keeps the prompt prefix byte-stable for provider prefix
caching, and each model call only re-sends what was already sent plus the
incremental messages.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime, timezone
from typing import Optional

from .types import ContextConfig, Fact, Message, Observation, RunState, ToolCall


def _current_time_line() -> str:
    """Local wall-clock time + timezone, injected so the model is time-aware."""
    now = datetime.now().astimezone()
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {now.tzname()}"


def environment_info() -> dict[str, str]:
    """Detect OS / Python / terminal / shell / user / cwd for the agent."""
    system = platform.system()
    return {
        "os": system,
        "os_release": (
            platform.version() if system == "Windows" else platform.release()
        ),
        "arch": platform.machine(),
        "python": sys.version.split()[0],
        "terminal": _detect_terminal(),
        "shell": _detect_shell(),
        "user": (
            os.environ.get("USER")
            or os.environ.get("USERNAME")
            or os.environ.get("LOGNAME")
            or ""
        ),
        "cwd": os.getcwd(),
    }


def _detect_terminal() -> str:
    """Best-effort terminal emulator name from the environment."""
    env = os.environ
    program = (env.get("TERM_PROGRAM") or "").strip()
    if program:
        if program.lower() == "vscode":
            return "VS Code terminal"
        if program == "Apple_Terminal":
            return "Apple Terminal"
        if program == "iTerm.app":
            return "iTerm2"
        if program == "Windows Terminal":
            return "Windows Terminal"
        return program
    if env.get("WT_SESSION"):
        return "Windows Terminal"
    term = (env.get("TERM") or "").strip()
    if not term:
        return "unknown"
    if env.get("SSH_TTY") or env.get("SSH_CONNECTION"):
        return f"ssh ({term})"
    if env.get("TMUX"):
        return f"tmux ({term})"
    if term.startswith("screen"):
        return f"screen ({term})"
    return term


def _detect_shell() -> str:
    env = os.environ
    shell = (env.get("SHELL") or "").strip()
    if shell:
        return os.path.basename(shell)
    if env.get("PSModulePath"):
        return "powershell"
    comspec = (env.get("COMSPEC") or "").strip()
    if comspec:
        return os.path.basename(comspec)
    return "unknown"


def _environment_section() -> str:
    info = environment_info()
    os_display = {"Windows": "Windows", "Linux": "Linux", "Darwin": "macOS"}.get(
        info["os"], info["os"]
    )
    if info["os_release"]:
        os_display += f" {info['os_release']}"
    if info["arch"]:
        os_display += f" ({info['arch']})"
    lines = [
        f"os: {os_display}",
        f"python: {info['python']}",
        f"terminal: {info['terminal']}",
        f"shell: {info['shell']}",
    ]
    if info["user"]:
        lines.append(f"user: {info['user']}")
    lines.append(f"cwd: {info['cwd']}")
    return "\n".join(lines)


def _ago(stored: datetime, now: datetime) -> str:
    """Human '3 minutes ago' between a stored (UTC) time and local now."""
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc).astimezone(now.tzinfo)
    seconds = max(0, int((now - stored).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} minute(s) ago"
    if seconds < 86400:
        return f"{seconds // 3600} hour(s) ago"
    return f"{seconds // 86400} day(s) ago"


def _session_state(created: datetime, updated: datetime) -> str:
    """Orient the agent inside its own conversation timeline."""
    now = datetime.now().astimezone()
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc).astimezone(now.tzinfo)
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc).astimezone(now.tzinfo)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (
        f"started: {created.strftime(fmt)} ({_ago(created, now)})"
        f"\nlast activity: {updated.strftime(fmt)} ({_ago(updated, now)})"
    )


class ContextBuilder:
    """Builds the base message list and incremental message helpers.

    Layout of the base messages::

        SYSTEM PROMPT
        CURRENT TIME
        ENVIRONMENT              (OS / terminal / shell / cwd)
        SESSION STATE          (from memory, when a prior session exists)
        VERIFIED FACTS            (initial facts from memory)
        RELEVANT EXPERIENCE       (reusable methodology from memory, when matched)
        AVAILABLE TOOLS
        CONVERSATION SUMMARY      (earlier turns, one line each)
        CONVERSATION HISTORY      (most recent turns, verbatim)
        CURRENT USER REQUEST      (always the last section)

    The loop then appends, per model step: an assistant message carrying the
    tool calls, one tool message per result, and a user message whenever new
    facts were recorded. Raw tool output is never re-rendered; the appended
    messages are the only copy the model sees.
    """

    def __init__(
        self,
        system_prompt: str,
        config: ContextConfig,
        tool_names: Optional[list[str]] = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.config = config
        self.tool_names = tool_names or []

    def build_initial(
        self,
        state: RunState,
        history: Optional[list[tuple[str, str]]] = None,
        experiences: Optional[list[dict]] = None,
        session_times: Optional[tuple] = None,
    ) -> list[Message]:
        sections = [
            ("CURRENT TIME", _current_time_line()),
            ("ENVIRONMENT", _environment_section()),
        ]
        if session_times is not None:
            created, updated = session_times
            if created is not None and updated is not None:
                sections.append(("SESSION STATE", _session_state(created, updated)))
        if state.facts:
            sections.append(("VERIFIED FACTS", self._render_facts(state)))
        if experiences:
            sections.append(("RELEVANT EXPERIENCE", self._render_experiences(experiences)))
        if self.tool_names:
            sections.append(("AVAILABLE TOOLS", ", ".join(self.tool_names)))
        if history:
            sections.extend(self.history_sections(history))
        # The current request is always the last section: the newest input the
        # model reads, directly after the conversation it belongs to.
        sections.append(("CURRENT USER REQUEST", state.request))
        body = "\n\n".join(f"{header}\n{content}" for header, content in sections)
        return [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=body),
        ]

    def assistant_tool_calls_message(self, calls: list[ToolCall]) -> Message:
        """Assistant message echoing the model's tool calls (OpenAI format)."""
        tool_calls = [
            {
                "id": call.id or f"call_{index}",
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, default=str
                    ),
                },
            }
            for index, call in enumerate(calls)
        ]
        return Message(role="assistant", content="", tool_calls=tool_calls)

    def tool_result_message(self, call: ToolCall, obs: Observation) -> Message:
        """One tool message per executed call, tied to the assistant call id."""
        lines = [f"{obs.tool} -> {obs.status}: {obs.summary}"]
        if obs.preview and obs.preview != obs.summary:
            preview = obs.preview
            if len(preview) > self.config.max_tool_result_chars:
                preview = preview[: self.config.max_tool_result_chars] + "…"
            lines.append(preview)
        if obs.artifact_id:
            lines.append(
                f"[saved as artifact {obs.artifact_id}; read with artifacts.read]"
            )
        if obs.status == "cached":
            lines.append(
                "[cached — identical call reuses the previous result; no new information]"
            )
        elif obs.status == "blocked":
            lines.append(
                "[blocked — duplicate call; change the arguments or use a different tool]"
            )
        return Message(role="tool", content="\n".join(lines), tool_call_id=call.id)

    def facts_message(self, facts: list[Fact]) -> Message:
        lines = [self._render_fact(fact) for fact in facts]
        return Message(role="user", content="VERIFIED FACTS\n" + "\n".join(lines))

    def bootstrap_message(self, observations: list[Observation]) -> Message:
        lines = []
        for obs in observations:
            lines.append(f"[bootstrap] {obs.tool} -> {obs.status}: {obs.summary}")
            if obs.preview and obs.preview != obs.summary:
                lines.append(obs.preview)
        return Message(role="user", content="BOOTSTRAP\n" + "\n".join(lines))

    def notice_message(self, text: str) -> Message:
        return Message(role="user", content=f"HARNESS NOTICE\n{text}")

    def stop_hint_due(self, state: RunState) -> bool:
        """Hint fires only after enough steps + non-cached successes + stagnation."""
        cfg = self.config
        if state.steps < cfg.stop_hint_after_steps:
            return False
        successes = sum(1 for obs in state.observations if obs.status == "success")
        if successes < cfg.stop_hint_min_successes:
            return False
        recent_steps = sorted(
            {obs.step for obs in state.observations if obs.step > 0}
        )[-cfg.stop_hint_stagnant_rounds :]
        if not recent_steps:
            return False
        return not any(
            obs.step in recent_steps and obs.status == "success"
            for obs in state.observations
        )

    def stop_hint_message(self) -> Message:
        return Message(role="user", content="STOP HINT\n" + self._stop_hint_text())

    @staticmethod
    def estimate_tokens(messages: list[Message]) -> int:
        """Cheap byte-based token estimate (ceil(bytes/4)), like the reference."""
        total = 0
        for message in messages:
            total += len(message.content.encode("utf-8"))
            if message.tool_calls:
                total += len(
                    json.dumps(message.tool_calls, ensure_ascii=False).encode("utf-8")
                )
        return (total + 3) // 4

    def history_sections(self, history: list[tuple]) -> list[tuple[str, str]]:
        """Split history into (header, body) sections for the context.

        The ``history_recent_turns`` most recent turns are rendered verbatim
        (``CONVERSATION HISTORY``); everything earlier is compressed to one
        line per turn (``CONVERSATION SUMMARY``). Turns are labeled with their
        global session number when available, else with a window index.
        """
        numbered = bool(history) and len(history[0]) == 3  # (seq, user, assistant)
        recent_count = max(0, int(self.config.history_recent_turns))
        split_at = max(0, len(history) - recent_count) if recent_count else len(history)
        older = history[:split_at]
        recent = history[split_at:]
        sections: list[tuple[str, str]] = []

        if older:
            lines: list[str] = []
            for index, item in enumerate(older, 1):
                if numbered:
                    seq, user_text, assistant_text = item
                    label = seq
                else:
                    user_text, assistant_text = item
                    label = index
                lines.append(f"[turn {label}] user: {self._clip(user_text, 60)}")
                lines.append(f"    → {self._clip(assistant_text, 80)}")
            sections.append(("CONVERSATION SUMMARY", "\n".join(lines)))

        if recent:
            lines = []
            last_label: Optional[int] = None
            for index, item in enumerate(recent, 1):
                if numbered:
                    seq, user_text, assistant_text = item
                    label = seq
                else:
                    user_text, assistant_text = item
                    label = split_at + index
                last_label = label
                lines.append(f"[turn {label}] user: {self._verbatim(user_text)}")
                lines.append(f"[turn {label}] assistant: {self._verbatim(assistant_text)}")
            if numbered and last_label is not None:
                lines.append(
                    f"[history note: the last {len(recent)} turn(s) of this session "
                    f"are shown above; earlier turns are summarized; the latest is "
                    f"[turn {last_label}]]"
                )
            sections.append(("CONVERSATION HISTORY", "\n".join(lines)))
        return sections

    @staticmethod
    def _render_experiences(experiences: list[dict]) -> str:
        """Render stored experiences as reusable methodology (not facts)."""
        lines: list[str] = [
            "NOTE: historical methods from past runs, for reference only. If they "
            "do not fit the current request or environment, explore a new path "
            "instead of blindly following them."
        ]
        for exp in experiences:
            exp_id = exp.get("id") or "?"
            label = exp.get("problem_type") or exp.get("request") or "task"
            lines.append(f"[E{exp_id}] {label}")
            keywords = exp.get("keywords") or []
            if keywords:
                lines.append(f"    keywords: {', '.join(str(k) for k in keywords[:6])}")
            method = str(exp.get("method") or "").strip()
            if method:
                lines.append(f"    method: {method}")
            result = str(exp.get("result") or "").strip()
            if result:
                lines.append(f"    result: {result}")
            success = bool(exp.get("success", True))
            uses = int(exp.get("uses") or 1)
            meta = f"success: {success} | used {uses} time(s)"
            age = exp.get("age_days")
            if age is not None:
                meta += f" | last used {int(age)}d ago"
            if exp.get("time_sensitive"):
                meta += " | time-sensitive: verify live"
            if exp.get("stale"):
                meta += " | STALE"
            lines.append(f"    {meta}")
        return "\n".join(lines)

    @staticmethod
    def _clip(text: str, limit: int = 400) -> str:
        text = str(text or "").replace("\n", " ").strip()
        return text[:limit] + "…" if len(text) > limit else text

    @staticmethod
    def _verbatim(text: str) -> str:
        """Full text for the most recent turns (line breaks collapsed only)."""
        return str(text or "").replace("\n", " ").strip()

    def _render_facts(self, state: RunState) -> str:
        return "\n".join(self._render_fact(fact) for fact in state.facts)

    @staticmethod
    def _render_fact(fact: Fact) -> str:
        if isinstance(fact.value, str):
            value = fact.value
        else:
            value = json.dumps(fact.value, ensure_ascii=False, default=str)
        if len(value) > 500:
            value = value[:500] + "…"
        source = f" (source: {fact.source})" if fact.source else ""
        via = f" [via {fact.tool}]" if fact.tool else ""
        return f"[{fact.id}] {value}{source}{via}"

    @staticmethod
    def _stop_hint_text() -> str:
        return (
            "You have gathered substantial evidence and your recent tool calls added no new "
            "information. If the evidence is sufficient to answer, you are allowed to answer "
            "now. Extra exploration is optional, not required — only continue if you genuinely "
            "lack the information needed to answer."
        )
