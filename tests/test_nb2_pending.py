"""nb2 待发合并（pending.py）定向测试：

- add 的首个调用方成为处理者，处理期间入队只等待
- take_batch 按到达顺序取空
- finish 时若有收尾间隙残留则保留队列、释放处理权
- merge_batch 文本保序、上下文去重、幂等键取首条
"""

import unittest

from GensokyoAI.backends.nb2.config import Nb2Config
from GensokyoAI.backends.nb2.pending import PendingChat, PendingChatQueue, merge_batch


def _item(text: str, *, qq: int = 10001, mid: int = 1, contexts: list[str] | None = None):
    return PendingChat(
        text=text,
        contexts=contexts or [],
        member_name="魔理沙",
        member_qq=qq,
        self_id=99999,
        message_id=mid,
    )


class PendingChatQueueTests(unittest.TestCase):
    def test_first_caller_becomes_drainer_second_waits(self):
        queue = PendingChatQueue()
        self.assertTrue(queue.add("group:1", _item("第一条", mid=1)))
        self.assertFalse(queue.add("group:1", _item("第二条", mid=2)))
        self.assertEqual(queue.pending_count("group:1"), 2)

    def test_take_batch_drains_in_arrival_order(self):
        queue = PendingChatQueue()
        queue.add("group:1", _item("A", mid=1))
        queue.add("group:1", _item("B", mid=2))
        batch = queue.take_batch("group:1")
        self.assertEqual([item.text for item in batch], ["A", "B"])
        self.assertEqual(queue.take_batch("group:1"), [])

    def test_finish_keeps_gap_arrivals_and_releases_drainer(self):
        queue = PendingChatQueue()
        queue.add("group:1", _item("A", mid=1))
        queue.take_batch("group:1")
        # 处理循环收尾间隙又来了新消息（仍在 active 内 → 只入队）
        self.assertFalse(queue.add("group:1", _item("B", mid=2)))
        queue.finish("group:1")
        # 队列保留，处理权已释放：下一条消息的调用方重新成为处理者
        self.assertTrue(queue.add("group:1", _item("C", mid=3)))
        batch = queue.take_batch("group:1")
        self.assertEqual([item.text for item in batch], ["B", "C"])

    def test_finish_cleans_empty_queue(self):
        queue = PendingChatQueue()
        queue.add("group:1", _item("A", mid=1))
        queue.take_batch("group:1")
        queue.finish("group:1")
        self.assertEqual(queue.pending_count("group:1"), 0)


class MergeBatchTests(unittest.TestCase):
    def test_merge_joins_texts_and_dedups_contexts(self):
        batch = [
            _item("【灵梦】在吗", qq=10001, mid=11, contexts=["【QQ 聊天场景附加要求】", "印象A"]),
            _item(
                "【魔理沙】我也在", qq=10002, mid=12, contexts=["【QQ 聊天场景附加要求】", "印象B"]
            ),
        ]
        text, contexts, idem = merge_batch(batch)
        self.assertEqual(text, "【灵梦】在吗\n【魔理沙】我也在")
        self.assertEqual(contexts, ["【QQ 聊天场景附加要求】", "印象A", "印象B"])
        # 幂等键取批次首条消息
        self.assertEqual(idem, "nb2:99999:11")

    def test_single_message_batch_unchanged(self):
        text, contexts, idem = merge_batch([_item("你好", mid=7, contexts=["ctx"])])
        self.assertEqual(text, "你好")
        self.assertEqual(contexts, ["ctx"])
        self.assertEqual(idem, "nb2:99999:7")


class MergeWindowConfigTests(unittest.TestCase):
    def test_default_window(self):
        self.assertEqual(Nb2Config().merge_window_seconds, 1.5)

    def test_env_parse(self):
        env = {"GSK_NB2_MERGE_WINDOW_SECONDS": "3"}
        self.assertEqual(Nb2Config.from_env(env.get).merge_window_seconds, 3.0)

    def test_env_zero_disables_wait(self):
        env = {"GSK_NB2_MERGE_WINDOW_SECONDS": "0"}
        self.assertEqual(Nb2Config.from_env(env.get).merge_window_seconds, 0.0)

    def test_env_invalid_falls_back_to_default(self):
        env = {"GSK_NB2_MERGE_WINDOW_SECONDS": "abc"}
        self.assertEqual(Nb2Config.from_env(env.get).merge_window_seconds, 1.5)


if __name__ == "__main__":
    unittest.main()
