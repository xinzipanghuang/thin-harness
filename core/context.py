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
from typing import Optional

from .types import ContextConfig, Fact, Message, Observation, RunState, ToolCall


class ContextBuilder:
    """Builds the base message list and incremental message helpers.

    Layout of the base messages::

        SYSTEM PROMPT
        CURRENT USER REQUEST
        CONVERSATION HISTORY      (from memory, clipped)
        VERIFIED FACTS            (initial facts from memory)
        AVAILABLE TOOLS

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
    ) -> list[Message]:
        sections = [("CURRENT USER REQUEST", state.request)]
        if history:
            sections.append(("CONVERSATION HISTORY", self._render_history(history)))
        if state.facts:
            sections.append(("VERIFIED FACTS", self._render_facts(state)))
        if self.tool_names:
            sections.append(("AVAILABLE TOOLS", ", ".join(self.tool_names)))
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

    def _render_history(self, history: list[tuple[str, str]]) -> str:
        verbatim = max(0, int(self.config.history_verbatim_turns))
        start = max(0, len(history) - verbatim)
        lines: list[str] = []
        for index, (user_text, assistant_text) in enumerate(history, 1):
            if index - 1 >= start:
                lines.append(f"[turn {index}] user: {self._verbatim(user_text)}")
                lines.append(f"[turn {index}] assistant: {self._verbatim(assistant_text)}")
            else:
                lines.append(f"[turn {index}] user: {self._clip(user_text)}")
                lines.append(f"[turn {index}] assistant: {self._clip(assistant_text)}")
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
