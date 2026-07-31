"""ThinkEngine 相关测试"""

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from GensokyoAI.core.agent.think_engine import ThinkEngine
from GensokyoAI.core.agent.types import UnifiedMessage, UnifiedResponse
from GensokyoAI.core.config import ThinkEngineConfig
from GensokyoAI.core.events import EventBus
from GensokyoAI.core.migrations import migrate_memory_store_payload
from GensokyoAI.memory.topic_store import TopicAwareStore
from GensokyoAI.memory.types import Topic
from GensokyoAI.utils.helpers import utc_now


class _FakeSemanticMemory:
    """只暴露 ThinkEngine 需要的 store 接口。"""

    def __init__(self, store: TopicAwareStore):
        self.store = store


class _FakeModelClient:
    def __init__(self, content: str = "一些静默思考内容", *, structured_output: bool = False):
        self.content = content
        self.structured_output = structured_output
        self.call_count = 0
        self.last_messages = None
        self.last_options = None

    def supports(self, capability: str) -> bool:
        return self.structured_output and capability == "structured_output"

    async def chat(self, messages, options=None):
        self.last_messages = messages
        self.last_options = options
        self.call_count += 1
        return UnifiedResponse(message=UnifiedMessage(role="assistant", content=self.content))


class TopicThoughtTrackingTests(unittest.TestCase):
    def test_mark_topic_thought_updates_fields(self):
        with TemporaryDirectory() as tmpdir:
            store = TopicAwareStore(Path(tmpdir) / "topics.json")
            topic = Topic(name="测试话题")
            store._topics[topic.id] = topic

            self.assertIsNone(topic.last_thought_at)
            self.assertEqual(topic.thought_count, 0)

            result = store.mark_topic_thought(topic.id)
            self.assertTrue(result)
            self.assertIsNotNone(topic.last_thought_at)
            self.assertEqual(topic.thought_count, 1)

            # 再次标记应累加计数并更新时间戳
            before = topic.last_thought_at
            result = store.mark_topic_thought(topic.id)
            self.assertTrue(result)
            self.assertEqual(topic.thought_count, 2)
            self.assertGreaterEqual(topic.last_thought_at, before)

    def test_mark_topic_thought_returns_false_for_missing_topic(self):
        with TemporaryDirectory() as tmpdir:
            store = TopicAwareStore(Path(tmpdir) / "topics.json")
            self.assertFalse(store.mark_topic_thought("not-exist"))


