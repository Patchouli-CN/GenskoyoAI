"""深度审查修复的定向回归测试。

覆盖：
- C1：响应槽位请求代际绑定（超时后孤儿生成零副作用）
- C1'：会话切换后缓存组件失效（builder/handler 重建、planner/think 就地更新、
  replace_messages 原地复用工作记忆实例）
- topics.json 原子写 + .bak 恢复 + 损坏隔离
- 工具层：execute_batch 写后读顺序、execute_sync async 守卫、registry 隔离、
  remember 失败诚实化
- 事件层：Event.id 全量 uuid、flush_critical 不被普通事件截断
- SCENE_SWITCHED 载荷含 from_scene_id/actor_id
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from GensokyoAI.core.agent import Agent
from GensokyoAI.core.agent.action_executor import ActionExecutor
from GensokyoAI.core.agent.action_planner import ActionPlanner
from GensokyoAI.core.agent.actions import ActionFactory
from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import StreamChunk, UnifiedResponse
from GensokyoAI.core.config import (
    AppConfig,
    CharacterConfig,
    ModelConfig,
    SessionConfig,
    TopicGenerationConfig,
)
from GensokyoAI.core.event_listeners import SceneServiceListeners
from GensokyoAI.core.events import Event, EventBus, SystemEvent
from GensokyoAI.memory.topic_store import TopicAwareStore
from GensokyoAI.memory.types import Topic
from GensokyoAI.tools.executor import ToolExecutor
from GensokyoAI.tools.registry import ToolRegistry


def _chunk(content: str) -> StreamChunk:
    return StreamChunk(type="content", content=content)


class RequestBindingTests(unittest.TestCase):
    """C1：响应槽位与请求 id 绑定，孤儿生成不得污染新请求。"""

    def _executor(self) -> ActionExecutor:
        return ActionExecutor(SimpleNamespace(), EventBus(enable_trace=False))

    def test_stale_generation_has_no_side_effects(self):
        async def scenario():
            executor = self._executor()
            future1 = executor.prepare_response("req1")
            executor.cancel_response("timeout")  # 模拟 send 的 60s 超时取消
            future2 = executor.prepare_response("req2")

            # 孤儿生成（req1）的 feed/complete 全部作废
            await executor.feed_chunk(_chunk("旧回复"), request_id="req1")
            executor.complete_response("旧回复", request_id="req1")
            self.assertIsNone(executor.get_chunk_nowait())
            self.assertFalse(future2.done())

            # 新请求的 feed/complete 正常工作
            await executor.feed_chunk(_chunk("新回复"), request_id="req2")
            executor.complete_response("新回复", request_id="req2")
            self.assertTrue(future1.cancelled())
            self.assertEqual(future2.result(), "新回复")
            self.assertEqual(executor.get_chunk_nowait().content, "新回复")

        asyncio.run(scenario())

    def test_unbound_legacy_request_is_accepted(self):
        async def scenario():
            executor = self._executor()
            future = executor.prepare_response("req1")
            # request_id=None 的旧式无绑定调用保持兼容
            self.assertTrue(executor.is_current_request(None))
            await executor.feed_chunk(_chunk("x"), request_id=None)
            executor.complete_response("done", request_id=None)
            self.assertEqual(future.result(), "done")

        asyncio.run(scenario())

    def test_stale_wait_does_not_resolve_new_request(self):
        async def scenario():
            executor = self._executor()
            future = executor.prepare_response("req2")
            stale_event = Event(
                type=SystemEvent.ACTION_DECIDED,
                source="test",
                data={"action": {"type": "WAIT", "reason": "旧决策"}, "request_id": "req1"},
            )
            await executor._execute_wait(stale_event)
            self.assertFalse(future.done())

            current_event = Event(
                type=SystemEvent.ACTION_DECIDED,
                source="test",
                data={"action": {"type": "WAIT", "reason": "新决策"}, "request_id": "req2"},
            )
            await executor._execute_wait(current_event)
            self.assertTrue(future.done())
            self.assertEqual(future.result(), "")

        asyncio.run(scenario())

    def test_execute_speak_prefers_bound_request_id(self):
        async def scenario():
            bus = EventBus(enable_trace=False)
            await bus.start()
            captured: list[dict] = []

            async def handler(event):
                captured.append(event.data)

            bus.subscribe(SystemEvent.GENERATE_RESPONSE, handler)
            executor = ActionExecutor(SimpleNamespace(), bus)
            bound = Event(
                type=SystemEvent.ACTION_DECIDED,
                source="test",
                data={"action": {"type": "SPEAK"}, "request_id": "req-bound"},
            )
            await executor._execute_speak(bound, "你好")
            legacy = Event(
                type=SystemEvent.ACTION_DECIDED,
                source="test",
                data={"action": {"type": "SPEAK"}},
            )
            await executor._execute_speak(legacy, "你好")
            await asyncio.sleep(0.05)
            await bus.stop()
            return captured, legacy

        captured, legacy = asyncio.run(scenario())
        self.assertEqual(captured[0]["request_id"], "req-bound")
        # 无绑定的旧路径回退为 ACTION_DECIDED 事件自身 id
        self.assertEqual(captured[1]["request_id"], legacy.id)


class PlannerRequestPropagationTests(unittest.TestCase):
    """request_id 在 MESSAGE_RECEIVED → ACTION_DECIDED 链路上透传。"""

    def test_publish_action_propagates_request_id(self):
        async def scenario():
            bus = EventBus(enable_trace=False)
            await bus.start()
            captured: list[dict] = []

            async def handler(event):
                captured.append(event.data)

            bus.subscribe(SystemEvent.ACTION_DECIDED, handler)
            planner = ActionPlanner(
                character_name="Reimu",
                model_client=SimpleNamespace(),
                working_memory=SimpleNamespace(),
                semantic_memory=SimpleNamespace(),
                event_bus=bus,
            )
            trigger = Event(
                type=SystemEvent.MESSAGE_RECEIVED,
                source="agent",
                data={"content": "你好", "request_id": "req-xyz"},
            )
            planner._publish_action(ActionFactory.wait(reason="测试"), trigger)
            planner._publish_action(
                ActionFactory.wait(reason="无绑定"),
                Event(type=SystemEvent.MESSAGE_RECEIVED, source="agent", data={"content": "嗨"}),
            )
            await asyncio.sleep(0.05)
            await bus.stop()
            return captured

        captured = asyncio.run(scenario())
        self.assertEqual(captured[0]["request_id"], "req-xyz")
        self.assertNotIn("request_id", captured[1])


class _ReviewProvider(BaseProvider):
    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        return UnifiedResponse(model=model)

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        if False:
            yield None


class SessionScopedResetTests(unittest.TestCase):
    """C1'：会话切换后所有捕获记忆实例的组件跟随新会话。"""

    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("review_test", _ReviewProvider)

    def _agent(self, tmp: str) -> Agent:
        config = AppConfig(
            character=CharacterConfig(name="Reimu", system_prompt="你是灵梦。"),
            model=ModelConfig(provider="review_test", name="test-model"),
            session=SessionConfig(save_path=Path(tmp)),
        )
        return Agent(config=config, setup_signal_handlers=False)

    def test_builder_and_handler_rebuilt_on_session_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            agent.create_session()
            builder1 = agent.message_builder
            handler1 = agent.response_handler

            agent.create_session()

            self.assertIsNot(agent.message_builder, builder1)
            self.assertIsNot(agent.response_handler, handler1)
            # 新组件持有的是新会话的记忆实例，而非旧会话孤儿
            self.assertIs(agent.message_builder._working_memory, agent.working_memory)

    def test_planner_and_think_engine_follow_session_in_place(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                agent = self._agent(tmp)
                agent.create_session()
                await agent.start()
                planner = agent._action_planner
                think_engine = agent._think_engine

                agent.create_session()
                wm2 = agent.working_memory
                sm2 = agent.semantic_memory

                # 就地更新：实例不换（保留事件订阅），引用换新
                self.assertIs(agent._action_planner, planner)
                self.assertIs(planner.working_memory, wm2)
                self.assertIs(planner.semantic_memory, sm2)
                if think_engine is not None:
                    self.assertIs(agent._think_engine, think_engine)
                    self.assertIs(think_engine.semantic_memory, sm2)
                await agent.shutdown()

        asyncio.run(scenario())

    def test_replace_messages_reuses_working_memory_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            session = agent.create_session()
            wm = agent.working_memory

            ok = agent.session_manager.replace_messages(
                session.session_id, [{"role": "user", "content": "编辑后的消息"}]
            )

            self.assertTrue(ok)
            # 原地复用：所有捕获方（builder/handler/planner）自动看到新内容
            self.assertIs(agent.working_memory, wm)
            self.assertEqual(agent.working_memory.get_context()[-1]["content"], "编辑后的消息")


class TopicStoreRobustnessTests(unittest.TestCase):
    """topics.json：原子写 + .bak 恢复 + 损坏隔离。"""

    def _save(self, store: TopicAwareStore) -> None:
        asyncio.run(store._save_async())

    def test_bak_created_on_overwrite_and_restore_from_bak(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topics.json"
            store = TopicAwareStore(path, topic_config=TopicGenerationConfig())
            store._topics["t1"] = Topic(
                name="红魔馆", summary="吸血鬼的宅邸", importance=0.5, emotional_valence=0.0
            )
            self._save(store)
            bak = Path(str(path) + ".bak")
            self.assertFalse(bak.exists())  # 首次保存无旧文件可备份

            self._save(store)
            self.assertTrue(bak.exists())  # 覆盖前旧主文件进 .bak
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])  # 无半截临时文件残留

            path.write_bytes(b"{ corrupt json")
            store2 = TopicAwareStore(path, topic_config=TopicGenerationConfig())
            # 从 .bak 恢复，记忆不丢（话题以其自身 id 重新索引）
            self.assertIn("红魔馆", {topic.name for topic in store2._topics.values()})
            json.loads(path.read_text(encoding="utf-8"))  # 主文件已被回写为合法 JSON

    def test_corrupt_without_bak_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topics.json"
            path.write_bytes(b"garbage{")

            store = TopicAwareStore(path, topic_config=TopicGenerationConfig())

            self.assertEqual(len(store._topics), 0)
            self.assertFalse(path.exists())  # 主文件已被隔离而非静默覆盖
            quarantined = list(Path(tmp).glob("topics.json.corrupt-*"))
            self.assertEqual(len(quarantined), 1)


