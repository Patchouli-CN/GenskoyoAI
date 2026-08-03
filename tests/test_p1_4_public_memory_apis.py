import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from GensokyoAI.core.config import MemoryConfig
from GensokyoAI.memory.semantic import SemanticMemoryManager
from GensokyoAI.memory.topic_store import TopicAwareStore
from GensokyoAI.memory.types import TopicMemoryType
from GensokyoAI.memory.working import WorkingMemoryManager


class WorkingMemoryRollbackTests(unittest.TestCase):
    def test_rollback_messages_removes_recent_messages_and_returns_count(self):
        memory = WorkingMemoryManager(max_turns=10)
        memory.add_message("user", "1")
        memory.add_message("assistant", "2")
        memory.add_message("user", "3")

        removed = memory.rollback_messages(2)

        self.assertEqual(removed, 2)
        self.assertEqual(memory.get_context(), [{"role": "user", "content": "1"}])

    def test_rollback_messages_ignores_non_positive_count(self):
        memory = WorkingMemoryManager(max_turns=10)
        memory.add_message("user", "1")

        self.assertEqual(memory.rollback_messages(0), 0)
        self.assertEqual(memory.rollback_messages(-1), 0)
        self.assertEqual(len(memory), 1)

    def test_rollback_turns_removes_two_messages_per_turn(self):
        memory = WorkingMemoryManager(max_turns=10)
        for index in range(5):
            role = "user" if index % 2 == 0 else "assistant"
            memory.add_message(role, str(index))

        removed = memory.rollback_turns(2)

        self.assertEqual(removed, 4)
        self.assertEqual(memory.get_context(), [{"role": "user", "content": "0"}])