class ThinkEngineWalkTests(unittest.TestCase):
    def _make_engine(self, store: TopicAwareStore, **config_overrides):
        config = ThinkEngineConfig(**config_overrides)
        event_bus = EventBus(enable_trace=False)
        semantic_memory = _FakeSemanticMemory(store)
        model_client = _FakeModelClient()
        return (
            ThinkEngine(
                semantic_memory=semantic_memory,
                model_client=model_client,
                event_bus=event_bus,
                character_name="test",
                config=config,
            ),
            model_client,
            event_bus,
        )

    def test_random_walk_avoids_revisiting_topics_when_dedup_enabled(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                store = TopicAwareStore(Path(tmpdir) / "topics.json")
                topic_a = Topic(name="A")
                topic_b = Topic(name="B")
                topic_a.related_topics[topic_b.id] = 10.0
                topic_b.related_topics[topic_a.id] = 10.0
                store._topics[topic_a.id] = topic_a
                store._topics[topic_b.id] = topic_b
                store._topic_name_index["a"] = topic_a.id
                store._topic_name_index["b"] = topic_b.id
                store._index_topic(topic_a)
                store._index_topic(topic_b)

                engine, model_client, _ = self._make_engine(
                    store,
                    walk_visit_dedup=True,
                    random_walk_steps_min=5,
                    random_walk_steps_max=5,
                )
                await engine._long_term_think()

                # A 和 B 互相强关联，但去重后 walk 最多只能访问两个不同话题
                visited_names = []
                for topic in store._topics.values():
                    if topic.thought_count > 0:
                        visited_names.append(topic.name)
                self.assertIn("A", visited_names)
                self.assertIn("B", visited_names)
                self.assertEqual(model_client.call_count, 1)

        asyncio.run(run())

    def test_cooldown_reduces_recently_thought_topic_reselection(self):
        async def run():
            with TemporaryDirectory() as tmpdir:
                store = TopicAwareStore(Path(tmpdir) / "topics.json")
                hot_topic = Topic(name="hot", emotional_valence=1.0)
                warm_topic = Topic(name="warm", emotional_valence=0.6)
                store._topics[hot_topic.id] = hot_topic
                store._topics[warm_topic.id] = warm_topic
                store._topic_name_index["hot"] = hot_topic.id
                store._topic_name_index["warm"] = warm_topic.id
                store._index_topic(hot_topic)
                store._index_topic(warm_topic)

                engine, _, _ = self._make_engine(
                    store,
                    think_cooldown_minutes=10,
                    emotional_priority_probability=1.0,  # 总是从高情感话题里选
                    emotional_trigger_threshold=0.5,
                    random_walk_steps_min=0,
                    random_walk_steps_max=0,
                )

                # hot 被标记为刚刚思考过，warm 没有
                hot_topic.last_thought_at = utc_now()
                hot_topic.thought_count = 1

                hot_count = 0
                warm_count = 0
                total = 30
                for _ in range(total):
                    await engine._long_term_think()
                    if hot_topic.thought_count > 1:
                        hot_count += 1
                    elif warm_topic.thought_count > 0:
                        warm_count += 1

                    # 重置实验条件
                    hot_topic.last_thought_at = utc_now()
                    hot_topic.thought_count = 1
                    warm_topic.last_thought_at = None
                    warm_topic.thought_count = 0

                # hot 处于冷却期，应显著少于未冷却的 warm
                self.assertLess(hot_count, warm_count)

        asyncio.run(run())


class ThinkEngineDecisionTests(unittest.TestCase):
    def _make_engine(self, model_client: _FakeModelClient):
        config = ThinkEngineConfig()
        event_bus = EventBus(enable_trace=False)
        semantic_memory = _FakeSemanticMemory(_FakeTopicStore())
        return (
            ThinkEngine(
                semantic_memory=semantic_memory,
                model_client=model_client,
                event_bus=event_bus,
                character_name="博丽灵梦",
                config=config,
            ),
            event_bus,
        )

    def test_evaluate_speaking_drive_parses_valid_json(self):
        async def run():
            model_client = _FakeModelClient(
                '{"message": "赛钱箱该擦擦了", "delay_seconds": 120, "reason": "有想法", '
                '"enthusiasm": 0.8, "motivation": {"expression_drive": 0.9, '
                '"emotional_charge": 0.8, "relational_need": 0.7, "situational_relevance": 0.8}}'
            )
            engine, _ = self._make_engine(model_client)
            decision = await engine.evaluate_speaking_drive(
                "刚才的回复",
                [],
                min_delay_seconds=30,
                max_delay_seconds=1800,
            )
            self.assertIsNotNone(decision)
            assert decision is not None
            # total = 0.9*0.3 + 0.8*0.35 + 0.7*0.2 + 0.8*0.15 = 0.81 >= 0.6
            self.assertTrue(decision.want_speak)
            self.assertAlmostEqual(decision.total_drive, 0.81, places=2)
            self.assertEqual(decision.message, "赛钱箱该擦擦了")
            self.assertEqual(decision.delay_seconds, 120)
            self.assertEqual(decision.reason, "有想法")
            self.assertEqual(model_client.call_count, 1)

        asyncio.run(run())

    def test_evaluate_speaking_drive_below_threshold_is_silent(self):
        async def run():
            model_client = _FakeModelClient(
                '{"message": "没什么想说的", "delay_seconds": 300, "reason": "平淡", '
                '"enthusiasm": 0.2, "motivation": {"expression_drive": 0.1, '
                '"emotional_charge": 0.1, "relational_need": 0.2, "situational_relevance": 0.1}}'
            )
            engine, _ = self._make_engine(model_client)
            decision = await engine.evaluate_speaking_drive("刚才的回复", [])
            self.assertIsNotNone(decision)
            assert decision is not None
            # total = 0.1*0.3 + 0.1*0.35 + 0.2*0.2 + 0.1*0.15 = 0.12 < 0.6
            self.assertFalse(decision.want_speak)

        asyncio.run(run())

    def test_evaluate_speaking_drive_returns_none_on_invalid_json(self):
        async def run():
            model_client = _FakeModelClient("这不是 JSON")
            engine, _ = self._make_engine(model_client)
            decision = await engine.evaluate_speaking_drive("刚才的回复", [])
            self.assertIsNone(decision)

        asyncio.run(run())

    def test_evaluate_speaking_drive_uses_structured_output_when_supported(self):
        async def run():
            model_client = _FakeModelClient(
                '{"message": "结构化摘要", "delay_seconds": 60, "reason": "想补充", '
                '"enthusiasm": 0.5, "motivation": {"expression_drive": 0.9, '
                '"emotional_charge": 0.9, "relational_need": 0.9, "situational_relevance": 0.9}}',
                structured_output=True,
            )
            engine, _ = self._make_engine(model_client)
            decision = await engine.evaluate_speaking_drive("刚才的回复", [])
            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertTrue(decision.want_speak)
            self.assertIsNotNone(model_client.last_options)
            assert model_client.last_options is not None
            self.assertEqual(
                model_client.last_options.get("response_format", {}).get("type"), "json_schema"
            )

        asyncio.run(run())

    def test_evaluate_speaking_drive_prompt_contains_character_and_constraints(self):
        async def run():
            model_client = _FakeModelClient()
            engine, _ = self._make_engine(model_client)
            await engine.evaluate_speaking_drive(
                "「赛钱箱在那边，随意投一点吧。」",
                [{"role": "user", "content": "你好"}],
                min_delay_seconds=30,
                max_delay_seconds=1800,
            )
            self.assertIsNotNone(model_client.last_messages)
            assert model_client.last_messages is not None
            system_prompt = model_client.last_messages[0]["content"]
            user_prompt = model_client.last_messages[1]["content"]
            self.assertIn("你是 博丽灵梦", system_prompt)
            self.assertIn("内在状态评估", system_prompt)
            self.assertIn("只输出一个原始 JSON 对象", system_prompt)
            self.assertIn("「赛钱箱在那边，随意投一点吧。」", user_prompt)
            self.assertIn("User: 你好", user_prompt)

        asyncio.run(run())

    def test_pre_speak_thought_returns_model_content(self):
        async def run():
            model_client = _FakeModelClient("重新组织后的重点")
            engine, _ = self._make_engine(model_client)
            thought = await engine.pre_speak_thought("想补充一点", "User: 你好")
            self.assertEqual(thought, "重新组织后的重点")
            self.assertIsNotNone(model_client.last_messages)
            assert model_client.last_messages is not None
            self.assertIn("你想说的内容", model_client.last_messages[0]["content"])
            self.assertIn("想补充一点", model_client.last_messages[0]["content"])
            # 防回归：提示词不得再引入机制词汇（会漏进发给用户的话）
            self.assertNotIn("主动定时器到点", model_client.last_messages[0]["content"])

        asyncio.run(run())

    def test_pre_speak_thought_returns_empty_on_failure(self):
        async def run():
            class _FailingModelClient(_FakeModelClient):
                async def chat(self, messages, options=None):
                    raise RuntimeError("模型调用失败")

            engine, _ = self._make_engine(_FailingModelClient())
            thought = await engine.pre_speak_thought("想补充一点", "User: 你好")
            self.assertEqual(thought, "")

        asyncio.run(run())


class _FakeTopicStore:
    def __init__(self):
        self._topics = {}

    def get_all_topics(self):
        return list(self._topics.values())


class MemoryStoreMigrationTests(unittest.TestCase):
    def test_v1_to_v2_migration_adds_thought_fields_history(self):
        v1_data = {
            "schema_version": 1,
            "format": "gensokyoai.memory.topic_store",
            "created_by": "GensokyoAI",
            "topics": [
                {
                    "name": "旧话题",
                    "id": "abc123",
                    "summary": "",
                    "created_at": utc_now().isoformat(),
                    "last_updated": utc_now().isoformat(),
                    "last_accessed": utc_now().isoformat(),
                    "access_count": 0,
                    "message_count": 1,
                    "importance": 0.5,
                    "emotional_valence": 0.0,
                    "related_topics": {},
                    "message_ids": ["m1"],
                }
            ],
            "memories": [],
        }

        migrated, changed = migrate_memory_store_payload(v1_data)
        self.assertTrue(changed)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["format"], "gensokyoai.memory.topic_store")
        self.assertTrue(any(entry["from_version"] == 1 for entry in migrated["migration_history"]))

    def test_v2_data_unchanged(self):
        v2_data = {
            "schema_version": 2,
            "format": "gensokyoai.memory.topic_store",
            "created_by": "GensokyoAI",
            "migration_history": [],
            "topics": [],
            "memories": [],
        }
        migrated, changed = migrate_memory_store_payload(v2_data)
        self.assertFalse(changed)
        self.assertEqual(migrated["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
