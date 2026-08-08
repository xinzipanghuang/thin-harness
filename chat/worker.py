"""AgentWorker: consumes inbound chat messages, runs the agent, emits replies."""

from __future__ import annotations

from core.agent import Agent

from .bus import MessageBus
from .events import InboundMessage, OutboundEvent


class AgentWorker:
    def __init__(
        self,
        bus: MessageBus,
        agent: Agent,
        history_limit: int = 8,
        memory=None,
        session_id: str = "",
    ) -> None:
        self.bus = bus
        self.agent = agent
        self.history_limit = history_limit
        self.history: list[tuple[str, str]] = []
        self.memory = memory
        self.session_id = session_id

    def clear_history(self) -> None:
        self.history.clear()
        if self.memory is not None and self.session_id:
            self.memory.clear_session(self.session_id)

    async def handle(self, message: InboundMessage) -> None:
        await self.bus.emit(
            OutboundEvent(kind="status", reply_to=message.id, text="agent is working…")
        )
        try:
            result = await self.agent.run(
                message.content,
                history=None if self.memory is not None else self.history,
                memory=self.memory,
                session_id=self.session_id,
                on_token=lambda text, _id=message.id: self._emit_stream(_id, text),
                on_debug=lambda level, kind, detail, _id=message.id: self._emit_debug(
                    _id, level, kind, detail
                ),
            )
        except Exception as exc:
            await self.bus.emit(
                OutboundEvent(
                    kind="error",
                    reply_to=message.id,
                    text=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        if self.memory is None:
            self.history.append((message.content, result.text))
            if self.history_limit and len(self.history) > self.history_limit:
                self.history = self.history[-self.history_limit :]
        await self.bus.emit(
            OutboundEvent(
                kind="reply",
                reply_to=message.id,
                text=result.text,
                detail={
                    "steps": result.state.steps,
                    "tool_calls": result.state.tool_calls,
                    "failures": result.state.failures,
                    "stop_reason": result.stop_reason,
                },
            )
        )

    async def _emit_stream(self, message_id: str, text: str) -> None:
        await self.bus.emit(OutboundEvent(kind="stream", reply_to=message_id, text=text))

    async def _emit_debug(self, message_id: str, level: int, kind: str, detail: dict) -> None:
        await self.bus.emit(
            OutboundEvent(
                kind="debug",
                reply_to=message_id,
                detail={"level": level, "kind": kind, **detail},
            )
        )
