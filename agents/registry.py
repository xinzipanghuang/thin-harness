"""Backward-compatible access to the core Agent registry."""

from core.registry import (
    AGENT_REGISTRY,
    create_agent,
    discover_agent_packages,
    register_agent,
)

__all__ = [
    "AGENT_REGISTRY",
    "create_agent",
    "discover_agent_packages",
    "register_agent",
]
