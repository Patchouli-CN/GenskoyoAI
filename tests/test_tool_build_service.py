import unittest

from GensokyoAI.core.agent import Agent
from GensokyoAI.core.agent.message_builder import MessageBuilder
from GensokyoAI.core.agent.types import ModelInfo, ProviderCapability
from GensokyoAI.core.config import ModelConfig, ToolConfig, WebSearchToolConfig
from GensokyoAI.core.events import EventBus, SystemEvent
from GensokyoAI.tools.base import tool
from GensokyoAI.tools.build_service import ToolBuildContext, ToolBuildResult, ToolBuildService
from GensokyoAI.tools.registry import ToolRegistry


class _EmptyMemory:
    def get_context(self):
        return []

    def get_relevant_context(self, _query):
        return []


@tool(name="p8_custom_tool", description="P8 custom test tool")
def p8_custom_tool(query: str) -> str:
    return query


class _RegistryOnlyModelService:
    async def get_model_info(self, config):
        return ModelInfo(id=config.name, name=config.name, capabilities=[ProviderCapability.CHAT])


class _ProviderWithTools:
    capabilities = [ProviderCapability.CHAT, ProviderCapability.STREAM, ProviderCapability.TOOLS]


class ToolBuildServiceTests(unittest.TestCase):
    def test_build_injects_enabled_tool_schemas_when_model_supports_tools(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)

        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=True, builtin_tools=["time"]),
                model_config=ModelConfig(provider="openai", name="gpt-4o"),
                model_capabilities={ProviderCapability.CHAT, ProviderCapability.TOOLS},
            )
        )

        names = [schema["function"]["name"] for schema in result.tools]
        self.assertIn("get_current_dateinfo", names)
        self.assertIn("get_current_time", names)
        self.assertIn("get_current_time", result.instructions)
        self.assertTrue(result.model_supports_tools)

    def test_build_returns_instructions_without_schemas_when_model_lacks_tool_capability(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)

        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=True, builtin_tools=["time"]),
                model_config=ModelConfig(provider="openai", name="text-only"),
                model_capabilities={ProviderCapability.CHAT, ProviderCapability.STREAM},
            )
        )

        self.assertEqual(result.tools, [])
        self.assertIn("未声明支持结构化 tool calling", result.instructions)
        self.assertIn("get_current_time", result.enabled_tool_names)
        self.assertFalse(result.model_supports_tools)

    def test_tool_config_disabled_disables_all_tools_and_instructions(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)

        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=False),
                model_config=ModelConfig(),
                model_capabilities={ProviderCapability.TOOLS},
            )
        )

        self.assertEqual(result.tools, [])
        self.assertEqual(result.instructions, "")
        self.assertEqual(result.disabled_reasons["*"], "tool_config_disabled")

    def test_builtin_tools_allowlist_filters_builtin_modules_but_keeps_custom_tools(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)

        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=True, builtin_tools=["moon"]),
                model_config=ModelConfig(),
                model_capabilities={ProviderCapability.TOOLS},
            )
        )

        names = [schema["function"]["name"] for schema in result.tools]
        self.assertIn("get_moon_phase", names)
        self.assertIn("p8_custom_tool", names)
        self.assertNotIn("get_current_time", names)
        self.assertEqual(result.disabled_reasons["get_current_time"], "not_in_builtin_tools")

    def test_runtime_availability_filters_tools_with_stable_disabled_reason(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)

        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=True, builtin_tools=["time"]),
                model_config=ModelConfig(provider="openai", name="gpt-4o"),
                model_capabilities={ProviderCapability.CHAT, ProviderCapability.TOOLS},
                runtime_available_tools={"get_current_time"},
            )
        )

        names = [schema["function"]["name"] for schema in result.tools]
        self.assertEqual(names, ["get_current_time"])
        self.assertEqual(result.enabled_tool_names, ["get_current_time"])
        self.assertEqual(result.disabled_reasons["get_current_dateinfo"], "runtime_unavailable")

    def test_runtime_empty_availability_disables_all_tools(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)

        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(enabled=True, builtin_tools=["time"]),
                model_config=ModelConfig(provider="openai", name="gpt-4o"),
                model_capabilities={ProviderCapability.CHAT, ProviderCapability.TOOLS},
                runtime_available_tools=set(),
            )
        )

        self.assertEqual(result.tools, [])
        self.assertEqual(result.enabled_tool_names, [])
        self.assertEqual(result.disabled_reasons["get_current_time"], "runtime_unavailable")
        self.assertEqual(result.disabled_reasons["get_current_dateinfo"], "runtime_unavailable")

    def test_web_search_tool_respects_tool_config_and_provider_builtin_search(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)
        tool_config = ToolConfig(
            enabled=True,
            builtin_tools=["web_search"],
            web_search=WebSearchToolConfig(enabled=True, trigger_strategy="auto"),
        )

        enabled = service.build(
            ToolBuildContext(
                tool_config=tool_config,
                model_config=ModelConfig(web_search_enabled=False),
                model_capabilities={ProviderCapability.TOOLS},
            )
        )
        enabled_names = [schema["function"]["name"] for schema in enabled.tools]
        self.assertIn("web_search", enabled_names)

        provider_builtin = service.build(
            ToolBuildContext(
                tool_config=tool_config,
                model_config=ModelConfig(web_search_enabled=True, web_search_strategy="auto"),
                model_capabilities={ProviderCapability.TOOLS},
            )
        )
        provider_builtin_names = [schema["function"]["name"] for schema in provider_builtin.tools]
        self.assertNotIn("web_search", provider_builtin_names)
        self.assertIn("Provider 内置联网搜索", provider_builtin.instructions)

    def test_web_search_tool_schema_contract_is_stable(self):
        registry = ToolRegistry()
        service = ToolBuildService(registry)
        result = service.build(
            ToolBuildContext(
                tool_config=ToolConfig(
                    enabled=True,
                    builtin_tools=["web_search"],
                    web_search=WebSearchToolConfig(enabled=True, trigger_strategy="auto"),
                ),
                model_config=ModelConfig(web_search_enabled=False),
                model_capabilities={ProviderCapability.TOOLS},
            )
        )

        schemas = {schema["function"]["name"]: schema for schema in result.tools}
        schema = schemas["web_search"]["function"]
        parameters = schema["parameters"]
        properties = parameters["properties"]

        self.assertEqual(schema["name"], "web_search")
        self.assertIn("联网搜索", schema["description"])
        self.assertEqual(parameters["type"], "object")
        self.assertEqual(parameters["required"], ["query"])
        self.assertEqual(set(properties), {"query", "max_results", "provider", "time_range"})
        self.assertEqual(properties["query"]["type"], "string")
        self.assertEqual(properties["max_results"]["type"], "integer")
        self.assertEqual(properties["max_results"]["default"], 5)
        self.assertEqual(properties["provider"]["type"], "string")
        self.assertEqual(properties["provider"]["default"], "")
        self.assertEqual(properties["time_range"]["type"], "string")
        self.assertEqual(properties["time_range"]["default"], "")

    def test_message_builder_uses_tool_build_result_instructions(self):
        builder = MessageBuilder(
            system_prompt="system",
            working_memory=_EmptyMemory(),
            semantic_memory=_EmptyMemory(),
            tool_build_result=ToolBuildResult(instructions="【可用工具】\n- test: instruction"),
        )

        self.assertIn("test: instruction", builder.system_prompt)
        self.assertNotIn("Provider 内置联网搜索", builder.system_prompt)

    def test_agent_publishes_tool_selected_event_payload(self):
        event_bus = EventBus(enable_trace=False)
        events = []
        event_bus.subscribe(SystemEvent.TOOL_CALL_SELECTED, lambda event: events.append(event))
        agent = Agent.__new__(Agent)
        agent.event_bus = event_bus

        result = ToolBuildResult(
            tools=[{"type": "function"}],
            instructions="instruction",
            enabled_tool_names=["get_current_time"],
            disabled_reasons={"web_search": "not_in_builtin_tools"},
            model_supports_tools=True,
        )

        async def run_and_flush():
            agent._publish_tool_selected_event(result)
            while not event_bus._event_queue.empty():
                event = await event_bus._event_queue.get()
                await event_bus._process_event(event)
                event_bus._event_queue.task_done()

        import asyncio

        asyncio.run(run_and_flush())

        self.assertEqual(len(events), 1)
        data = events[0].data
        self.assertEqual(data["enabled_tool_names"], ["get_current_time"])
        self.assertEqual(data["tool_count"], 1)
        self.assertTrue(data["model_supports_tools"])
        self.assertTrue(data["has_instructions"])
        self.assertEqual(data["disabled_reasons"], {"web_search": "not_in_builtin_tools"})

    def test_agent_build_tools_uses_model_registry_capabilities_over_provider_capabilities(self):
        agent = Agent.__new__(Agent)
        agent.config = type(
            "Config",
            (),
            {
                "tool": ToolConfig(enabled=True, builtin_tools=["time"]),
                "model": ModelConfig(provider="openai", name="registry-text-only"),
            },
        )()
        agent.character_name = "test"
        agent._model_client = type("ModelClient", (), {"_provider": _ProviderWithTools()})()
        agent.tool_build_service = ToolBuildService(ToolRegistry())
        agent.model_registry_service = _RegistryOnlyModelService()
        event_bus = EventBus(enable_trace=False)
        agent.event_bus = event_bus

        import asyncio

        result = asyncio.run(agent._build_tools())

        self.assertFalse(result.model_supports_tools)
        self.assertEqual(result.tools, [])
        self.assertIn("model_does_not_support_tools", result.disabled_reasons["*"])
        self.assertIn("get_current_time", result.enabled_tool_names)


