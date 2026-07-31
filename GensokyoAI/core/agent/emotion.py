"""情绪模块 - 八维情绪状态机（Ekman 基本情绪 + 爱/羞耻）。

古明地觉：情绪不是开关，是缓慢流动的水。

定位：**持续情绪状态**（mood state），与四维动机模型（`motivation_evaluator`，
回答「此刻想不想说」的瞬态决策）分层——本模块回答「这段时间心情怎么样」。

情绪从哪来：LLM 在对话欲评估（ThinkEngine.evaluate_speaking_drive）中顺带
自报八维情绪（零新增 LLM 调用）；本模块负责把一次次自评**混合**成连续状态：
- 每次自评按 alpha 指数混合（lerp），避免单轮情绪跳变；
- 随时间向基线（角色卡 emotion_baseline，默认全 0 平稳）指数衰减——
  再强烈的情绪也会慢慢平复，就像人一样。

情绪到哪去：
- 回复语气注入（`_publish_message_received` 追加 system_contexts）；
- 对话欲评估的输入（LLM 打分时知道自己当前心情）。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from msgspec import Struct

# 八维 → 中文标签（to_prompt_context / dominant 用）
EMOTION_LABELS: dict[str, str] = {
    "anger": "愤怒",
    "sorrow": "悲伤",
    "fear": "恐惧",
    "happy": "快乐",
    "love": "爱意",
    "surprised": "惊讶",
    "disgust": "厌恶",
    "shame": "羞耻",
}


class Emotion(Struct):
    """情绪表示类（各维度 0~1，0 为平静）。"""

    anger: float = 0.0  # 愤怒
    sorrow: float = 0.0  # 悲伤
    fear: float = 0.0  # 恐惧
    happy: float = 0.0  # 快乐
    love: float = 0.0  # 爱意
    surprised: float = 0.0  # 惊讶
    disgust: float = 0.0  # 厌恶
    shame: float = 0.0  # 羞耻

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, value))

    def clamped(self) -> Emotion:
        return Emotion(**{name: self._clamp01(getattr(self, name)) for name in EMOTION_LABELS})

    def lerp(self, other: Emotion, alpha: float) -> Emotion:
        """线性插值：self → other，alpha=1 时完全取 other。"""
        alpha = self._clamp01(alpha)
        return Emotion(
            **{
                name: getattr(self, name) + (getattr(other, name) - getattr(self, name)) * alpha
                for name in EMOTION_LABELS
            }
        )

    def dominant(self, threshold: float = 0.3, limit: int = 3) -> list[tuple[str, float]]:
        """显著情绪（≥ threshold）按强度降序，最多 limit 条。"""
        ranked = sorted(
            ((label, getattr(self, name)) for name, label in EMOTION_LABELS.items()),
            key=lambda item: item[1],
            reverse=True,
        )
        return [(label, value) for label, value in ranked[:limit] if value >= threshold]

    def to_prompt_context(self) -> str:
        dominant = self.dominant()
        if not dominant:
            return "（平稳，无显著情绪）"
        return " | ".join(f"{label} {value:.2f}" for label, value in dominant)


class EmotionState:
    """情绪状态机：基线 + 当前值，LLM 自评混合，随时间向基线衰减。

    线程模型：单事件循环内读写，无锁；clock 可注入便于测试。
    """

    def __init__(
        self,
        baseline: Emotion | None = None,
        *,
        alpha: float = 0.6,
        half_life_minutes: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.baseline = (baseline or Emotion()).clamped()
        self.current = self.baseline
        self.alpha = alpha  # 每次自评的混合比例（越大越容易变脸）
        self.half_life_seconds = half_life_minutes * 60.0
        self._clock = clock
        self._last_update = clock()

    def update(self, appraised: Emotion) -> Emotion:
        """吃进一次 LLM 自评：先按经过时间向基线衰减，再按 alpha 混合。"""
        now = self._clock()
        self._decay(now - self._last_update)
        self._last_update = now
        self.current = self.current.lerp(appraised.clamped(), self.alpha).clamped()
        return self.current

    def _decay(self, elapsed_seconds: float) -> None:
        """情绪随时间平复：current 向 baseline 指数衰减（半衰期可配）。"""
        if elapsed_seconds <= 0 or self.half_life_seconds <= 0:
            return
        factor = 0.5 ** (elapsed_seconds / self.half_life_seconds)
        self.current = self.baseline.lerp(self.current, factor)

    def context_line(self) -> str:
        """供提示词使用的一行情绪描述；全平稳时返回空串（不注入）。"""
        dominant = self.current.dominant()
        if not dominant:
            return ""
        return " | ".join(f"{label} {value:.2f}" for label, value in dominant)

    def reset(self) -> None:
        """会话切换时回到基线。"""
        self.current = self.baseline
        self._last_update = self._clock()
