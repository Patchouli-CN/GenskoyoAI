"""代际令牌（utils.generation.GenerationGuard）与 ThinkEngine 写回守卫测试。

借鉴 creature-chat ContextToken：会话切换翻代后，旧代际捕获的异步 LLM 结果
（长期思考/记忆蒸馏）禁止写回新会话记忆。
"""

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from GensokyoAI.core.agent.think_engine import ThinkEngine
from GensokyoAI.core.agent.types import UnifiedMessage, UnifiedResponse
from GensokyoAI.core.config import ThinkEngineConfig
from GensokyoAI.core.events import EventBus
from GensokyoAI.memory.topic_store import TopicAwareStore
from GensokyoAI.memory.types import Topic
from GensokyoAI.utils.generation import GenerationGuard


class GenerationGuardUnitTests(unittest.TestCase):
    def test_capture_and_bump_semantics(self):
        guard = GenerationGuard()
        token = guard.capture()
        self.assertTrue(guard.is_current(token))
        self.assertTrue(guard.if_current(token, "测试"))
        guard.bump()
        self.assertFalse(guard.is_current(token))
        self.assertFalse(guard.if_current(token, "测试"))
        # 新代际捕获的新令牌有效
        self.assertTrue(guard.is_current(guard.capture()))


class _FakeSemanticMemory:
    def __init__(self, store: TopicAwareStore):
        self.store = store
        self.config = None
        self.added: list[dict] = []

    async def add_async(self, content, importance, emotional_valence, topic_name=None):
        self.added.append({"content": content, "topic": topic_name})
        return True


class _FakeModelClient:
    def __init__(self, content: str, on_chat=None):
        self.content = content
        self.on_chat = on_chat

    async def chat(self, messages, options=None, call_context=None):
        if self.on_chat:
            self.on_chat()
        return UnifiedResponse(message=UnifiedMessage(role="assistant", content=self.content))


def _make_engine(semantic_memory, model_client, event_bus=None) -> ThinkEngine:
    return ThinkEngine(
        semantic_memory=semantic_memory,
        model_client=model_client,
        event_bus=event_bus or EventBus(enable_trace=False),
        character_name="test",
        config=ThinkEngineConfig(),
    )


class DistillGenerationGuardTests(unittest.TestCase):
    _DISTILL_JSON = '[{"content": "珍贵记忆A", "importance": 8, "emotional_valence": 0.5}]'
    _MESSAGES = [{"role": "user", "content": "今天去赏花"}]

    def test_distill_writes_when_current(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                memory = _FakeSemanticMemory(TopicAwareStore(Path(tmpdir) / "t.json"))
                engine = _make_engine(memory, _FakeModelClient(self._DISTILL_JSON))
                token = engine._generation_guard.capture()
                written = await engine.distill_memories(self._MESSAGES, token)
                self.assertEqual(written, 1)
                self.assertEqual(memory.added[0]["content"], "珍贵记忆A")

        asyncio.run(run())

    def test_distill_dropped_after_generation_bump(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                memory = _FakeSemanticMemory(TopicAwareStore(Path(tmpdir) / "t.json"))
                engine = _make_engine(memory, _FakeModelClient(self._DISTILL_JSON))
                token = engine._generation_guard.capture()
                # 会话切换：update_semantic_memory 翻代
                engine.update_semantic_memory(
                    _FakeSemanticMemory(TopicAwareStore(Path(tmpdir) / "t2.json"))
                )
                written = await engine.distill_memories(self._MESSAGES, token)
                self.assertEqual(written, 0)
                self.assertEqual(memory.added, [])

        asyncio.run(run())


class LongTermThinkGuardTests(unittest.TestCase):
    def test_thought_dropped_when_generation_flips_midflight(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                store = TopicAwareStore(Path(tmpdir) / "t.json")
                topic = Topic(name="樱花")
                store._topics[topic.id] = topic
                store._index_topic(topic)

                memory = _FakeSemanticMemory(store)
                bus = EventBus(enable_trace=False)
                engine = None

                def flip_midflight():
                    # 模拟 LLM 调用在途期间会话切换（翻代）
                    engine.update_semantic_memory(_FakeSemanticMemory(store))

                engine = _make_engine(
                    memory, _FakeModelClient("内心独白内容", on_chat=flip_midflight), bus
                )
                await engine._long_term_think()
                # 翻代后：话题不被标记思考过（写回被守卫丢弃）
                self.assertEqual(topic.thought_count, 0)

        asyncio.run(run())

    def test_thought_written_when_current(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                store = TopicAwareStore(Path(tmpdir) / "t.json")
                topic = Topic(name="樱花")
                store._topics[topic.id] = topic
                store._index_topic(topic)

                bus = EventBus(enable_trace=False)

                engine = _make_engine(
                    _FakeSemanticMemory(store), _FakeModelClient("内心独白内容"), bus
                )
                await engine._long_term_think()
                self.assertEqual(topic.thought_count, 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
