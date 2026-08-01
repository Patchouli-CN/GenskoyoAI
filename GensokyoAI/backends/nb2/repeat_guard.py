"""复读烦躁模型：同一用户连续复读/刷屏时，角色逐渐厌烦直至暂时不理。

判定完全在适配器侧按发送者进行（发送者身份只存在于接入层）：
- 与近期消息窗口内任意一条相似（归一化后相同或相似度达标）即判复读，连击 +1；
- 连击达到 warn_streak：调用方注入厌烦情绪上下文，角色回复自然转冷淡；
- 连击达到 mute_streak：调用方让角色说最后一句话表态，随后进入「不理」冷却；
- 冷却期内继续复读 → 静默丢弃（零 token）；内容有新意 → 交给调用方，
  由 LLM 以角色性格裁决：消气原谅（forgive）/ 破例回一句（respond）/ 继续不理；
- 冷却结束自动消气、从零计数。

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
    MUTED = auto()  # 冷却中且内容仍是复读：静默丢弃，不进 Runtime（零 token）
    MUTED_NOVEL = auto()  # 冷却中但内容有新意：交给调用方做破例判定（LLM 以性格裁决）


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
        llm_break: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.similarity = similarity
        self.history_size = history_size
        self.warn_streak = warn_streak
        self.mute_streak = mute_streak
        self.mute_seconds = mute_minutes * 60.0
        # 冷却期间遇到「有新意」的内容时，是否交给 LLM 以角色性格做破例判定
        # （False = 一律静默到冷却结束，零额外 token）
        self.llm_break = llm_break
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
            llm_break=config.llm_break,
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
        """判定一条新消息；冷却期内：复读白丢（零 token），新内容交调用方破例判定。"""
        now = self._clock()
        key = (conversation_key, user_id)
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _UserState(recent=deque(maxlen=self.history_size))

        if state.muted_until > now:
            remaining = state.muted_until - now
            normalized = self._normalize(text)
            if not normalized:
                return RepeatCheck(RepeatVerdict.MUTED, remaining_seconds=remaining)
            # 冷却期保留判重窗口：继续复读 → 白丢（算法拦截，不烦 LLM）；
            # 有新意 → 交给调用方（LLM 以角色性格裁决要不要破例理一下）
            is_repeat = self._is_repeat(state, normalized)
            state.recent.append(normalized)
            verdict = RepeatVerdict.MUTED if is_repeat else RepeatVerdict.MUTED_NOVEL
            return RepeatCheck(verdict, remaining_seconds=remaining)

        if state.muted_until > 0:
            # 冷却刚结束：消气清零，重新计数（判重窗口一并清空）
            state.muted_until = 0.0
            state.streak = 0
            state.recent.clear()

        normalized = self._normalize(text)
        if not normalized:
            return RepeatCheck(RepeatVerdict.OK)  # 纯表情/标点：不计数也不打断连击

        is_repeat = self._is_repeat(state, normalized)
        state.recent.append(normalized)
        state.streak = state.streak + 1 if is_repeat else 0

        if state.streak >= self.mute_streak:
            streak = state.streak
            # 进入冷却：连击清零但保留判重窗口（冷却期内识别继续刷屏用）
            state.streak = 0
            state.muted_until = now + self.mute_seconds
            return RepeatCheck(RepeatVerdict.FAREWELL, streak=streak)
        if state.streak >= self.warn_streak:
            return RepeatCheck(RepeatVerdict.ANNOYED, streak=state.streak)
        return RepeatCheck(RepeatVerdict.OK, streak=state.streak)

    def forgive(self, conversation_key: str, user_id: int) -> None:
        """提前解除「不理」冷却并清零（LLM 判定消气后调用）。"""
        state = self._states.get((conversation_key, user_id))
        if state is not None:
            state.muted_until = 0.0
            state.streak = 0
            state.recent.clear()

    def stats(self) -> dict[str, int]:
        """防护状态快照（/status 用）：冷却中人数 / 连击观察中人数 / 追踪总数。"""
        now = self._clock()
        muted = sum(1 for state in self._states.values() if state.muted_until > now)
        watching = sum(
            1
            for state in self._states.values()
            if state.muted_until <= now and state.streak >= self.warn_streak
        )
        return {"muted": muted, "watching": watching, "tracked": len(self._states)}
