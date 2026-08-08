"""Model resolution: build a model adapter from config or generic .env vars.

The .env file provides only the four values every model endpoint needs —
api key, base url, model name, and whether thinking is enabled. The backend
resolves them (explicit config value -> env var -> default) and hands them to
the OpenAI-SDK transport. No vendor/factory catalog to maintain.
"""

from __future__ import annotations

from typing import Any, Optional

from .dotenv import get_env
from .model import Model, OpenAIModel, ScriptedModel

DEFAULT_MODEL = "deepseek-chat"


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def resolve(config: Optional[dict[str, Any]] = None) -> Model:
    """Build a model adapter. ``provider: scripted`` yields an offline fake."""
    cfg = dict(config or {})
    if str(cfg.pop("provider", "openai")).lower() == "scripted":
        return ScriptedModel()

    api_key = cfg.pop("api_key", None) or get_env("LLM_API_KEY")
    base_url = cfg.pop("base_url", None) or get_env("LLM_BASE_URL")
    name = cfg.pop("name", None) or get_env("LLM_MODEL") or DEFAULT_MODEL
    enable_thinking = cfg.pop("enable_thinking", None)
    if enable_thinking is None:
        raw = get_env("LLM_ENABLE_THINKING")
        enable_thinking = _parse_bool(raw) if raw else False
    if not api_key:
        raise ValueError(
            "LLM_API_KEY is required: set it in .env or pass api_key in the model config"
        )
    return OpenAIModel(
        name=name,
        api_key=api_key,
        base_url=base_url,
        enable_thinking=enable_thinking,
        **cfg,
    )
