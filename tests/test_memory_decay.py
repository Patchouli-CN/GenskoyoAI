"""话题热度淘汰测试（memory/decay.py，参考 Lumi_Nox decay 的读取时惰性衰减）"""

import asyncio
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from GensokyoAI.core.agent.think_engine import ThinkEngine
from GensokyoAI.core.agent.types import UnifiedMessage, UnifiedResponse
from GensokyoAI.core.config import ThinkEngineConfig
from GensokyoAI.core.events import EventBus
from GensokyoAI.memory.decay import filter_active_topics, is_pinned, topic_heat
from GensokyoAI.memory.topic_store import TopicAwareStore
from GensokyoAI.memory.types import Topic
from GensokyoAI.utils.helpers import utc_now


def _topic(name: str, *, age_hours: float = 0.0, importance: float = 0.0) -> Topic:
    stamp = utc_now() - timedelta(hours=age_hours)
    return Topic(
        name=name,
        importance=importance,
        last_updated=stamp,
        last_accessed=stamp,
    )


class TopicHeatTests(unittest.TestCase):
    def test_fresh_topic_has_full_heat(self):
        self.assertAlmostEqual(topic_heat(_topic("新鲜")), 1.0, places=3)

    def test_half_life_age_gives_half_heat(self):
        topic = _topic("刚好一个半衰期", age_hours=72.0)
        heat = topic_heat(topic, half_life_hours=72.0)
        self.assertAlmostEqual(heat, 0.5, places=2)

    def test_very_old_topic_falls_below_threshold(self):
        # 默认 72h 半衰期下，约 10 天（240h）未活跃 → 热度 < 0.1
        topic = _topic("陈年旧事", age_hours=240.0)
        self.assertLess(topic_heat(topic), 0.1)

    def test_non_positive_half_life_disables_decay(self):
        topic = _topic("不衰减", age_hours=10000.0)
        self.assertEqual(topic_heat(topic, half_life_hours=0), 1.0)

    def test_recent_access_revives_heat(self):
        # 内容很旧但刚被检索命中过（last_accessed 较新）→ 按较新者算热度
        topic = _topic("刚被提起", age_hours=240.0)
        topic.last_accessed = utc_now()
        self.assertAlmostEqual(topic_heat(topic), 1.0, places=3)


class IsPinnedTests(unittest.TestCase):
    def test_high_importance_topic_is_pinned(self):
        self.assertTrue(is_pinned(_topic("核心记忆", importance=8.0)))

    def test_low_importance_topic_is_not_pinned(self):
        self.assertFalse(is_pinned(_topic("路人话题", importance=7.9)))


class FilterActiveTopicsTests(unittest.TestCase):
    def test_cold_topics_hidden_pinned_kept_and_order_preserved(self):
        fresh = _topic("新鲜", age_hours=1.0)
        cold = _topic("冷却", age_hours=240.0)
        pinned = _topic("钉住", age_hours=240.0, importance=9.0)

        active = filter_active_topics([fresh, cold, pinned])

        self.assertEqual([t.name for t in active], ["新鲜", "钉住"])

    def test_revived_topic_returns_to_active(self):
        revived = _topic("复活", age_hours=240.0)
        self.assertEqual(filter_active_topics([revived]), [])

        revived.last_accessed = utc_now()  # 用户重新谈起 → 检索刷新时间戳
        self.assertEqual([t.name for t in filter_active_topics([revived])], ["复活"])


class _FakeSemanticMemory:
    """带 memory config 的语义记忆替身（供 ThinkEngine 读取淘汰配置）。"""

    def __init__(self, store: TopicAwareStore, **decay_config):
        self.store = store
        self.config = SimpleNamespace(**decay_config)


class _FakeModelClient:
    def __init__(self, content: str = "一些静默思考内容"):
        self.content = content
        self.call_count = 0

    async def chat(self, messages, options=None, call_context=None):
        self.call_count += 1
        return UnifiedResponse(message=UnifiedMessage(role="assistant", content=self.content))


