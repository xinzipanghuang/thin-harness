"""Small explicit registry for built-in and domain Agent subclasses."""

from __future__ import annotations

import importlib
import pkgutil
from typing import Callable

from .agent import Agent
from .model import Model

AGENT_REGISTRY: dict[str, type[Agent]] = {}


def register_agent(
    name: str,
    agent_class: type[Agent] | None = None,
) -> Callable[[type[Agent]], type[Agent]] | type[Agent]:
    """Register an Agent class, directly or as ``@register_agent(name)``."""

    def decorate(cls: type[Agent]) -> type[Agent]:
        existing = AGENT_REGISTRY.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(f"Agent already registered: {name}")
        AGENT_REGISTRY[name] = cls
        return cls

    return decorate(agent_class) if agent_class is not None else decorate


def discover_agent_packages(package_names: tuple[str, ...] = ("domains",)) -> None:
    """Import declared domain packages so their registrations become visible."""
    for package_name in package_names:
        package = importlib.import_module(package_name)
        prefix = f"{package.__name__}."
        for item in pkgutil.iter_modules(package.__path__, prefix=prefix):
            relative = item.name[len(prefix) :]
            if relative.startswith("_"):
                continue
            importlib.import_module(item.name)


def create_agent(
    name: str = "daily",
    model: Model | None = None,
    *,
    workdir: str | None = None,
    log_dir: str | None = None,
) -> Agent:
    """Instantiate a registered domain agent by name."""
    try:
        agent_class = AGENT_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown agent {name!r} (available: {', '.join(sorted(AGENT_REGISTRY))})"
        ) from None
    return agent_class(model=model, workdir=workdir, log_dir=log_dir)