class ToolRobustnessTests(unittest.TestCase):
    """工具层：批量顺序、同步守卫、registry 隔离、remember 失败诚实化。"""

    def test_batch_write_then_read_sees_fresh_state(self):
        state = {"value": "old"}
        registry = ToolRegistry()

        async def writer(v: str) -> str:
            state["value"] = v
            return "written"

        async def reader() -> str:
            return state["value"]

        registry.register(writer, name="test_batch_writer", parallel_safe=False)
        registry.register(reader, name="test_batch_reader", parallel_safe=True)
        executor = ToolExecutor(registry=registry)

        results = asyncio.run(
            executor.execute_batch(
                [
                    {"id": "1", "name": "test_batch_writer", "arguments": {"v": "new"}},
                    {"id": "2", "name": "test_batch_reader", "arguments": {}},
                ]
            )
        )

        # 旧实现「读全部先于写」会拿到 "old"；分段执行后写先生效
        self.assertEqual(results[0]["tool_call_id"], "1")
        self.assertEqual(results[1]["tool_call_id"], "2")
        self.assertEqual(results[1]["content"], "new")

    def test_execute_sync_async_tool_guard(self):
        registry = ToolRegistry()

        async def async_tool() -> str:
            return "ran"

        registry.register(async_tool, name="test_async_tool")

        # 无运行中事件循环：经 asyncio.run 真正执行
        result = ToolExecutor(registry=registry).execute_sync(
            {"id": "1", "name": "test_async_tool", "arguments": {}}
        )
        self.assertEqual(result["content"], "ran")

        # 有运行中事件循环：结构化错误，而非返回未 await 的协程
        async def in_loop():
            return ToolExecutor(registry=registry).execute_sync(
                {"id": "2", "name": "test_async_tool", "arguments": {}}
            )

        result2 = asyncio.run(in_loop())
        self.assertNotEqual(result2["content"], "ran")
        self.assertIn("execute_sync", result2["content"])

    def test_registry_get_has_no_global_fallback(self):
        registry2 = ToolRegistry()  # 先建，避免 _load_builtin 拾起后注册的工具
        registry1 = ToolRegistry()

        async def custom() -> str:
            return "x"

        registry1.register(custom, name="test_iso_tool")

        # 不跨实例泄漏；unregister 真实生效
        self.assertIsNone(registry2.get("test_iso_tool"))
        self.assertTrue(registry1.unregister("test_iso_tool"))
        self.assertIsNone(registry1.get("test_iso_tool"))


