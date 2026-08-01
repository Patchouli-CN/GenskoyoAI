"""Agent 装配收敛回归测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from GensokyoAI.core.agent import Agent
from GensokyoAI.core.agent.composition import AgentComposition
from GensokyoAI.core.agent.model_client import ModelClient
from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.runtime_context import AgentDependencies, AgentRuntimeContext
from GensokyoAI.core.agent.types import UnifiedResponse
from GensokyoAI.core.config import AppConfig, CharacterConfig, ModelConfig, SessionConfig
from GensokyoAI.core.events import SystemEvent
from GensokyoAI.memory.semantic import SemanticMemoryManager
from GensokyoAI.memory.working import WorkingMemoryManager
from GensokyoAI.runtime.resource_control import build_resource_gates
from GensokyoAI.session.manager import SessionManager
from GensokyoAI.tools.executor import ToolExecutor
from GensokyoAI.tools.registry import ToolRegistry
from GensokyoAI.utils.helpers import build_world_memory_root


class _CompositionProvider(BaseProvider):
    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        return UnifiedResponse(model=model)

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        if False:
            yield None


class AgentCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("composition_test", _CompositionProvider)

    def _make_config(self, tmp: str) -> AppConfig:
        return AppConfig(
            character=CharacterConfig(name="Reimu", system_prompt="你是灵梦。"),
            model=ModelConfig(provider="composition_test", name="test-model"),
            session=SessionConfig(save_path=Path(tmp)),
        )

    def test_composition_builds_runtime_context_with_expected_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._make_config(tmp)

            context = AgentComposition(config, "Reimu").build()

            self.assertIsInstance(context, AgentRuntimeContext)
            self.assertFalse(context.event_bus.enable_trace)
            self.assertEqual(context.memory_base_path, Path(tmp))
            self.assertIs(context.model_client.config, config.model)
            self.assertIsInstance(context.tool_registry, ToolRegistry)
            self.assertIsInstance(context.tool_executor, ToolExecutor)
            self.assertIsInstance(context.session_manager, SessionManager)

    def test_agent_constructor_maps_runtime_context_without_starting_background_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
                agent = Agent(config=self._make_config(tmp))

            self.assertIs(agent.event_bus, agent.runtime_context.event_bus)
            self.assertIs(agent._model_client, agent.runtime_context.model_client)
            self.assertIs(agent.tool_registry, agent.runtime_context.tool_registry)
            self.assertIs(agent.tool_executor, agent.runtime_context.tool_executor)
            self.assertIs(agent.session_manager, agent.runtime_context.session_manager)
            self.assertIsNone(agent._background_manager)
            self.assertIsNone(agent._think_engine)
            self.assertIsNone(agent._action_executor)

    def test_agent_lazy_memory_components_reset_between_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
                agent = Agent(config=self._make_config(tmp))

            first_session = agent.create_session()
            first_working_memory = agent.working_memory
            first_semantic_memory = agent.semantic_memory

            second_session = agent.create_session()

            self.assertNotEqual(first_session.session_id, second_session.session_id)
            self.assertIsInstance(first_working_memory, WorkingMemoryManager)
            self.assertIsInstance(first_semantic_memory, SemanticMemoryManager)
            self.assertIsNot(agent.working_memory, first_working_memory)
            self.assertIsNot(agent.semantic_memory, first_semantic_memory)

    def test_agent_bootstrap_state_tracks_lazy_components_created_after_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
                agent = Agent(config=self._make_config(tmp))

            session = agent.create_session()
            self.assertIsNotNone(session)

            builder = agent.message_builder
            saver = agent.save_coordinator
            handler = agent.response_handler

            self.assertIs(agent._lazy_components.message_builder, builder)
            self.assertIs(agent._lazy_components.save_coordinator, saver)
            self.assertIs(agent._lazy_components.response_handler, handler)
            self.assertIs(agent._lazy_components.lifecycle, agent.lifecycle)

    def test_agent_constructor_publishes_started_event_on_runtime_event_bus(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
                agent = Agent(config=self._make_config(tmp))

            started_event = agent.event_bus._event_queue.get_nowait()

            self.assertEqual(started_event.type, SystemEvent.AGENT_STARTED)
            self.assertEqual(started_event.source, "agent")
            self.assertEqual(started_event.data["character"], "Reimu")

    def test_actor_id_defaults_to_character_name_without_deps(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = AgentComposition(self._make_config(tmp), "Reimu").build()

            self.assertEqual(context.actor_id, "Reimu")
            self.assertIsNone(context.world_id)

    def test_composition_reuses_injected_model_client_and_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._make_config(tmp)
            shared_client = ModelClient(config.model)
            shared_gates = build_resource_gates(config.resource_control)
            deps = AgentDependencies(
                model_client=shared_client,
                resource_gates=shared_gates,
                actor_id="marisa",
                world_id="gensokyo",
            )

            context = AgentComposition(config, "KirisameMarisa", deps).build()

            # 共享 ModelClient / gates 被复用，而非自建
            self.assertIs(context.model_client, shared_client)
            self.assertIs(context.resource_gates, shared_gates)
            self.assertIs(context.tool_executor._resource_gates, shared_gates)
            # 稳定 actor 身份优先于角色显示名
            self.assertEqual(context.actor_id, "marisa")
            self.assertEqual(context.world_id, "gensokyo")
            self.assertEqual(context.tool_executor._actor_id, "marisa")
            self.assertEqual(context.tool_executor._world_id, "gensokyo")

    def test_two_actors_share_model_client_but_isolate_bus_and_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._make_config(tmp)
            shared_client = ModelClient(config.model)
            shared_gates = build_resource_gates(config.resource_control)

            ctx_a = AgentComposition(
                config,
                "KirisameMarisa",
                AgentDependencies(
                    model_client=shared_client, resource_gates=shared_gates, actor_id="marisa"
                ),
            ).build()
            ctx_b = AgentComposition(
                config,
                "PatchouliKnowledge",
                AgentDependencies(
                    model_client=shared_client, resource_gates=shared_gates, actor_id="patchouli"
                ),
            ).build()

            # 共享大脑：同一个 ModelClient 对象
            self.assertIs(ctx_a.model_client, ctx_b.model_client)
            # 私有状态：事件总线 / 会话 / 场景各自独立，绝不串台
            self.assertIsNot(ctx_a.event_bus, ctx_b.event_bus)
            self.assertIsNot(ctx_a.session_manager, ctx_b.session_manager)
            self.assertIsNot(ctx_a.scene_manager, ctx_b.scene_manager)
            self.assertIsNot(ctx_a.tool_executor, ctx_b.tool_executor)
            # actor 身份区分
            self.assertEqual(ctx_a.actor_id, "marisa")
            self.assertEqual(ctx_b.actor_id, "patchouli")

    def test_single_actor_semantic_memory_keeps_session_scoped_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
                agent = Agent(config=self._make_config(tmp))
            session = agent.create_session()

            memory = agent.semantic_memory

            self.assertEqual(
                memory.store.path,
                Path(tmp) / "Reimu" / "memory" / session.session_id / "topics.json",
            )

    def test_world_semantic_memory_uses_injected_root_across_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._make_config(tmp)
            memory_root = build_world_memory_root(Path(tmp), "gensokyo", "Reimu")
            dependencies = AgentDependencies(
                actor_id="reimu",
                world_id="gensokyo",
                semantic_memory_root=memory_root,
            )
            with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
                agent = Agent(config=config, dependencies=dependencies)

            agent.create_session()
            first_memory = agent.semantic_memory
            agent.create_session()
            second_memory = agent.semantic_memory

            self.assertIsNot(first_memory, second_memory)
            self.assertEqual(first_memory.store.path, memory_root / "topics.json")
            self.assertEqual(second_memory.store.path, memory_root / "topics.json")
            self.assertEqual(agent.runtime_context.semantic_memory_root, memory_root)

    def test_world_memory_roots_isolate_worlds_and_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reimu_a = build_world_memory_root(base, "world/a", "Reimu/博丽")
            reimu_b = build_world_memory_root(base, "world/b", "Reimu/博丽")
            marisa_a = build_world_memory_root(base, "world/a", "Marisa")

            self.assertEqual(reimu_a, base / "world" / "world_a" / "memory" / "Reimu_博丽")
            self.assertNotEqual(reimu_a, reimu_b)
            self.assertNotEqual(reimu_a, marisa_a)


if __name__ == "__main__":
    unittest.main()
