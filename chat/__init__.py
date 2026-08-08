"""Nanobot-style terminal chat: rich rendering + a simple async message bus.

Messages flow: Channel -> MessageBus (inbound queue) -> AgentWorker
-> MessageBus (outbound queue) -> Channel. Input/output are decoupled from
the agent loop.
"""

