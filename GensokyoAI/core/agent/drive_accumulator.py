"""对话欲累积器：纯算术的「想说话」驱动模型（§7.3）。

替代「每轮一次 LLM 决策」的旧主动调度：

- 事件增量为主：对话 momentum（每轮基础增量）、情感尖峰（话题图效价）、
  场景匹配；沉默时长只给低权重累积——否则退化成伪装的固定间隔定时器。
- 心情非对称衰减（享乐适应）：正面情绪半衰期短、负面情绪半衰期长但仍衰减。
- 说完话后表达欲部分泄压（vent）。
- 跨阈值前零 LLM 成本；状态随会话持久化（重启不重置人格）。

本类只做算术，不调用 LLM、不感知 Agent；编排（跨阈值→意图摘要→调度）
由 InitiativeCoordinator 负责。
"""

from __future__ import annotations

import time
from typing import Any

from ..config import InitiativeTimerConfig
from .motivation_evaluator import MotivationProfile

_DRIVE_CAP = 1.5  # 对话欲上限，防止无限累积
_MOOD_EMA_ALPHA = 0.3  # 心情对单轮效价的指数移动平均系数
_MOOD_EPSILON = 1e-3  # 心情衰减到该绝对值以下即归零


class DriveAccumulator:
    """对话欲（drive）与心情（mood）的纯算术累积状态。"""

    def __init__(self, config: InitiativeTimerConfig) -> None:
        self._config = config
        self._drive = 0.0
        self._mood = 0.0
        self._last_update = time.time()

    @property
    def drive(self) -> float:
        return self._drive

    @property
    def mood(self) -> float:
        return self._mood

    # ==================== 累积 ====================

    def record_turn(
        self,
        *,
        emotional_valence: float = 0.0,
        motivation: MotivationProfile | None = None,
        scene_match: bool = False,
    ) -> float:
        """一轮对话后累积对话欲与心情，返回当前 drive。

        增量以事件为主：每轮基础增量、四维动机（短期思考评估）、情感尖峰
        （话题图效价）、场景匹配；沉默时长在 `_apply_time_effects` 低权重累积。
        """
        self._apply_time_effects()
        config = self._config
        self._drive += config.drive_turn_increment
        # 四维动机回灌：表达欲/情感驱动/关系需求/情景相关的加权和
        if motivation is not None:
            self._drive += config.drive_motivation_boost * motivation.total_drive
        # 情感尖峰是主要增量来源（来自话题图效价，零 LLM）
        if abs(emotional_valence) >= 0.5:
            self._drive += config.drive_emotion_boost * abs(emotional_valence)
        if scene_match:
            self._drive += config.drive_scene_boost
        if emotional_valence:
            self._mood += (emotional_valence - self._mood) * _MOOD_EMA_ALPHA
        self._drive = min(self._drive, _DRIVE_CAP)
        self._mood = max(-1.0, min(1.0, self._mood))
        return self._drive

    def current_drive(self) -> float:
        """读取当前 drive（先惰性结算时间效应）。"""
        self._apply_time_effects()
        return self._drive

    def vent(self) -> None:
        """主动发言成功后泄压：表达欲按 vent_factor 保留。"""
        self._apply_time_effects()
        self._drive *= self._config.drive_vent_factor

    # ==================== 时间效应 ====================

    def _apply_time_effects(self) -> None:
        now = time.time()
        elapsed = max(0.0, now - self._last_update)
        self._last_update = now
        if elapsed <= 0:
            return
        minutes = elapsed / 60.0
        # 沉默低权重累积对话欲
        self._drive = min(
            _DRIVE_CAP, self._drive + self._config.drive_silence_rate_per_minute * minutes
        )
        # 心情非对称衰减：正快负慢（享乐适应）
        half_life = (
            self._config.mood_half_life_positive_minutes
            if self._mood > 0
            else self._config.mood_half_life_negative_minutes
        )
        if half_life > 0:
            self._mood *= 0.5 ** (minutes / half_life)
            if abs(self._mood) < _MOOD_EPSILON:
                self._mood = 0.0

    # ==================== 持久化 ====================

    def to_dict(self) -> dict[str, Any]:
        """序列化（存 session.metadata）。"""
        return {
            "drive": self._drive,
            "mood": self._mood,
            "last_update": self._last_update,
        }

    @classmethod
    def from_dict(
        cls, config: InitiativeTimerConfig, data: dict[str, Any] | None
    ) -> DriveAccumulator:
        """从 session.metadata 恢复；数据缺失/损坏时从全新状态开始。"""
        accumulator = cls(config)
        if not isinstance(data, dict):
            return accumulator
        try:
            accumulator._drive = max(0.0, min(_DRIVE_CAP, float(data.get("drive", 0.0))))
            accumulator._mood = max(-1.0, min(1.0, float(data.get("mood", 0.0))))
            accumulator._last_update = float(data.get("last_update", time.time()))
        except TypeError, ValueError:
            return cls(config)
        return accumulator
