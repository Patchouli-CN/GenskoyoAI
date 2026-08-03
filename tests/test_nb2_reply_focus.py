"""回应焦点（AttentionThings reply_focus 种类 + nb2 @ 接线）定向测试

因果判定全权归 LLM：种类只管 prompt/解析；本文件覆盖判定解析（focus
名单/空/坏输出/截断）、文本 @ 去重剥除、焦点名单 → QQ 解析。
plugin 经 nonebot.init 导入（与 test_nb2_reminders 同款前置）。
"""

import unittest

import nonebot

nonebot.init(driver="~fastapi")  # plugin 导入期 get_driver() 需要驱动实例

from GensokyoAI.backends.nb2 import plugin  # noqa: E402
from GensokyoAI.backends.nb2.pending import PendingChat  # noqa: E402


def _item(name: str, qq: int | None) -> PendingChat:
    return PendingChat(text=f"【{name}】在吗", member_name=name, member_qq=qq)


class ReplyFocusParseTests(unittest.TestCase):
    def setUp(self):
        self.kind = plugin._ReplyFocusAttentionKind()

    def test_parse_focus_names(self):
        data = self.kind.parse('{"focus": ["帕秋莉", "赤色杀人魔"]}')
        self.assertEqual(data, {"focus": ["帕秋莉", "赤色杀人魔"]})

    def test_empty_focus_means_no_verdict(self):
        self.assertIsNone(self.kind.parse('{"focus": []}'))

    def test_bad_output_returns_none(self):
        self.assertIsNone(self.kind.parse("不是 JSON"))
        self.assertIsNone(self.kind.parse('{"focus": "帕秋莉"}'))
        self.assertIsNone(self.kind.parse("[1,2]"))

    def test_json_embedded_in_text(self):
        data = self.kind.parse('思考过程……\n{"focus": ["帕秋莉"]}\n以上')
        self.assertEqual(data, {"focus": ["帕秋莉"]})

    def test_truncates_to_two_names(self):
        data = self.kind.parse('{"focus": ["甲", "乙", "丙"]}')
        self.assertEqual(data, {"focus": ["甲", "乙"]})


class StripLeadingMentionsTests(unittest.TestCase):
    def test_strips_model_own_mention(self):
        self.assertEqual(
            plugin._strip_leading_mentions("@帕秋莉 好～好～", ["帕秋莉"]), "好～好～"
        )

    def test_strips_multiple_mentions(self):
        self.assertEqual(
            plugin._strip_leading_mentions("@甲 @乙 来了", ["甲", "乙"]), "来了"
        )

    def test_non_target_mention_kept(self):
        # 模型 @ 的是焦点外的人（有意内容），不动
        self.assertEqual(
            plugin._strip_leading_mentions("@丙 你看", ["甲"]), "@丙 你看"
        )

    def test_strip_to_empty_keeps_original(self):
        self.assertEqual(plugin._strip_leading_mentions("@甲", ["甲"]), "@甲")


class ResolveFocusTargetsTests(unittest.TestCase):
    def tearDown(self):
        plugin._member_names.clear()

    def test_batch_map_hit(self):
        batch = [_item("帕秋莉", 10001), _item("赤色杀人魔", 10002)]
        targets = plugin._resolve_focus_targets("group:123", batch, ["帕秋莉"])
        self.assertEqual(targets, [10001])

    def test_fallback_to_member_cache(self):
        plugin._member_names[(123, 10002)] = "赤色杀人魔"
        targets = plugin._resolve_focus_targets("group:123", [], ["赤色杀人魔"])
        self.assertEqual(targets, [10002])

    def test_unknown_name_skipped(self):
        self.assertEqual(plugin._resolve_focus_targets("group:123", [], ["不存在"]), [])


if __name__ == "__main__":
    unittest.main()