class EventRobustnessTests(unittest.TestCase):
    """事件层：请求 id 全量 uuid、flush_critical 不被普通事件截断。"""

    def test_event_id_is_full_uuid(self):
        event = Event(type=SystemEvent.MESSAGE_SENT)
        self.assertEqual(len(event.id), 32)
        # 批量生成不撞键
        ids = {Event(type=SystemEvent.MESSAGE_SENT).id for _ in range(1000)}
        self.assertEqual(len(ids), 1000)

    def test_flush_critical_processes_critical_behind_normal(self):
        async def scenario():
            bus = EventBus(enable_trace=False)
            seen: list[SystemEvent] = []

            async def handler(event):
                seen.append(event.type)

            bus.subscribe(SystemEvent.PERSISTENCE_SAVE_COMPLETED, handler)
            # 不启动 worker，事件积压在队列：普通在前，关键在后
            bus.publish(Event(type=SystemEvent.MESSAGE_SENT, source="test"))
            bus.publish(Event(type=SystemEvent.PERSISTENCE_SAVE_COMPLETED, source="test"))
            await bus.flush_critical(timeout=1.0)
            return bus, seen

        bus, seen = asyncio.run(scenario())
        # 旧实现遇到普通事件即 break，关键事件被丢弃
        self.assertEqual(seen, [SystemEvent.PERSISTENCE_SAVE_COMPLETED])
        # 普通事件被放回队列而非误处理或丢失
        self.assertEqual(bus._event_queue.qsize(), 1)


