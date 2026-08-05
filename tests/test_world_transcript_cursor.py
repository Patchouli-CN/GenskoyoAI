"""SharedTranscript 截断安全游标（M2 回归）：分片饱和截断后，
按绝对下标追踪的游标会永久停摆；按「追加总数」追踪则持续工作。
"""

import unittest

from GensokyoAI.world.transcript import SharedTranscript
from GensokyoAI.world.types import SpeakerKind, TranscriptEntry


def _entry(scene: str, content: str) -> TranscriptEntry:
    return TranscriptEntry(
        scene_id=scene,
        speaker_kind=SpeakerKind.CHARACTER,
        speaker_id="reimu",
        speaker_name="灵梦",
        content=content,
    )


class TranscriptCursorTests(unittest.TestCase):
    def test_new_entries_since_before_truncation(self):
        transcript = SharedTranscript(max_entries_per_scene=10)
        for i in range(5):
            transcript.append(_entry("s", f"msg{i}"))
        entries, cursor = transcript.new_entries_since("s", 0)
        self.assertEqual(len(entries), 5)
        self.assertEqual(cursor, 5)
        entries, cursor = transcript.new_entries_since("s", cursor)
        self.assertEqual(entries, [])

    def test_cursor_survives_truncation(self):
        """分片饱和截断后游标仍能拿到新记录（回归：按下标追踪永久停摆）。"""
        transcript = SharedTranscript(max_entries_per_scene=3)
        for i in range(5):
            transcript.append(_entry("s", f"msg{i}"))
        # 已消费到第 3 条（0,1,2），bucket=[2,3,4]，total=5
        entries, cursor = transcript.new_entries_since("s", 3)
        self.assertEqual([e.content for e in entries], ["msg3", "msg4"])
        self.assertEqual(cursor, 5)
        # 继续截断（bucket=[4,5]）后仍能拿到新记录
        transcript.append(_entry("s", "msg5"))
        entries, cursor = transcript.new_entries_since("s", cursor)
        self.assertEqual([e.content for e in entries], ["msg5"])
        self.assertEqual(cursor, 6)

    def test_trimmed_gap_replays_surviving_entries(self):
        """游标落在被截掉的区间时，回放幸存记录（不永久停摆）。"""
        transcript = SharedTranscript(max_entries_per_scene=2)
        for i in range(4):
            transcript.append(_entry("s", f"msg{i}"))
        # 游标 1 已被截掉（bucket=[2,3]，trimmed=2）：从幸存最早处回放
        entries, cursor = transcript.new_entries_since("s", 1)
        self.assertEqual([e.content for e in entries], ["msg2", "msg3"])
        self.assertEqual(cursor, 4)


if __name__ == "__main__":
    unittest.main()
