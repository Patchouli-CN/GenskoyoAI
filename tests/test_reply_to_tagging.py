"""回复对象标记（§8.64）：触发消息带【昵称】前缀时，助手消息在工作记忆里
带 `@昵称` 前缀（与 QQ 端真实 @ 同款约定）——多人快节奏对话中帮模型归因
「我刚才那句话是回谁的」，防张冠李戴；投递给用户的 content 本体不受影响。
"""

import asyncio
import unittest
from types import SimpleNamespace

from GensokyoAI.core.event_listeners import CoreListeners
from GensokyoAI.core.events import Event, SystemEvent
from GensokyoAI.memory.working import WorkingMemoryManager
from GensokyoAI.utils.helpers import extract_speaker_tags


class _DummyEventBus:
    def subscribe(self, *args, **kwargs):
        pass

    def publish(self, *args, **kwargs):
        pass


class ExtractSpeakerTagsTests(unittest.TestCase):
    def test_single_speaker(self):
        self.assertEqual(extract_speaker_tags("【帕秋莉】现在几点"), ["帕秋莉"])

    def test_merged_batch_multiple_speakers(self):
        text = "【帕秋莉】啊？现在是中午\n【赤色杀人魔】随便你了"
        self.assertEqual(extract_speaker_tags(text), ["帕秋莉", "赤色杀人魔"])

    def test_dedup_same_speaker_lines(self):
        text = "【帕秋莉】第一句\n【帕秋莉】第二句"
        self.assertEqual(extract_speaker_tags(text), ["帕秋莉"])

    def test_no_tag_returns_empty(self):
        self.assertEqual(extract_speaker_tags("私聊纯文本"), [])
        self.assertEqual(extract_speaker_tags(""), [])

    def test_mid_line_tag_ignored(self):
        # 提醒正文里引用昵称不是行首标记，不算说话人
        self.assertEqual(extract_speaker_tags("该提醒 【帕秋莉】：喝水"), [])


class ReplyToMemoryTaggingTests(unittest.TestCase):
    def _run_listener(self, data: dict) -> WorkingMemoryManager:
        working_memory = WorkingMemoryManager()
        agent = SimpleNamespace(working_memory=working_memory)
        listener = CoreListeners(agent, _DummyEventBus())
        asyncio.run(
            listener.on_message_sent(
                Event(type=SystemEvent.MESSAGE_SENT, source="agent", data=data)
            )
        )
        return working_memory

    def test_reply_to_prefix_written_to_memory(self):
        wm = self._run_listener({"content": "到时间了哦～", "reply_to": ["帕秋莉", "赤色杀人魔"]})
        message = wm.get_context()[-1]
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], "@帕秋莉 @赤色杀人魔 到时间了哦～")

    def test_model_own_mention_not_duplicated(self):
        # 模型自己开头写了 @目标：良性模仿，不重复加标记
        wm = self._run_listener({"content": "@帕秋莉 到时间了哦～", "reply_to": ["帕秋莉"]})
        self.assertEqual(wm.get_context()[-1]["content"], "@帕秋莉 到时间了哦～")

    def test_no_reply_to_stores_plain(self):
        wm = self._run_listener({"content": "你好。"})
        self.assertEqual(wm.get_context()[-1]["content"], "你好。")


if __name__ == "__main__":
    unittest.main()
