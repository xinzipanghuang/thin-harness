"""Built-in domain agents: Agent base class + subclasses.

An agent is a Python subclass, not a YAML file. It defines only the domain —
prompt, tools, runtime limits — while the model is constructed from the
generic .env values (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL /
LLM_ENABLE_THINKING) by the provider layer. Pick an agent with
``create_agent``:

    agent = create_agent()           # default: full-featured local daily agent
    agent = create_agent("coding")   # general coding agent
    agent = create_agent("faq")      # answers questions from PDFs
"""

from __future__ import annotations

from core.agent import Agent
from core.model import Model

from .coding_agent import CodingAgent
from .daily_agent import DailyAgent
from .faq_agent import FAQAgent

__all__ = ["Agent", "CodingAgent", "DailyAgent", "FAQAgent", "create_agent"]

AGENT_REGISTRY: dict[str, type[Agent]] = {
    "coding": CodingAgent,
    "daily": DailyAgent,
    "faq": FAQAgent,
}


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