class ToolTypeMappingTests(unittest.TestCase):
    """06#6：typing 泛型解包——list[str]/Optional[X]/X | None 不再退化成 STRING。"""

    def test_generic_types_map_to_structural_schema(self):
        from GensokyoAI.tools.base import ToolParameterType, build_tool_definition

        def sample(
            tags: list[str],
            meta: dict[str, int],
            maybe: str | None,
            plain: bool,
        ) -> str:
            return ""

        definition = build_tool_definition(sample, name="sample", description="sample tool")
        by_name = {name: param.type for name, param in definition.parameters.items()}

        self.assertEqual(by_name["tags"], ToolParameterType.ARRAY)
        self.assertEqual(by_name["meta"], ToolParameterType.OBJECT)
        self.assertEqual(by_name["maybe"], ToolParameterType.STRING)
        self.assertEqual(by_name["plain"], ToolParameterType.BOOLEAN)


class MoonPhaseTests(unittest.TestCase):
    """06#5：月相工具按真实朔望周期计算。"""

    def test_output_is_valid_phase_and_tracks_synodic_cycle(self):
        from GensokyoAI.tools.tool_builtin.moon import _PHASES, get_moon_phase

        today = get_moon_phase(0)
        self.assertIn(today, _PHASES)
        # 朔望月 ~29.53 天：30 天内月相应遍历绝大多数相位（而非 day%8 的假周期）
        seen = {get_moon_phase(day) for day in range(0, 30)}
        self.assertGreaterEqual(len(seen), 6)


if __name__ == "__main__":
    unittest.main()