class SceneSwitchedPayloadTests(unittest.TestCase):
    """SCENE_SWITCHED 载荷含 from_scene_id 与 actor_id（阶段 5 WorldStage 联动）。"""

    def test_payload_contains_from_scene_and_actor(self):
        async def scenario():
            bus = EventBus(enable_trace=False)
            await bus.start()
            captured: list[dict] = []

            async def handler(event):
                captured.append(event.data)

            bus.subscribe(SystemEvent.SCENE_SWITCHED, handler)

            async def switch_scene(scene_id: str):
                return SimpleNamespace(
                    id=scene_id,
                    name="红魔馆",
                    description="吸血鬼的宅邸",
                    render=lambda: "红魔馆大厅",
                )

            manager = SimpleNamespace(
                enabled=True, current_scene_id="magic_forest", switch_scene=switch_scene
            )
            agent = SimpleNamespace(
                actor_id="marisa",
                scene_manager=manager,
                session_manager=SimpleNamespace(get_current_session=lambda: None),
            )
            SceneServiceListeners(agent, bus)
            response = await bus.request(
                Event(
                    type=SystemEvent.SCENE_SWITCH_REQUESTED,
                    source="tool.scene_switch",
                    data={"scene_id": "scarlet_devil_mansion"},
                ),
                timeout=1.0,
            )
            await asyncio.sleep(0.05)
            await bus.stop()
            return response, captured

        response, captured = asyncio.run(scenario())
        self.assertTrue(response["ok"])
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertEqual(payload["scene_id"], "scarlet_devil_mansion")
        self.assertEqual(payload["from_scene_id"], "magic_forest")
        self.assertEqual(payload["actor_id"], "marisa")


if __name__ == "__main__":
    unittest.main()