class ThinkEngineDecayFilterTests(unittest.TestCase):
    def _make_engine(self, store: TopicAwareStore, **decay_config):
        model_client = _FakeModelClient()
        engine = ThinkEngine(
            semantic_memory=_FakeSemanticMemory(store, **decay_config),
            model_client=model_client,
            event_bus=EventBus(enable_trace=False),
            character_name="test",
            config=ThinkEngineConfig(random_walk_steps_min=0, random_walk_steps_max=0),
        )
        return engine, model_client

    def test_cold_topic_is_not_thought_about(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                store = TopicAwareStore(Path(tmpdir) / "topics.json")
                cold = _topic("陈年旧事", age_hours=240.0)
                store._topics[cold.id] = cold

                engine, model_client = self._make_engine(store, topic_decay_enabled=True)
                await engine._long_term_think()

                self.assertEqual(model_client.call_count, 0)
                self.assertEqual(cold.thought_count, 0)

        asyncio.run(run())

    def test_hot_topic_still_thought_about_when_others_cold(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                store = TopicAwareStore(Path(tmpdir) / "topics.json")
                hot = _topic("新鲜", age_hours=1.0)
                cold = _topic("冷却", age_hours=240.0)
                store._topics[hot.id] = hot
                store._topics[cold.id] = cold
                store._index_topic(hot)
                store._index_topic(cold)

                engine, model_client = self._make_engine(store, topic_decay_enabled=True)
                await engine._long_term_think()

                self.assertEqual(model_client.call_count, 1)
                self.assertEqual(hot.thought_count, 1)
                self.assertEqual(cold.thought_count, 0)

        asyncio.run(run())

    def test_decay_disabled_keeps_all_topics_eligible(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                store = TopicAwareStore(Path(tmpdir) / "topics.json")
                cold = _topic("陈年旧事", age_hours=240.0)
                store._topics[cold.id] = cold

                engine, model_client = self._make_engine(store, topic_decay_enabled=False)
                await engine._long_term_think()

                self.assertEqual(model_client.call_count, 1)
                self.assertEqual(cold.thought_count, 1)

        asyncio.run(run())

    def test_pinned_topic_survives_decay(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                store = TopicAwareStore(Path(tmpdir) / "topics.json")
                pinned = _topic("钉住", age_hours=240.0, importance=9.0)
                store._topics[pinned.id] = pinned

                engine, model_client = self._make_engine(store, topic_decay_enabled=True)
                await engine._long_term_think()

                self.assertEqual(model_client.call_count, 1)
                self.assertEqual(pinned.thought_count, 1)

        asyncio.run(run())


class TopicEvictionTests(unittest.TestCase):
    """写入侧淘汰：话题数达 max_topics 上限时，回忆权重最低的非 pin 话题被移除。"""

    def _add(self, store: TopicAwareStore, name: str, importance: float):
        return asyncio.run(
            store.add_async(content=f"{name}的内容", importance=importance, topic_name=name)
        )

    def test_cap_enforced_and_weakest_evicted(self):
        with TemporaryDirectory() as tmpdir:
            store = TopicAwareStore(Path(tmpdir) / "topics.json", max_topics=2)
            self._add(store, "重要", 0.9)
            self._add(store, "路人", 0.1)
            self._add(store, "新人", 0.5)

            self.assertEqual(store.topic_count, 2)
            self.assertIsNone(store.find_topic_by_name("路人"))
            self.assertIsNotNone(store.find_topic_by_name("重要"))
            self.assertIsNotNone(store.find_topic_by_name("新人"))

    def test_evicted_topic_memories_are_removed(self):
        with TemporaryDirectory() as tmpdir:
            store = TopicAwareStore(Path(tmpdir) / "topics.json", max_topics=2)
            self._add(store, "重要", 0.9)
            self._add(store, "路人", 0.1)
            self.assertEqual(store.memory_count, 2)
            victim_memory_id = store.list_memories(topic_name="路人")["items"][0]["id"]

            self._add(store, "新人", 0.5)

            self.assertEqual(store.memory_count, 2)  # 路人的记忆随话题一起移除
            self.assertIsNone(store.get_memory(victim_memory_id))

    def test_pinned_topic_survives_eviction(self):
        with TemporaryDirectory() as tmpdir:
            store = TopicAwareStore(Path(tmpdir) / "topics.json", max_topics=2, pin_importance=8.0)
            self._add(store, "核心", 9.0)
            self._add(store, "路人", 0.1)
            self._add(store, "新人", 0.5)

            self.assertIsNotNone(store.find_topic_by_name("核心"))
            self.assertIsNone(store.find_topic_by_name("路人"))


if __name__ == "__main__":
    unittest.main()
