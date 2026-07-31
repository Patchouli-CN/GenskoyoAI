"""复读烦躁模型：同一用户连续复读/刷屏时，角色逐渐厌烦直至暂时不理。

判定完全在适配器侧按发送者进行（发送者身份只存在于接入层），纯内存、零 token：
- 与近期消息窗口内任意一条相似（归一化后相同或相似度达标）即判复读，连击 +1；
- 连击达到 warn_streak：调用方注入厌烦情绪上下文，角色回复自然转冷淡；
- 连击达到 mute_streak：调用方让角色说最后一句话表态，随后进入「不理」冷却；
- 冷却期间该用户消息被静默丢弃（不进 Runtime）；冷却结束自动消气、从零计数。

阈值来自全局配置 repeat_guard 节（config/local.yaml），见 RepeatGuardConfig。
"""

from __future__ import annotations

import difflib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.config_schema import RepeatGuardConfig


class RepeatVerdict(Enum):
    """单条消息的复读判定结果。"""

    OK = auto()  # 正常回应
    ANNOYED = auto()  # 厌烦区：注入厌烦上下文，角色回复转冷淡
    FAREWELL = auto()  # 最后一句话：本轮注入告别上下文，随后进入「不理」冷却
    MUTED = auto()  # 冷却中：静默丢弃，不进 Runtime


@dataclass(frozen=True)
class RepeatCheck:
    verdict: RepeatVerdict
    streak: int = 0  # 触发本次判定的连击数（ANNOYED/FAREWELL 时非零）
    remaining_seconds: float = 0.0  # MUTED 时的剩余冷却秒数


@dataclass
class _UserState:
    recent: deque[str]  # 近期归一化消息窗口（判重基准）
    streak: int = 0
    muted_until: float = 0.0


class RepeatGuard:
    """按 (会话, 用户) 追踪复读连击与「不理」冷却。"""

    def __init__(
        self,
        *,
        similarity: float = 0.75,
        history_size: int = 5,
        warn_streak: int = 3,
        mute_streak: int = 5,
        mute_minutes: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.similarity = similarity
        self.history_size = history_size
        self.warn_streak = warn_streak
        self.mute_streak = mute_streak
        self.mute_seconds = mute_minutes * 60.0
        self._clock = clock
        self._states: dict[tuple[str, int], _UserState] = {}

    @classmethod
    def from_config(cls, config: RepeatGuardConfig, **kwargs) -> RepeatGuard:
        return cls(
            similarity=config.similarity,
            history_size=config.history_size,
            warn_streak=config.warn_streak,
            mute_streak=config.mute_streak,
            mute_minutes=config.mute_minutes,
            **kwargs,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        """归一化：小写 + 只留字母数字汉字，标点/空白/emoji 不参与判重。"""
        return "".join(ch for ch in text.lower() if ch.isalnum())

    def _is_repeat(self, state: _UserState, normalized: str) -> bool:
        for past in state.recent:
            if normalized == past:
                return True
            if difflib.SequenceMatcher(None, normalized, past).ratio() >= self.similarity:
                return True
        return False

    def check(self, conversation_key: str, user_id: int, text: str) -> RepeatCheck:
        """判定一条新消息；MUTED 之外的结果都会推进该用户的判重窗口。"""
        now = self._clock()
        key = (conversation_key, user_id)
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _UserState(recent=deque(maxlen=self.history_size))

        if state.muted_until > now:
            return RepeatCheck(RepeatVerdict.MUTED, remaining_seconds=state.muted_until - now)

        normalized = self._normalize(text)
        if not normalized:
            return RepeatCheck(RepeatVerdict.OK)  # 纯表情/标点：不计数也不打断连击

        is_repeat = self._is_repeat(state, normalized)
        state.recent.append(normalized)
        state.streak = state.streak + 1 if is_repeat else 0

        if state.streak >= self.mute_streak:
            streak = state.streak
            # 进入冷却：清空连击与判重窗口，冷却结束后从零开始（消气了）
            state.streak = 0
            state.recent.clear()
            state.muted_until = now + self.mute_seconds
            return RepeatCheck(RepeatVerdict.FAREWELL, streak=streak)
        if state.streak >= self.warn_streak:
            return RepeatCheck(RepeatVerdict.ANNOYED, streak=state.streak)
        return RepeatCheck(RepeatVerdict.OK, streak=state.streak)