class TopicAwareStorePublicApiTests(unittest.TestCase):
    def test_find_topic_by_name_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TopicAwareStore(Path(tmp) / "topics.json")

            topic = asyncio.run(store.add_async("灵梦喜欢喝茶", topic_name="Reimu"))

            self.assertIs(store.find_topic_by_name("reimu"), topic)
            self.assertIs(store.find_topic_by_name("REIMU"), topic)

    def test_update_topic_memory_appends_correction_memory_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topics.json"
            store = TopicAwareStore(path)
            topic = asyncio.run(store.add_async("旧设定", topic_name="设定"))
            self.assertIsNotNone(topic)
            assert topic is not None
            original_last_message_id = topic.message_ids[-1]

            updated = asyncio.run(store.update_topic_memory("设定", "新设定"))

            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.name, "设定")
            self.assertEqual(len(updated.message_ids), 2)
            appended_memory_id = updated.message_ids[-1]
            appended_memory = store._memories[appended_memory_id]
            self.assertEqual(appended_memory.content, "新设定")
            self.assertEqual(appended_memory.memory_type, TopicMemoryType.CORRECTION)
            self.assertEqual(appended_memory.supersedes, original_last_message_id)
            self.assertTrue(path.exists())

    def test_update_topic_memory_returns_none_for_missing_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TopicAwareStore(Path(tmp) / "topics.json")

            updated = asyncio.run(store.update_topic_memory("不存在", "内容"))

            self.assertIsNone(updated)

    def test_list_search_get_update_delete_and_graph_are_explainable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TopicAwareStore(Path(tmp) / "topics.json")
            topic = asyncio.run(store.add_async("灵梦喜欢喝热茶", topic_name="偏好"))
            self.assertIsNotNone(topic)
            assert topic is not None
            memory_id = topic.message_ids[-1]

            listed = store.list_memories(topic_name="偏好")
            searched = store.search(query_text="热茶", threshold=0.1, top_k=3)
            fetched = store.get_memory(memory_id)
            updated = asyncio.run(
                store.update_memory(memory_id, importance=0.9, tags=["偏好", "饮品"])
            )
            graph = store.get_topic_graph()
            deleted = asyncio.run(store.delete_memory(memory_id))

            self.assertEqual(listed["total"], 1)
            self.assertEqual(searched[0]["id"], memory_id)
            self.assertGreaterEqual(searched[0]["score"], 0.1)
            self.assertIn("matched_by", searched[0])
            self.assertIsNotNone(fetched)
            self.assertIsNotNone(updated)
            assert fetched is not None
            assert updated is not None
            self.assertEqual(fetched["topic_name"], "偏好")
            self.assertEqual(updated["importance"], 0.9)
            self.assertIn("recall_weight", graph["nodes"][0])
            self.assertTrue(deleted)
            self.assertIsNone(store.get_memory(memory_id))

    def test_delete_last_memory_cleans_related_topic_edges(self):
        """05#10/15：清空话题最后一篇记忆走 _remove_topic，其他话题指向它的边一并清除。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = TopicAwareStore(Path(tmp) / "topics.json")
            topic_a = asyncio.run(store.add_async("灵梦喜欢喝热茶", topic_name="偏好"))
            topic_b = asyncio.run(store.add_async("魔理沙喜欢借书", topic_name="魔法使"))
            self.assertIsNotNone(topic_a)
            self.assertIsNotNone(topic_b)
            assert topic_a is not None
            assert topic_b is not None
            # 模拟 LLM 打分路径（_update_edges）生成的关联边：B 指向 A
            store._topics[topic_b.id].related_topics[topic_a.id] = 1.0
            memory_id = topic_a.message_ids[-1]

            deleted = asyncio.run(store.delete_memory(memory_id))

            self.assertTrue(deleted)
            self.assertNotIn(topic_a.id, store._topics)  # 空话题已移除
            self.assertNotIn(topic_a.id, store._topics[topic_b.id].related_topics)  # 边已清

    def test_embedding_similarity_can_rank_memory_above_keyword_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TopicAwareStore(Path(tmp) / "topics.json")
            reimu = asyncio.run(store.add_async("博丽神社的赛钱箱", topic_name="神社"))
            marisa = asyncio.run(store.add_async("雾雨魔理沙的魔法书", topic_name="魔法"))
            assert reimu is not None
            assert marisa is not None
            reimu_memory_id = reimu.message_ids[-1]
            marisa_memory_id = marisa.message_ids[-1]

            results = store.search(
                query_text="完全无关键词",
                top_k=2,
                threshold=0.0,
                query_embedding=[1.0, 0.0],
                memory_embeddings={
                    reimu_memory_id: [0.1, 0.9],
                    marisa_memory_id: [1.0, 0.0],
                },
            )

            self.assertEqual(results[0]["id"], marisa_memory_id)
            self.assertIn("embedding", results[0]["matched_by"])
            self.assertGreater(results[0]["embedding_score"], results[1]["embedding_score"])


class SemanticMemoryManagerSearchTests(unittest.TestCase):
    def test_search_async_falls_back_when_embedding_fails(self):
        class FailingEmbeddingClient:
            supports_embeddings = True

            async def chat(self, *args, **kwargs):
                return SimpleNamespace(message=SimpleNamespace(content="{}"))

            async def embeddings(self, prompt):
                raise RuntimeError("embedding down")

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                manager = SemanticMemoryManager(
                    MemoryConfig(semantic_similarity_threshold=0.1),
                    "reimu",
                    Path(tmp),
                    cast(Any, FailingEmbeddingClient()),
                )
                await manager.add_async("灵梦喜欢喝茶", topic_name="偏好", importance=0.8)
                return await manager.search_async("喝茶", include_embedding=True)

        results = asyncio.run(run())

        self.assertEqual(results[0]["diagnostics"]["embedding_fallback"], True)
        self.assertEqual(results[0]["diagnostics"]["embedding_used"], False)
        self.assertIn("embedding down", results[0]["diagnostics"]["embedding_error"])


if __name__ == "__main__":
    unittest.main()
