"""Small model adapter interface plus the OpenAI-SDK transport.

Self-contained: no imports from external reference folders or other projects.
Every model endpoint goes through the OpenAI SDK (AsyncOpenAI) — one
transport, no hand-written HTTP client. Config resolution (LLM_API_KEY /
LLM_BASE_URL / LLM_MODEL / LLM_ENABLE_THINKING) lives in the provider layer
(core/providers.py).
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional, Protocol

from .types import Message, ModelResponse, ToolCall


class Model(Protocol):
    """Minimal adapter contract: messages + tool schemas in, response out."""

    async def respond(
        self,
        messages: list[Message],
        tools: list[dict],
        on_token=None,
    ) -> ModelResponse:
        ...


class ScriptedModel:
    """Deterministic fake model: pops the next scripted response per call.

    Used by tests and offline examples so no API key or network is required.
    """

    def __init__(self, script: Optional[list[ModelResponse]] = None) -> None:
        self.script = list(script or [])
        self.calls: list[tuple[list[Message], list[dict]]] = []

    async def respond(
        self,
        messages: list[Message],
        tools: list[dict],
        on_token=None,
    ) -> ModelResponse:
        self.calls.append((messages, list(tools)))
        if self.script:
            return self.script.pop(0)
        return ModelResponse(text="(script exhausted)")


def _extract_request(body: str) -> str:
    marker = "CURRENT USER REQUEST\n"
    if marker in body:
        rest = body.split(marker, 1)[1]
        return rest.split("\n\n", 1)[0].strip()
    return str(body or "").strip()


class EchoModel:
    """Offline demo model: echoes the current user request, never calls tools."""

    async def respond(
        self,
        messages: list[Message],
        tools: list[dict],
        on_token=None,
    ) -> ModelResponse:
        for message in messages:
            if message.role == "user" and "CURRENT USER REQUEST" in message.content:
                request = _extract_request(message.content)
                return ModelResponse(text=f"[echo] {request or '(no request)'}")
        for message in reversed(messages):
            if message.role == "user":
                request = _extract_request(message.content)
                return ModelResponse(text=f"[echo] {request or '(no request)'}")
        return ModelResponse(text="[echo] (no request)")


def _messages_payload(messages: list[Message]) -> list[dict[str, Any]]:
    payload = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = message.tool_calls
        payload.append(item)
    return payload


def _usage_dict(usage: Any) -> Optional[dict[str, Any]]:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    try:
        return usage.model_dump()
    except AttributeError:
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }


def _parse_chat_completion(response: Any) -> ModelResponse:
    """Normalize an OpenAI SDK response into a ModelResponse."""
    choices = list(getattr(response, "choices", None) or [])
    text = ""
    thinking = None
    calls: list[ToolCall] = []
    if choices:
        message = getattr(choices[0], "message", None)
        if message is not None:
            text = getattr(message, "content", None) or ""
            thinking = getattr(message, "reasoning_content", None) or None
            for tool_call in list(getattr(message, "tool_calls", None) or []):
                function = getattr(tool_call, "function", None)
                name = getattr(function, "name", "") or ""
                raw = getattr(function, "arguments", "{}") or "{}"
                try:
                    arguments = json.loads(raw)
                except json.JSONDecodeError:
                    arguments = {"_raw": raw}
                call_id = getattr(tool_call, "id", None) or None
                calls.append(ToolCall(name=name, arguments=arguments, id=call_id))
    return ModelResponse(
        text=text,
        tool_calls=calls,
        usage=_usage_dict(getattr(response, "usage", None)),
        thinking=thinking if choices else None,
    )


class OpenAIModel:
    """Model transport built on the OpenAI SDK (AsyncOpenAI).

    Light to import and works with any OpenAI-compatible endpoint through
    ``base_url`` (DeepSeek, DashScope, LiteLLM proxy, ...). Values are resolved
    by the provider layer from the generic .env config; ``enable_thinking`` is
    passed through as ``extra_body`` for providers that support it.
    """

    def __init__(
        self,
        name: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 120.0,
        max_tokens: Optional[int] = None,
        temperature: float = 0.0,
        enable_thinking: Optional[bool] = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "model requires api_key (set LLM_API_KEY in .env or pass api_key in the model config)"
            )
        self.name = name
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.enable_thinking = enable_thinking
        # Eager import: the SDK takes ~1s to import; doing it inside respond()
        # would block the event loop and stall the terminal on the first call.
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

    async def respond(
        self,
        messages: list[Message],
        tools: list[dict],
        on_token=None,
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self.name,
            "messages": _messages_payload(messages),
            "temperature": self.temperature,
        }
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        if tools:
            kwargs["tools"] = [{"type": "function", "function": tool} for tool in tools]
            kwargs["tool_choice"] = "auto"
        if self.enable_thinking is not None:
            kwargs["extra_body"] = {"enable_thinking": bool(self.enable_thinking)}
        if on_token is not None:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        started_at = time.perf_counter()
        response = await self._client.chat.completions.create(**kwargs)
        connect_ms = (time.perf_counter() - started_at) * 1000
        if on_token is not None:
            return await self._collect_stream(
                response,
                on_token,
                started_at=started_at,
                connect_ms=connect_ms,
            )
        return _parse_chat_completion(response)

    async def _collect_stream(
        self,
        stream,
        on_token,
        started_at: Optional[float] = None,
        connect_ms: Optional[float] = None,
    ) -> ModelResponse:
        """Accumulate a streaming response, forwarding content tokens as they arrive."""
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage = None
        first_token_ms: Optional[float] = None
        async for chunk in stream:
            usage = getattr(chunk, "usage", None) or usage
            choices = list(getattr(chunk, "choices", None) or [])
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            reasoning = getattr(delta, "reasoning_content", None)
            tool_call_deltas = list(getattr(delta, "tool_calls", None) or [])
            if first_token_ms is None and (content or reasoning or tool_call_deltas):
                if started_at is not None:
                    first_token_ms = (time.perf_counter() - started_at) * 1000
            if content:
                text_parts.append(content)
                await on_token(content)
            if reasoning:
                thinking_parts.append(reasoning)
            for tool_call in tool_call_deltas:
                index = getattr(tool_call, "index", 0)
                entry = calls.setdefault(
                    index, {"index": index, "id": "", "name": "", "arguments": ""}
                )
                if getattr(tool_call, "id", None):
                    entry["id"] = tool_call.id
                function = getattr(tool_call, "function", None)
                if function is not None:
                    if getattr(function, "name", None):
                        entry["name"] = function.name
                    if getattr(function, "arguments", None):
                        entry["arguments"] += function.arguments or ""
        tool_calls: list[ToolCall] = []
        for entry in sorted(calls.values(), key=lambda item: item["index"]):
            raw = entry["arguments"] or "{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = {"_raw": raw}
            tool_calls.append(
                ToolCall(name=entry["name"], arguments=arguments, id=entry.get("id") or None)
            )
        text = "".join(text_parts)
        thinking = "".join(thinking_parts) or None
        metrics: dict[str, Any] = {}
        if connect_ms is not None:
            metrics["connect_ms"] = round(connect_ms, 1)
        if first_token_ms is not None:
            metrics["ttft_ms"] = round(first_token_ms, 1)
        return ModelResponse(
            text=text or None,
            tool_calls=tool_calls,
            usage=_usage_dict(usage),
            thinking=thinking,
            metrics=metrics or None,
        )


def build_model(config: dict[str, Any]) -> Model:
    """Create a model adapter via the provider layer (see core/providers.py)."""
    from .providers import resolve

    return resolve(config)
