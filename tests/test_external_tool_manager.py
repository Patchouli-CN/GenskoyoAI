import asyncio
import unittest

from GensokyoAI.core.agent.types import ProviderCapability
from GensokyoAI.core.config import ModelConfig, ToolConfig
from GensokyoAI.core.events import SystemEvent
from GensokyoAI.runtime.rpc import external_tool_status_methods, legacy_rpc_methods, rpc_methods
from GensokyoAI.runtime.service import RuntimeService
from GensokyoAI.tools.build_service import ToolBuildContext, ToolBuildService
from GensokyoAI.tools.errors import ToolExecutionError
from GensokyoAI.tools.executor import ToolExecutor
from GensokyoAI.tools.external_manager import (
    ExternalToolDefinition,
    ExternalToolExecutionPolicy,
    ExternalToolManager,
    ExternalToolSourceStatus,
    is_external_tool_name,
    make_external_tool_name,
    split_external_tool_name,
)
from GensokyoAI.tools.mcp import McpSource
from GensokyoAI.tools.registry import ToolRegistry


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "external test tool",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


class FakeExternalSource:
    def __init__(
        self, source_id="server", *, fail_list=False, metadata=None, result=None, delay=0.0
    ):
        self.source_id = source_id
        self.fail_list = fail_list
        self.metadata = metadata or {"transport": "fake"}
        self.result = result
        self.delay = delay
        self.calls = []

    async def start(self):
        return None

    async def stop(self):
        return None

    async def list_tools(self):
        if self.fail_list:
            raise RuntimeError("list failed")
        return [
            ExternalToolDefinition(
                source_id=self.source_id,
                tool_name="search",
                namespaced_name=make_external_tool_name(self.source_id, "search"),
                description="External search",
                schema=_schema("search"),
                metadata=self.metadata,
            )
        ]

    async def call_tool(self, tool_name, arguments):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append((tool_name, arguments))
        if self.result is not None:
            return self.result
        return {"tool_name": tool_name, "arguments": arguments}


class FakeMcpTransport:
    def __init__(self):
        self.started = False
        self.requests = []

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    async def request(self, method, params=None):
        self.requests.append((method, params or {}))
        if method == "initialize":
            return {"serverInfo": {"name": "fake"}}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Lookup things",
                        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        if method == "tools/call":
            assert params is not None
            return {"content": [{"type": "text", "text": params["arguments"]["q"]}]}
        raise AssertionError(method)


class ExternalToolManagerContractTests(unittest.TestCase):
    def test_namespaced_external_tool_name_contract(self):
        name = make_external_tool_name("server", "search")

        self.assertEqual(name, "external__server__search")
        self.assertTrue(is_external_tool_name(name))
        self.assertEqual(split_external_tool_name(name), ("server", "search"))

        with self.assertRaises(ValueError):
            make_external_tool_name("bad__server", "search")
        with self.assertRaises(ValueError):
            split_external_tool_name("search")

    def test_manager_lists_tools_with_source_failure_isolation(self):
        manager = ExternalToolManager()
        manager.register_source(FakeExternalSource("ok"))
        manager.register_source(FakeExternalSource("bad", fail_list=True))

        async def run():
            return await manager.list_tools(refresh=True), manager.source_status()

        tools, status = asyncio.run(run())

        self.assertEqual([tool.namespaced_name for tool in tools], ["external__ok__search"])
        states = {source["source_id"]: source for source in status["sources"]}
        self.assertEqual(states["ok"]["tool_count"], 1)
        self.assertEqual(states["bad"]["status"], ExternalToolSourceStatus.FAILED.value)
        self.assertEqual(states["bad"]["tool_count"], 0)
        self.assertIn("list failed", states["bad"]["error"])
        self.assertIn("policy", status)

    def test_manager_call_tool_proxies_to_source_without_registry_pollution(self):
        manager = ExternalToolManager()
        source = FakeExternalSource("server")
        manager.register_source(source)

        async def run():
            await manager.list_tools(refresh=True)
            return await manager.call_tool("external__server__search", {"q": "test"})

        result = asyncio.run(run())

        self.assertEqual(result["tool_name"], "search")
        self.assertEqual(source.calls, [("search", {"q": "test"})])
        self.assertIsNone(ToolRegistry().get("external__server__search"))

    def test_manager_rejects_unknown_tool_without_listing(self):
        """fail-closed：工具不在源列表（未先 list_tools）时拒绝调用，不再盲调。"""
        manager = ExternalToolManager()
        source = FakeExternalSource("server")
        manager.register_source(source)

        async def run():
            with self.assertRaises(ToolExecutionError) as ctx:
                await manager.call_tool("external__server__search", {"q": "test"})
            return ctx

        ctx = asyncio.run(run())
        self.assertEqual(ctx.exception.error.error_code, "external_tool.not_found")
        self.assertEqual(ctx.exception.error.details["tool_name"], "search")
        self.assertEqual(source.calls, [])  # 未盲调

    def test_manager_denies_risky_external_tool_permissions(self):
        manager = ExternalToolManager()
        manager.register_source(
            FakeExternalSource(
                "server", metadata={"permissions": ["filesystem"], "transport": "fake"}
            )
        )

        async def run():
            await manager.list_tools(refresh=True)
            return await ToolExecutor(ToolRegistry(), external_tool_manager=manager).execute(
                {"id": "call-1", "name": "external__server__search", "arguments": {"path": "x"}}
            )

        result = asyncio.run(run())

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "external_tool.permission_denied")
        self.assertEqual(result["error"]["details"]["permissions"], ["filesystem"])

    def test_manager_truncates_large_external_tool_output(self):
        manager = ExternalToolManager(
            ExternalToolExecutionPolicy(max_output_chars=12, timeout_seconds=1.0)
        )
        manager.register_source(FakeExternalSource("server", result="x" * 40))

        async def run():
            await manager.list_tools(refresh=True)
            return await manager.call_tool("external__server__search", {})

        result = asyncio.run(run())

        self.assertTrue(result["truncated"])
        self.assertEqual(result["content"], "x" * 12)
        self.assertEqual(result["original_length"], 40)

    def test_manager_times_out_external_tool_call(self):
        manager = ExternalToolManager(
            ExternalToolExecutionPolicy(timeout_seconds=0.01, max_output_chars=100)
        )
        manager.register_source(FakeExternalSource("server", delay=0.05))

        async def run():
            # fail-closed 语义下需先 list_tools 让工具入列，才能走到超时路径
            await manager.list_tools(refresh=True)
            return await ToolExecutor(ToolRegistry(), external_tool_manager=manager).execute(
                {"id": "call-1", "name": "external__server__search", "arguments": {}}
            )

        result = asyncio.run(run())

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "external_tool.timeout")


