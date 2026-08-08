"""Tiny async message bus decoupling channels from the agent worker.

Inspired by nanobot's MessageBus (inbound/outbound queues) but kept to the
minimum: two asyncio.Queue instances drained by background tasks into plain
subscriber callbacks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .events import InboundMessage, OutboundEvent

logger = logging.getLogger(__name__)

InboundHandler = Callable[[InboundMessage], Awaitable[None]]
OutboundHandler = Callable[[OutboundEvent], Awaitable[None]]


class MessageBus:
    def __init__(self) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._outbound: asyncio.Queue[OutboundEvent] = asyncio.Queue()
        self._inbound_handlers: list[InboundHandler] = []
        self._outbound_handlers: list[OutboundHandler] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False

    def subscribe(self, handler: InboundHandler) -> None:
        self._inbound_handlers.append(handler)

    def subscribe_outbound(self, handler: OutboundHandler) -> None:
        self._outbound_handlers.append(handler)

    async def publish(self, message: InboundMessage) -> None:
        await self._inbound.put(message)

    async def emit(self, event: OutboundEvent) -> None:
        await self._outbound.put(event)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._drain(self._inbound, self._inbound_handlers, "inbound")),
            asyncio.create_task(self._drain(self._outbound, self._outbound_handlers, "outbound")),
        ]

    async def _drain(self, queue, handlers, name: str) -> None:
        while self._running:
            item = await queue.get()
            try:
                for handler in list(handlers):
                    await handler(item)
            except Exception:
                logger.exception("message bus %s handler failed", name)
            finally:
                queue.task_done()

    async def stop(self) -> None:
        self._running = False
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

