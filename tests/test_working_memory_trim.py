"""工作记忆裁剪测试（memory/working.py）：max_turns 上限与孤儿 tool 消息清理"""

import unittest

from GensokyoAI.memory.working import WorkingMemoryManager


class WorkingMemoryTrimTests(unittest.TestCase):
    def test_add_message_trims_to_max_turns(self):
        manager = WorkingMemoryManager(max_turns=3)
        for index in range(10):
            manager.add_message("user", f"第 {index} 句")
        # 上限 = max_turns * 2 条消息，超出后只保留最近的部分
        self.assertEqual(len(manager), 6)
        context = manager.get_context()
        self.assertEqual(context[0]["content"], "第 4 句")
        self.assertEqual(context[-1]["content"], "第 9 句")

    def test_trim_drops_orphan_tool_messages_at_head(self):
        manager = WorkingMemoryManager(max_turns=2)
        manager.add_message("user", "查一下时间")
        manager.add_message(
            "assistant",
            "好的",
            tool_calls=[{"id": "call_1", "name": "get_current_time", "arguments": "{}"}],
        )
        manager.add_message("tool", "12:00", tool_call_id="call_1")
        manager.add_message("assistant", "现在十二点")
        manager.add_message("user", "再说一遍")
        # 再进一条触发裁剪：assistant(tool_calls) 被裁掉，tool 结果成孤儿
        manager.add_message("assistant", "中午十二点了")
        context = manager.get_context()
        self.assertNotEqual(context[0].get("role"), "tool")
        self.assertEqual(context[0]["content"], "现在十二点")
        self.assertEqual(len(context), 3)

    def test_no_trim_below_limit(self):
        manager = WorkingMemoryManager(max_turns=5)
        manager.add_message("user", "你好")
        manager.add_message("assistant", "你好呀")
        self.assertEqual(len(manager), 2)


if __name__ == "__main__":
    unittest.main()
