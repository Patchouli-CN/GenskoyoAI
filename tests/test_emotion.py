"""情绪模块测试：Emotion 八维方法 + EmotionState 混合/衰减/上下文行。"""

import unittest

from GensokyoAI.core.agent.emotion import Emotion, EmotionState


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class EmotionTests(unittest.TestCase):
    def test_lerp_blends_toward_other(self):
        a = Emotion(happy=0.0, anger=1.0)
        b = Emotion(happy=1.0, anger=0.0)
        mid = a.lerp(b, 0.5)
        self.assertAlmostEqual(mid.happy, 0.5)
        self.assertAlmostEqual(mid.anger, 0.5)
        # alpha 越界被钳制
        self.assertEqual(a.lerp(b, 2.0).happy, 1.0)

    def test_clamped_bounds_values(self):
        emotion = Emotion(happy=1.7, sorrow=-0.5)
        clamped = emotion.clamped()
        self.assertEqual(clamped.happy, 1.0)
        self.assertEqual(clamped.sorrow, 0.0)

    def test_dominant_threshold_sort_and_limit(self):
        emotion = Emotion(happy=0.8, anger=0.5, sorrow=0.4, fear=0.35, love=0.1)
        dominant = emotion.dominant()
        self.assertEqual([label for label, _ in dominant], ["快乐", "愤怒", "悲伤"])
        self.assertEqual(emotion.dominant(limit=2)[0][0], "快乐")
        # 全部低于阈值
        self.assertEqual(Emotion(happy=0.1).dominant(), [])

    def test_to_prompt_context(self):
        self.assertEqual(Emotion().to_prompt_context(), "（平稳，无显著情绪）")
        self.assertIn("快乐", Emotion(happy=0.6).to_prompt_context())


class EmotionStateTests(unittest.TestCase):
    def test_update_blends_appraisal(self):
        clock = FakeClock()
        state = EmotionState(alpha=0.5, clock=clock)
        state.update(Emotion(happy=1.0))
        self.assertAlmostEqual(state.current.happy, 0.5)
        self.assertIn("快乐", state.context_line())

    def test_decay_toward_baseline_over_time(self):
        clock = FakeClock()
        # alpha=0 隔离衰减路径（自评不参与混合）
        state = EmotionState(alpha=0.0, half_life_minutes=30.0, clock=clock)
        state.current = Emotion(anger=1.0)
        clock.advance(30 * 60)
        state.update(Emotion())  # 30 分钟：向基线衰减一半
        self.assertAlmostEqual(state.current.anger, 0.5, places=2)
        clock.advance(30 * 60)
        state.update(Emotion())  # 再 30 分钟：再减半
        self.assertAlmostEqual(state.current.anger, 0.25, places=2)

    def test_baseline_is_decay_target_and_reset_point(self):
        clock = FakeClock()
        state = EmotionState(Emotion(happy=0.4), alpha=1.0, clock=clock)
        state.update(Emotion(sorrow=1.0))
        self.assertAlmostEqual(state.current.sorrow, 1.0)
        state.reset()
        self.assertAlmostEqual(state.current.happy, 0.4)
        self.assertAlmostEqual(state.current.sorrow, 0.0)

    def test_context_line_empty_when_calm(self):
        state = EmotionState(clock=FakeClock())
        self.assertEqual(state.context_line(), "")


if __name__ == "__main__":
    unittest.main()
