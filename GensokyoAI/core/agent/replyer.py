"""Replyer — 回复投递前 OOC 自查编排层。

编排「廉价预检 → 判定 → 重写 → 重判（有界）」。任何失败一律放行原回复，
绝不抛异常、绝不拖垮主回复（对齐 attention.py 的「增强绝不能拖垮主回复」哲学）。
"""

from __future__ import annotations

import difflib

from ...utils.logger import logger
from ..config_schema import CharacterConfig, OocJudgeConfig
from .ooc_judge import OocContext, OocJudge, OocVerdict


def _normalize(text: str) -> str:
    """归一化：小写 + 只留字母数字汉字（照搬 nb2 repeat_guard 的判重归一化范式）。"""
    return "".join(ch for ch in text.lower() if ch.isalnum())


# 预检命中「照抄内部思考/意图摘要」时的合成 verdict：直接进重写
_PRECHECK_VERDICT = OocVerdict(
    ooc_score=1.0,
    character_match=0.0,
    naturalness=0.0,
    copied_inner_monologue=True,
    issues=["疑似照抄内部思考/意图摘要"],
)


class Replyer:
    """回复投递前 OOC 自查：廉价预检 → 判定 → 重写 → 重判（有界）。"""

    def __init__(self, judge: OocJudge, config: OocJudgeConfig) -> None:
        self._judge = judge
        self._config = config

    async def ensure_in_character(
        self,
        candidate: str,
        *,
        character: CharacterConfig,
        context: OocContext,
        source: str = "speak",  # "speak" | "initiative"，仅日志区分
    ) -> str:
        """判定/重写编排；任何失败静默放行原回复，绝不抛异常。"""
        if not self._config.enabled or not candidate or not candidate.strip():
            return candidate
        try:
            # ① 廉价预检：照抄内部思考/意图摘要 → 免 LLM 直接重写
            if self._precheck_is_copy(candidate, context):
                logger.info(f"[OOC] 预检疑似照抄内部内容（{source}），直接重写")
                rewritten = await self._judge.rewrite(
                    candidate, character, context, _PRECHECK_VERDICT
                )
                return rewritten or candidate

            # ② 有界 判定 → 重写 → 重判（每轮 1 判定 + 1 重写）
            current = candidate
            for _ in range(self._config.max_retries + 1):
                verdict = await self._judge.judge(current, character, context)
                if verdict is None:  # 判定失败：放行
                    logger.debug(f"[OOC] 判定失败，放行原回复（{source}）")
                    return current
                if not self._needs_rewrite(verdict):  # 通过：放行
                    logger.debug(
                        f"[OOC] 判定通过（{source}，OOC 分 {verdict.ooc_score:.2f} "
                        f"< 阈值 {self._config.threshold}）"
                    )
                    return current
                issues = "、".join(verdict.issues[:3]) or "无明细"
                logger.info(
                    f"[OOC] 判出戏（{source}，OOC 分 {verdict.ooc_score:.2f} "
                    f"≥ 阈值 {self._config.threshold}）：{issues}，重写"
                )
                rewritten = await self._judge.rewrite(current, character, context, verdict)
                if not rewritten or rewritten.strip() == current.strip():
                    logger.debug(f"[OOC] 重写失败或无变化，放行原回复（{source}）")
                    return current  # 重写失败/无变化：放行
                logger.trace(f"[OOC] 重写: {current[:40]!r} → {rewritten[:40]!r}")
                current = rewritten
            logger.info(f"[OOC] 重写重判轮数耗尽，接受最后结果（{source}）")
            return current  # 轮数耗尽：接受最后结果（有界）
        except Exception as error:
            logger.warning(f"[OOC] 回复管线异常，放行原回复: {error}")
            return candidate

    def _needs_rewrite(self, verdict: OocVerdict) -> bool:
        return verdict.ooc_score >= self._config.threshold or verdict.copied_inner_monologue

    def _precheck_is_copy(self, candidate: str, context: OocContext) -> bool:
        """候选回复与 pending_summary/thought 归一化后高度相似 → 判照抄（免 LLM）。"""
        refs = (context.pending_summary, context.thought)
        if not any(ref for ref in refs):
            return False
        norm_candidate = _normalize(candidate)
        if not norm_candidate:
            return False
        for ref in refs:
            if not ref:
                continue
            norm_ref = _normalize(ref)
            if norm_ref and (
                difflib.SequenceMatcher(None, norm_candidate, norm_ref).ratio()
                >= self._config.similarity_threshold
            ):
                return True
        return False
