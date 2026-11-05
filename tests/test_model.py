"""Exercise the SDK wire protocol across a complete tool round, without a key."""

import json
import tempfile
import unittest

import httpx
from openai import AsyncOpenAI

from core.agent import Agent
from core.model import OpenAIModel
from core.tool import Tool
from core.types import Message, RuntimeConfig, ToolResult


async def sample_overview(sample_id: str):
    return ToolResult(ok=True, summary=f"{sample_id} complete", data={"sample_id": sample_id})


class BailianModelTests(unittest.IsolatedAsyncioTestCase):
    async def check_tool_round(self, *, streaming: bool, thinking: bool, regenerate: bool = False,
                               source_truncated: bool = False):
        requests = []
        reasoning = "Need the sample evidence." if thinking else None

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            first = len(requests) == 1
            call = {"id": "call_sample", "type": "function", "function": {
                "name": "sample_overview", "arguments": '{"sample_id":"S1"}',
            }}
            if not streaming:
                message = {"role": "assistant", "content": "" if first else "S1 complete"}
                if first:
                    message["tool_calls"] = [call]
                    if reasoning:
                        message["reasoning_content"] = reasoning
                return httpx.Response(200, json={
                    "id": "completion", "object": "chat.completion", "created": 0,
                    "model": body["model"], "choices": [{"index": 0, "message": message,
                        "finish_reason": "tool_calls" if first else "stop"}],
                })

            deltas = []
            if first:
                if reasoning:
                    deltas.extend([{"reasoning_content": "Need the "},
                                   {"reasoning_content": "sample evidence."}])
                # Providers split function arguments across multiple SSE records.
                deltas.extend([
                    {"tool_calls": [{"index": 0, **{**call, "function": {
                        "name": "sample_overview", "arguments": '{"sample_'
                    }}}]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": 'id":"S1"}'}}]},
                ])
            else:
                deltas.extend([{"content": "S1 "}, {"content": "complete"}])
            chunks = [{"id": "completion", "object": "chat.completion.chunk", "created": 0,
                       "model": body["model"], "choices": [{"index": 0, "delta": delta,
                       "finish_reason": None}]} for delta in deltas]
            chunks.append({"id": "completion", "object": "chat.completion.chunk", "created": 0,
                           "model": body["model"], "choices": [], "usage": {
                               "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
            data = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=data + "data: [DONE]\n\n")

        model = OpenAIModel(name="deepseek-v4.1-flash-1017", api_key="test-key",
                            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                            enable_thinking=thinking)
        await model._client.close()
        model._client = AsyncOpenAI(api_key="test-key", base_url=model.base_url,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        tokens = []

        async def on_token(text):
            tokens.append(text)

        history = [
            Message(role="assistant", content="Earlier status from before the window."),
            Message(role="user", content="Check Wuhan.\n\nUse the latest run."),
            Message(role="assistant", content="Previous evidence:\n" + "x" * 120),
            Message(role="user", content="I meant sample S1."),
        ]
        try:
            with tempfile.TemporaryDirectory() as workdir:
                async def lookup(sample_id: str):
                    result = await sample_overview(sample_id)
                    if source_truncated:
                        result.data["truncated"] = True
                    return result

                agent = Agent(model=model, tools=[Tool(name="sample_overview", fn=lookup)],
                              system_prompt="Query sample evidence.", runtime=RuntimeConfig(
                                  experience_enabled=False, final_regenerate=regenerate,
                                  max_steps=1 if regenerate else 8,
                                  workdir=workdir, log_dir=workdir))
                result = await agent.run(
                    "Query sample S1", history=history,
                    on_token=on_token if streaming else None,
                )
        finally:
            await model._client.close()

        self.assertEqual(result.text, "S1 complete")
        self.assertEqual(result.state.tool_calls, 1)
        self.assertEqual(len(requests), 2)
        for body in requests:
            self.assertEqual(body["model"], "deepseek-v4.1-flash-1017")
            self.assertEqual(body["enable_thinking"], thinking)
            self.assertEqual(body.get("stream", False), streaming)
            self.assertEqual(body["messages"][0]["role"], "system")
            self.assertIn("CURRENT TIME", body["messages"][0]["content"])
            self.assertEqual(body["messages"][1:6], [
                {"role": message.role, "content": message.content} for message in history
            ] + [{"role": "user", "content": "Query sample S1"}])
        self.assertIn("[truncated=true/false]", requests[0]["tools"][0]["function"]["description"])
        if regenerate:
            self.assertNotIn("tools", requests[1])
            self.assertIn("AUTHORIZED EVIDENCE", requests[1]["messages"][6]["content"])
            self.assertIn("[truncated=false]", requests[1]["messages"][6]["content"])
            return
        self.assertEqual(requests[1]["messages"][:6], requests[0]["messages"])
        assistant = requests[1]["messages"][6]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant.get("reasoning_content"), reasoning)
        tool = next(m for m in requests[1]["messages"] if m["role"] == "tool")
        self.assertIn(f"[truncated={'true' if source_truncated else 'false'}]", tool["content"])
        self.assertEqual(tool["tool_call_id"], assistant["tool_calls"][0]["id"])
        if not thinking:
            self.assertNotIn("reasoning_content", assistant)
        if streaming:
            self.assertEqual("".join(tokens), "S1 complete")

    async def test_streaming_with_and_without_thinking(self):
        for thinking in (False, True):
            with self.subTest(thinking=thinking):
                await self.check_tool_round(streaming=True, thinking=thinking)

    async def test_non_streaming_with_and_without_thinking(self):
        for thinking in (False, True):
            with self.subTest(thinking=thinking):
                await self.check_tool_round(streaming=False, thinking=thinking)

    async def test_final_generation_preserves_role_history(self):
        await self.check_tool_round(streaming=False, thinking=False, regenerate=True)

    async def test_tool_reported_truncation_is_visible_without_harness_clipping(self):
        await self.check_tool_round(streaming=False, thinking=False, source_truncated=True)