class ExternalToolRuntimeAndBuildTests(unittest.TestCase):
    def test_runtime_exposes_external_tool_status_rpc(self):
        service = RuntimeService()
        service.external_tool_manager.register_source(FakeExternalSource("server"))

        async def run():
            await service.external_tool_manager.list_tools(refresh=True)
            status = await service.handle("external_tool.status")
            legacy = await service.handle("external_tool_status", {"include_tools": False})
            info = await service.handle("runtime.info")
            return status, legacy, info

        status, legacy, info = asyncio.run(run())

        self.assertIn("external_tool.status", rpc_methods())
        self.assertIn("external_tool_status", legacy_rpc_methods())
        self.assertEqual(status["source_count"], 1)
        self.assertEqual(status["tool_count"], 1)
        self.assertEqual(
            status["sources"][0]["tools"][0]["namespaced_name"], "external__server__search"
        )
        self.assertNotIn("tools", legacy["sources"][0])
        self.assertIn("external_tools", info)

    def test_external_tool_status_event_contract_is_explicit(self):
        mapping = external_tool_status_methods()

        self.assertEqual(mapping["starting"], SystemEvent.EXTERNAL_TOOL_STARTING.value)
        self.assertEqual(mapping["running"], SystemEvent.EXTERNAL_TOOL_RUNNING.value)
        self.assertEqual(mapping["stopping"], SystemEvent.EXTERNAL_TOOL_STOPPING.value)
        self.assertEqual(mapping["failed"], SystemEvent.EXTERNAL_TOOL_FAILED.value)
        self.assertEqual(mapping["reconnecting"], SystemEvent.EXTERNAL_TOOL_RECONNECTING.value)

    def test_tool_build_service_accepts_external_schemas_without_registry_registration(self):
        external = ExternalToolDefinition(
            source_id="server",
            tool_name="search",
            namespaced_name="external__server__search",
            description="External search",
            schema=_schema("search"),
        )
        registry = ToolRegistry()
        service = ToolBuildService(registry)

        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=True, builtin_tools=[]),
                model_config=ModelConfig(),
                model_capabilities={ProviderCapability.CHAT, ProviderCapability.TOOLS},
                external_tools=[external],
            )
        )

        names = [schema["function"]["name"] for schema in result.tools]
        self.assertIn("external__server__search", names)
        self.assertIn("external__server__search", result.enabled_tool_names)
        self.assertIn("external__server__search", result.instructions)
        self.assertIsNone(registry.get("external__server__search"))

    def test_tool_build_service_filters_external_tools_requiring_confirmation(self):
        external = ExternalToolDefinition(
            source_id="server",
            tool_name="delete_file",
            namespaced_name="external__server__delete_file",
            description="Delete file",
            schema=_schema("delete_file"),
            metadata={"permissions": ["destructive"]},
        )

        result = ToolBuildService(ToolRegistry()).build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=True, builtin_tools=[]),
                model_config=ModelConfig(),
                model_capabilities={ProviderCapability.TOOLS},
                external_tools=[external],
            )
        )

        self.assertNotIn("external__server__delete_file", result.enabled_tool_names)
        self.assertEqual(
            result.disabled_reasons["external__server__delete_file"],
            "external_permission_requires_confirmation",
        )

    def test_tool_executor_proxies_external_tool_calls(self):
        manager = ExternalToolManager()
        source = FakeExternalSource("server")
        manager.register_source(source)

        async def run():
            await manager.list_tools(refresh=True)
            executor = ToolExecutor(ToolRegistry(), external_tool_manager=manager)
            return await executor.execute(
                {"id": "call-1", "name": "external__server__search", "arguments": {"q": "test"}}
            )

        result = asyncio.run(run())

        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["name"], "external__server__search")
        self.assertEqual(source.calls, [("search", {"q": "test"})])
        self.assertIn('"tool_name": "search"', result["content"])

    def test_mcp_source_lists_and_calls_tools_via_transport(self):
        transport = FakeMcpTransport()
        source = McpSource("mcpserver", transport)

        async def run():
            tools = await source.list_tools()
            result = await source.call_tool("lookup", {"q": "hello"})
            await source.stop()
            return tools, result

        tools, result = asyncio.run(run())

        self.assertEqual(tools[0].namespaced_name, "external__mcpserver__lookup")
        self.assertEqual(tools[0].permissions, {"read-only"})
        self.assertEqual(result["content"][0]["text"], "hello")
        self.assertEqual(
            [request[0] for request in transport.requests],
            ["initialize", "tools/list", "tools/call"],
        )


if __name__ == "__main__":
    unittest.main()
