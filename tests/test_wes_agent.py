import unittest
from unittest.mock import patch

from core.model import EchoModel, ScriptedModel
from core.tool import Tool
from core.types import ModelResponse, ToolCall, ToolResult
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
    async def test_prompt_supports_direct_general_and_generic_wes_answers(self):
        agent = WESAgent(model=EchoModel())
        self.assertIn("answer directly from your own knowledge", agent.system_prompt)
        self.assertIn("Do not refuse a generic WES question", agent.system_prompt)
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
