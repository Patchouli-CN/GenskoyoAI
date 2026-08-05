"""群黑话矿工（AttentionThings jargon 种类 + plugin 代办学习链路）定向测试

覆盖：种类判定解析（terms/空/坏输出/截断/剥【】）、缓冲+冷却触发、
代办写入租户语义记忆、已知词去重、私聊不学、后台任务不阻塞语义。
plugin 经 nonebot.init 导入（与 test_nb2_reminders 同款前置）。
"""

import dataclasses
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import nonebot

nonebot.init(driver="~fastapi")  # plugin 导入期 get_driver() 需要驱动实例

from GensokyoAI.backends.nb2 import plugin  # noqa: E402


class JargonParseTests(unittest.TestCase):
    def setUp(self):
        self.kind = plugin._JargonAttentionKind()

    def test_parse_terms(self):
        raw = '{"terms": [{"term": "结芬", "meaning": "结婚的口误梗", "example": "和我结芬"}]}'
        self.assertEqual(
            self.kind.parse(raw),
            {"terms": [{"term": "结芬", "meaning": "结婚的口误梗"}]},
        )

    def test_empty_terms_means_no_verdict(self):
        self.assertIsNone(self.kind.parse('{"terms": []}'))

    def test_bad_output_returns_none(self):
        self.assertIsNone(self.kind.parse("不是 JSON"))
        self.assertIsNone(self.kind.parse('{"terms": "结芬"}'))
        self.assertIsNone(self.kind.parse("[1,2]"))

    def test_strips_brackets_and_caps_at_three(self):
        raw = (
            '{"terms": ['
            '{"term": "【结芬】", "meaning": "口误梗"},'
            '{"term": "b", "meaning": "m2"},'
            '{"term": "c", "meaning": "m3"},'
            '{"term": "d", "meaning": "m4"}]}'
        )
        data = self.kind.parse(raw)
        self.assertEqual([t["term"] for t in data["terms"]], ["结芬", "b", "c"])

    def test_filters_invalid_items(self):
        raw = '{"terms": [{"term": "", "meaning": "空词"}, {"term": "ok", "meaning": ""}, {"term": "ok", "meaning": "好"}]}'
        data = self.kind.parse(raw)
        self.assertEqual(data, {"terms": [{"term": "ok", "meaning": "好"}]})


class _FakeAttention:
    def __init__(self, verdicts):
        self._verdicts = verdicts
        self.calls: list[tuple[str, set]] = []

    async def inspect(self, text, *, only=None):
        self.calls.append((text, only))
        return self._verdicts


class _FakeHost:
    def __init__(self):
        self.topic_names: list[str] = []
        self.added: list[dict] = []

    async def list_memory_topic_names(self, agent_id, session_id):
        return list(self.topic_names)

    async def add_memory(self, agent_id, session_id, content, *, topic_name=None, importance=0.0):
        self.added.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "content": content,
                "topic_name": topic_name,
                "importance": importance,
            }
        )
        return True


class MaybeLearnJargonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = dataclasses.replace(
            plugin._config,
            jargon_enabled=True,
            jargon_min_lines=2,
            jargon_cooldown_seconds=0.0,
            jargon_max_lines=50,
        )
        self._config_patch = patch.object(plugin, "_config", config)
        self._config_patch.start()
        plugin._jargon_buffers.clear()
        plugin._jargon_last_learn.clear()
        plugin._jargon_known.clear()
        self.store = SimpleNamespace(get=lambda key: {"session_id": "sess-1", "revision": 0})
        self._store_patch = patch.object(plugin, "_store", self.store)
        self._store_patch.start()

    def tearDown(self):
        self._config_patch.stop()
        self._store_patch.stop()
        plugin._jargon_buffers.clear()
        plugin._jargon_last_learn.clear()
        plugin._jargon_known.clear()

    def _wire(self, verdicts):
        attention = _FakeAttention(verdicts)
        host = _FakeHost()
        return (
            attention,
            host,
            (
                patch.object(plugin, "_attention", attention),
                patch.object(plugin, "_host", host),
            ),
        )

    async def test_buffer_triggers_learn_and_writes_memory(self):
        verdicts = [
            SimpleNamespace(
                kind="jargon", data={"terms": [{"term": "结芬", "meaning": "结婚口误梗"}]}
            )
        ]
        attention, host, patches = self._wire(verdicts)
        with patches[0], patches[1]:
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【甲】和我结芬吧")
            self.assertEqual(attention.calls, [])  # 未满 min_lines 不判定
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【乙】真的吗")
        self.assertEqual(len(attention.calls), 1)
        self.assertEqual(attention.calls[0][1], {"jargon"})
        self.assertEqual(len(host.added), 1)
        self.assertEqual(host.added[0]["topic_name"], "结芬")
        self.assertIn("可能待考证", host.added[0]["content"])
        self.assertEqual(host.added[0]["session_id"], "sess-1")

    async def test_known_terms_not_relearned(self):
        verdicts = [
            SimpleNamespace(kind="jargon", data={"terms": [{"term": "结芬", "meaning": "m"}]})
        ]
        attention, host, patches = self._wire(verdicts)
        host.topic_names.append("结芬")  # 已在知识库
        with patches[0], patches[1]:
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【甲】结芬")
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【乙】结芬")
        self.assertEqual(host.added, [])

    async def test_second_batch_remembers_learned_terms(self):
        verdicts = [
            SimpleNamespace(kind="jargon", data={"terms": [{"term": "结芬", "meaning": "m"}]})
        ]
        attention, host, patches = self._wire(verdicts)
        with patches[0], patches[1]:
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【甲】结芬")
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【乙】好")
            # 第二次触发（冷却 0）：同词不再重复写入
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【丙】结芬")
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【丁】结芬")
        self.assertEqual(len(host.added), 1)
        self.assertEqual(len(attention.calls), 2)

    async def test_private_chat_not_mined(self):
        attention, host, patches = self._wire([])
        with patches[0], patches[1]:
            await plugin._maybe_learn_jargon("qq-user-1", "user:1", "结芬\n结芬\n结芬")
        self.assertEqual(attention.calls, [])

    async def test_empty_verdict_writes_nothing(self):
        attention, host, patches = self._wire([])
        with patches[0], patches[1]:
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【甲】普通聊天")
            await plugin._maybe_learn_jargon("qq-group-1", "group:1", "【乙】没有黑话")
        self.assertEqual(len(attention.calls), 1)
        self.assertEqual(host.added, [])


if __name__ == "__main__":
    unittest.main()
