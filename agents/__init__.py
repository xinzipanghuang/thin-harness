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
from core.registry import AGENT_REGISTRY, create_agent, discover_agent_packages, register_agent

from .coding_agent import CodingAgent
from .daily_agent import DailyAgent

register_agent("coding", CodingAgent)
register_agent("daily", DailyAgent)
discover_agent_packages()

from domains.bioinformatics import BioinformaticsAgent
from domains.faq import FAQAgent

__all__ = [
    "AGENT_REGISTRY",
    "Agent",
    "BioinformaticsAgent",
    "CodingAgent",
    "DailyAgent",
    "FAQAgent",
    "create_agent",
    "register_agent",
]
