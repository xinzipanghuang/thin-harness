import unittest

from core.mcp_provider import DiscoveredMcpTool, McpEndpoint, McpToolProvider
from core.artifacts import ArtifactStore
from core.tool import ToolContext, execute_tool
from core.types import ToolCall


class FakeTransport:
    async def list_tools(self):
        return [DiscoveredMcpTool(
            name="hub.wes.sample.find", description="Locate a WES sample.",
            input_schema={"type": "object", "properties": {
                "sample_id": {"type": "string"}}, "required": ["sample_id"],
                "additionalProperties": False},
        )]

    async def call_tool(self, name, arguments):
        return {"name": name, "sample_id": arguments["sample_id"]}


class BrokenTransport:
    async def list_tools(self):
        raise ConnectionError("offline")

    async def call_tool(self, name, arguments):
        raise AssertionError("not reached")


def endpoint():
    return McpEndpoint("central", "http://127.0.0.1:8866/mcp", "HUB_TOKEN")


class McpProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_and_calls_remote_tool(self):
        provider = McpToolProvider(endpoint(), transport=FakeTransport())
        tools = await provider.load_tools()
        self.assertEqual([item.name for item in tools], ["hub.wes.sample.find"])
        self.assertFalse(tools[0].to_schema()["parameters"]["additionalProperties"])
        result = await tools[0].fn(sample_id="S1")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["sample_id"], "S1")
        context = ToolContext(artifact_store=ArtifactStore())
        executed = await execute_tool(
            tools[0], context,
            ToolCall(name=tools[0].name, arguments={"sample_id": "S2"}),
            timeout=2, max_chars=1000,
        )
        self.assertTrue(executed.ok)
        self.assertEqual(executed.data["sample_id"], "S2")

    async def test_optional_endpoint_does_not_break_local_client(self):
        provider = McpToolProvider(endpoint(), transport=BrokenTransport())
        self.assertEqual(await provider.load_tools(), [])
        self.assertIn("offline", provider.last_error)
