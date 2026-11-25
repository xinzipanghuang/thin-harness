import os
import unittest
from unittest.mock import patch

from core.model import EchoModel, ScriptedModel
from core.tool import Tool
from core.types import ModelResponse, RuntimeConfig, ToolCall, ToolResult
from domains.wes.agent import WESAgent


async def remote_sample(sample_id: str):
    return ToolResult(ok=True, summary="resolved", data={"sample_id": sample_id})


class FakeProvider:
    last_error = None

    def __init__(self, endpoint, optional=True):
        self.endpoint = endpoint

    async def load_tools(self):
        return [Tool(name="wes_resolve_sample", fn=remote_sample)]


class CompatibilityProvider(FakeProvider):
    async def load_tools(self):
        return [Tool(name="sample_overview", fn=remote_sample)]


class WESAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_limits_can_be_configured_without_overriding_explicit_runtime(self):
        with patch.dict(os.environ, {"WES_MAX_STEPS": "72", "WES_MAX_TOOL_CALLS": "180", "WES_REQUEST_TIMEOUT": "2100"}):
            agent = WESAgent(model=EchoModel())
            self.assertEqual((agent.runtime.max_steps, agent.runtime.max_tool_calls, agent.runtime.request_timeout), (72, 180, 2100))
            explicit = WESAgent(model=EchoModel(), runtime=RuntimeConfig(max_steps=2, max_tool_calls=3, request_timeout=4))
            self.assertEqual((explicit.runtime.max_steps, explicit.runtime.max_tool_calls, explicit.runtime.request_timeout), (2, 3, 4))
        with patch.dict(os.environ, {"WES_MAX_TOOL_CALLS": "0"}):
            with self.assertRaisesRegex(ValueError, "WES_MAX_TOOL_CALLS"):
                WESAgent(model=EchoModel())

    async def test_prompt_supports_wes_knowledge_usage_and_scope_boundaries(self):
        agent = WESAgent(model=EchoModel())
        self.assertIn("questions about using this product", agent.system_prompt)
        self.assertIn("Do not refuse a generic WES question", agent.system_prompt)
        self.assertIn("requests unrelated to WES", agent.system_prompt)
        self.assertIn("Lead every user-facing answer with the direct conclusion", agent.system_prompt)

    async def test_dynamic_mcp_tool_is_available_to_first_model_call(self):
        model = ScriptedModel([
            ModelResponse(tool_calls=[ToolCall(name="sample_overview", arguments={"sample_id": "S1"})]),
            ModelResponse(text="已定位样本 S1。"),
        ])
        agent = WESAgent(model=model)
        with patch("domains.wes.agent.McpToolProvider", CompatibilityProvider):
            result = await agent.run("查询样本 S1")
        self.assertEqual(result.text, "已定位样本 S1。")
        self.assertIn("sample_overview", [item["name"] for item in model.calls[0][1]])

    async def test_loads_consolidated_mcp_catalog_before_run(self):
        agent = WESAgent(model=EchoModel())
        with patch("domains.wes.agent.McpToolProvider", FakeProvider):
            await agent.on_run_start("复核样本 S1")
        self.assertIn("wes_resolve_sample", [tool.name for tool in agent.tools])
        observations = await agent.bootstrap(None)
        self.assertEqual(observations[0].status, "success")
        self.assertIn("wes_resolve_sample", observations[0].preview)

    async def test_uses_compatibility_catalog_during_migration(self):
        agent = WESAgent(model=EchoModel())
        with patch("domains.wes.agent.McpToolProvider", CompatibilityProvider):
            await agent.on_run_start("复核样本 S1")
        self.assertIn("sample_overview", [tool.name for tool in agent.tools])
