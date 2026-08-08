"""Message and event types flowing through the bus."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.types import utcnow


@dataclass(frozen=True)
class InboundMessage:
    """A chat message entering the system from a channel."""

    id: str
    channel: str
    content: str
    sender_id: str = "terminal"
    created_at: str = field(default_factory=utcnow)


@dataclass
class OutboundEvent:
    """An event emitted toward channels: reply | status | error."""

    kind: str
    reply_to: str
    text: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

