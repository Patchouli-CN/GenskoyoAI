"""复读烦躁模型测试：RepeatGuard 状态机 + 全局 repeat_guard 配置链（不导入 nonebot）。"""

import unittest
from pathlib import Path

from GensokyoAI.backends.nb2.repeat_guard import RepeatGuard, RepeatVerdict
from GensokyoAI.core.config import ConfigLoader, RepeatGuardConfig
from GensokyoAI.core.config_validator import ConfigValidator


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_guard(clock, **overrides) -> RepeatGuard:
    kwargs = {
        "similarity": 0.75,
        "history_size": 5,
        "warn_streak": 3,
        "mute_streak": 5,
        "mute_minutes": 10,
        "clock": clock,
    }
    kwargs.update(overrides)
    return RepeatGuard(**kwargs)


class RepeatGuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.guard = make_guard(self.clock)

    def test_normal_messages_stay_ok(self):
        for text in ["你好", "今天天气不错", "在干嘛呢"]:
            self.assertIs(self.guard.check("group:1", 42, text).verdict, RepeatVerdict.OK)

    def test_alternating_repeat_builds_streak_to_annoyed(self):
        # 「转」「停」交替刷屏：各自命中自己历史，连击持续累积
        self.assertIs(self.guard.check("group:1", 42, "转").verdict, RepeatVerdict.OK)
        self.assertIs(self.guard.check("group:1", 42, "停").verdict, RepeatVerdict.OK)
        self.assertIs(self.guard.check("group:1", 42, "转").verdict, RepeatVerdict.OK)  # 1
        self.assertIs(self.guard.check("group:1", 42, "停").verdict, RepeatVerdict.OK)  # 2
        verdict = self.guard.check("group:1", 42, "转")  # 3 → 厌烦
        self.assertIs(verdict.verdict, RepeatVerdict.ANNOYED)
        self.assertEqual(verdict.streak, 3)

    def test_streak_reaches_farewell_then_muted_then_fresh(self):
        expected = [
            RepeatVerdict.OK,  # 首次，窗口为空
            RepeatVerdict.OK,  # 连击 1
            RepeatVerdict.OK,  # 连击 2
            RepeatVerdict.ANNOYED,  # 连击 3
            RepeatVerdict.ANNOYED,  # 连击 4
            RepeatVerdict.FAREWELL,  # 连击 5 → 最后一句话 + 进入冷却
        ]
        for want in expected:
            self.assertIs(self.guard.check("group:1", 42, "转").verdict, want)

        # 冷却中继续复读：算法白丢（零 token）
        muted = self.guard.check("group:1", 42, "转")
        self.assertIs(muted.verdict, RepeatVerdict.MUTED)
        self.assertGreater(muted.remaining_seconds, 0)
        # 冷却中出现新内容：交 LLM 破例判定
        self.assertIs(
            self.guard.check("group:1", 42, "对不起，我错了").verdict, RepeatVerdict.MUTED_NOVEL
        )

        # 冷却结束：消气，连击与判重窗口都已清零
        self.clock.advance(601)
        self.assertIs(self.guard.check("group:1", 42, "转").verdict, RepeatVerdict.OK)

    def test_forgive_releases_mute_early(self):
        for _ in range(6):
            self.guard.check("group:1", 42, "转")  # 进入冷却
        self.assertIs(self.guard.check("group:1", 42, "转").verdict, RepeatVerdict.MUTED)
        self.guard.forgive("group:1", 42)
        # 原谅后立即恢复正常（窗口与连击清零）
        self.assertIs(self.guard.check("group:1", 42, "转").verdict, RepeatVerdict.OK)
        # 对其他用户不影响
        self.assertIs(self.guard.check("group:1", 99, "转").verdict, RepeatVerdict.OK)

    def test_llm_break_flag_from_config(self):
        guard = RepeatGuard.from_config(RepeatGuardConfig(llm_break=False), clock=self.clock)
        self.assertFalse(guard.llm_break)
        self.assertTrue(RepeatGuard.from_config(RepeatGuardConfig(), clock=self.clock).llm_break)

    def test_normal_message_resets_streak(self):
        self.guard.check("group:1", 42, "转")
        self.guard.check("group:1", 42, "转")  # 连击 1
        self.guard.check("group:1", 42, "转")  # 连击 2
        self.assertIs(self.guard.check("group:1", 42, "说点正经的").verdict, RepeatVerdict.OK)
        verdict = self.guard.check("group:1", 42, "转")  # 窗口里仍有「转」，从 1 重新累计
        self.assertIs(verdict.verdict, RepeatVerdict.OK)
        self.assertEqual(verdict.streak, 1)

    def test_similar_message_counts_as_repeat(self):
        self.guard.check("group:1", 42, "今天去红魔馆玩了")
        verdict = self.guard.check("group:1", 42, "今天去红魔馆玩啦")  # 高相似 → 复读
        self.assertEqual(verdict.streak, 1)

    def test_users_and_conversations_are_isolated(self):
        for _ in range(4):
            self.guard.check("group:1", 42, "转")  # 42 在群 1 已到厌烦区
        self.assertIs(self.guard.check("group:1", 99, "转").verdict, RepeatVerdict.OK)
        self.assertIs(self.guard.check("group:2", 42, "转").verdict, RepeatVerdict.OK)

    def test_punctuation_and_emoji_neutral(self):
        self.assertIs(self.guard.check("group:1", 42, "😂😂").verdict, RepeatVerdict.OK)
        self.assertIs(self.guard.check("group:1", 42, "转！").verdict, RepeatVerdict.OK)
        verdict = self.guard.check("group:1", 42, "转？")  # 归一化后都是「转」
        self.assertEqual(verdict.streak, 1)

    def test_from_config(self):
        guard = RepeatGuard.from_config(
            RepeatGuardConfig(warn_streak=2, mute_streak=3, mute_minutes=5), clock=self.clock
        )
        self.assertIs(guard.check("group:1", 42, "转").verdict, RepeatVerdict.OK)
        self.assertIs(guard.check("group:1", 42, "转").verdict, RepeatVerdict.OK)  # 1
        self.assertIs(guard.check("group:1", 42, "转").verdict, RepeatVerdict.ANNOYED)  # 2
        self.assertIs(guard.check("group:1", 42, "转").verdict, RepeatVerdict.FAREWELL)  # 3
        self.assertIs(guard.check("group:1", 42, "转").verdict, RepeatVerdict.MUTED)

    def test_stats_snapshot(self):
        # 42 进入冷却，99 到厌烦区（连击 3，首条不计重），7 只是说过话
        for _ in range(6):
            self.guard.check("group:1", 42, "转")
        for _ in range(4):
            self.guard.check("group:1", 99, "停")
        self.guard.check("group:1", 7, "正常聊天")
        stats = self.guard.stats()
        self.assertEqual(stats["muted"], 1)
        self.assertEqual(stats["watching"], 1)
        self.assertEqual(stats["tracked"], 3)
        # 冷却结束后不再计入
        self.clock.advance(601)
        self.assertEqual(self.guard.stats()["muted"], 0)


class RepeatGuardConfigTests(unittest.TestCase):
    def test_defaults_present_without_section(self):
        config = ConfigLoader()._dict_to_config({})
        self.assertIsInstance(config.repeat_guard, RepeatGuardConfig)
        self.assertTrue(config.repeat_guard.enabled)
        self.assertEqual(config.repeat_guard.warn_streak, 3)
        self.assertEqual(config.repeat_guard.mute_streak, 5)

    def test_section_parses(self):
        config = ConfigLoader()._dict_to_config(
            {"repeat_guard": {"warn_streak": 2, "mute_minutes": 30, "similarity": 0.8}}
        )
        self.assertEqual(config.repeat_guard.warn_streak, 2)
        self.assertEqual(config.repeat_guard.mute_minutes, 30)
        self.assertEqual(config.repeat_guard.similarity, 0.8)

    def test_template_loads_repeat_guard_section(self):
        config = ConfigLoader().load(Path("tmp/template-conf.yaml"))
        self.assertTrue(config.repeat_guard.enabled)
        self.assertEqual(config.repeat_guard.warn_streak, 3)

    def test_validator_flags_out_of_range(self):
        diags = ConfigValidator().validate_config_dict({"repeat_guard": {"similarity": 1.5}})
        self.assertTrue(
            any(d.path == "repeat_guard.similarity" and d.severity == "error" for d in diags)
        )

    def test_validator_flags_warn_above_mute(self):
        diags = ConfigValidator().validate_config_dict(
            {"repeat_guard": {"warn_streak": 6, "mute_streak": 5}}
        )
        self.assertTrue(
            any(
                d.code == "config.range.cross_field" and d.path == "repeat_guard.mute_streak"
                for d in diags
            )
        )

    def test_validator_flags_unknown_field(self):
        diags = ConfigValidator().validate_config_dict({"repeat_guard": {"nope": 1}})
        self.assertTrue(
            any(d.code == "config.field.unknown" and d.path == "repeat_guard.nope" for d in diags)
        )

    def test_validator_accepts_defaults(self):
        diags = ConfigValidator().validate_config_dict({"repeat_guard": {}})
        self.assertFalse([d for d in diags if d.path.startswith("repeat_guard")])


if __name__ == "__main__":
    unittest.main()
