"""OOC（脱角色/模板化）判定与重写 — 回复投递前自查。

挂在 Replyer 里：投递前用一次轻量 LLM 调用给候选回复打分（人设契合 + 自然度 +
是否照抄内心独白），OOC 分数过高则按 issues 重写。判定/重写失败一律放行原回复
（attention.py 同款哲学：增强绝不能拖垮主回复）。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from msgspec import Struct, field

from ...utils.logger import logger
from ..config_schema import CharacterConfig, OocJudgeConfig
from .prompts import build_ooc_judge_prompts, build_ooc_rewrite_prompt
from .types import DECISION_MIN_MAX_TOKENS, ProviderCapability

if TYPE_CHECKING:
    from .model_client import ModelClient

_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)

# 人设截断：控制判定 prompt 体积（完整人设可能在几百字到几千字）
_PERSONA_MAX_CHARS = 1200

# 判定重试提示（复用对话欲的追加纠错模式）
_RETRY_HINT = (
    "你上一条回复不是合法的 JSON。请严格按照要求只输出 JSON 对象，"
    "不要写成角色台词、对白或解释。请重试。"
)

# 结构化输出契约（mirror think_engine._SPEAKING_DRIVE_SCHEMA 范式）
_OOC_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ooc_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "character_match": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "naturalness": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "copied_inner_monologue": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "ooc_score",
        "character_match",
        "naturalness",
        "copied_inner_monologue",
        "issues",
    ],
    "additionalProperties": False,
}
_OOC_VERDICT_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "ooc_verdict", "strict": True, "schema": _OOC_VERDICT_SCHEMA},
}


class OocVerdict(Struct):
    """一次 OOC 判定结果（纯数据；是否重写由 Replyer 按阈值判定）。"""

    ooc_score: float = 0.0  # 0~1，越高越脱角色/模板化
    character_match: float = 0.0  # 0~1，人设契合度
    naturalness: float = 0.0  # 0~1，自然度（模板化/过度结构化压低）
    copied_inner_monologue: bool = False  # 是否照抄内部思考/待表达摘要
    issues: list[str] = field(default_factory=list)  # ≤3 条，供重写参考


class OocContext(Struct):
    """判定/重写所需上下文（挂载点组装；SPEAK 路径 pending_summary/thought 为空）。"""

    context_text: str = ""  # 近期对话（已格式化）
    pending_summary: str = ""  # 主动发言的意图摘要
    thought: str = ""  # 说话前思考
    emotion_line: str = ""  # emotion_state.context_line()，空 = 平稳
    # 本轮回应的发言者名单（群聊【昵称】标记提取）：多人合并批时判定/重写
    # 需要知道「分头回应多人」是场景需要而非模板化，且不得丢掉任何人的回应
    reply_targets: list[str] = field(default_factory=list)
    # 近期 assistant 回复（防自我复读的预检对照）：相似情境下模型会逐字
    # 借用自己刚说过的话（「旧文 + --- + 新答」是典型形态）
    recent_assistant: list[str] = field(default_factory=list)


def _clamp01(value: Any, *, default: float) -> float:
    """钳制到 [0, 1]；非数值回落 default。"""
    try:
        number = float(value)
    except TypeError, ValueError:
        return default
    return max(0.0, min(1.0, number))


class OocJudge:
    """轻量 LLM 判定器：judge 打分 + rewrite 重写。"""

    def __init__(
        self,
        *,
        model_client: ModelClient,
        config: OocJudgeConfig,
        character_name: str,
        log_label: str | None = None,
    ) -> None:
        self._model_client = model_client
        self.config = config
        self.character_name = character_name
        self._log_suffix = f" (租户: {log_label})" if log_label else ""

    async def judge(
        self,
        candidate: str,
        character: CharacterConfig,
        context: OocContext,
    ) -> OocVerdict | None:
        """一次轻量判定（温度 0.1 / ~200 token / call_context="ooc_judge"）。

        结构化输出 + 最多 1 次重试；失败返回 None（调用方放行原回复）。
        """
        if not candidate or not candidate.strip():
            return None
        system_prompt, user_prompt = build_ooc_judge_prompts(
            self.character_name,
            (character.system_prompt or "")[:_PERSONA_MAX_CHARS],
            candidate,
            context.context_text,
            context.pending_summary,
            context.thought,
            context.emotion_line,
            context.reply_targets,
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                max_tok = max(self.config.judge_max_tokens, DECISION_MIN_MAX_TOKENS)
                options: dict[str, Any] = {
                    "temperature": self.config.judge_temperature,
                    "num_predict": max_tok,
                    "max_tokens": max_tok,
                }
                if self._supports_structured_output():
                    options["response_format"] = _OOC_VERDICT_RESPONSE_FORMAT
                response = await self._model_client.chat(
                    messages=messages,
                    options=options,
                    call_context="ooc_judge",
                )
                content = response.message.content
                text = content.strip() if isinstance(content, str) else ""
                verdict = self._parse_ooc_verdict(text)
                if verdict is not None:
                    return verdict
                if attempt < max_retries:
                    logger.warning(f"[OOC] 判定未返回合法 JSON，重试一次{self._log_suffix}")
                    messages.append({"role": "assistant", "content": text[:1000]})
                    messages.append({"role": "user", "content": _RETRY_HINT})
                    continue
                return None
            except Exception as error:
                logger.warning(f"[OOC] 判定调用失败: {error}{self._log_suffix}")
                return None

    async def rewrite(
        self,
        candidate: str,
        character: CharacterConfig,
        context: OocContext,
        verdict: OocVerdict,
    ) -> str:
        """按 verdict.issues 重写；失败/空串返回 ""（调用方放行原回复）。"""
        if not candidate or not candidate.strip():
            return ""
        prompt = build_ooc_rewrite_prompt(
            self.character_name,
            (character.system_prompt or "")[:_PERSONA_MAX_CHARS],
            candidate,
            verdict.issues,
            context.emotion_line,
            context.context_text,
            context.reply_targets,
        )
        try:
            max_tok = max(self.config.rewrite_max_tokens, DECISION_MIN_MAX_TOKENS)
            response = await self._model_client.chat(
                messages=[{"role": "system", "content": prompt}],
                options={
                    "temperature": self.config.rewrite_temperature,
                    "num_predict": max_tok,
                    "max_tokens": max_tok,
                },
                call_context="ooc_rewrite",
            )
            content = response.message.content
            return content.strip() if isinstance(content, str) else ""
        except Exception as error:
            logger.warning(f"[OOC] 重写调用失败: {error}{self._log_suffix}")
            return ""

    def _parse_ooc_verdict(self, text: str) -> OocVerdict | None:
        """解析判定 JSON；ooc_score 缺失默认 1.0（宁重写不放过），其余默认 0.0。"""
        if not text:
            return None
        match = _JSON_OBJECT_PATTERN.search(text)
        if match is None:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError, TypeError:
            return None
        if not isinstance(data, dict):
            return None
        raw_issues = data.get("issues") or []
        issues = [str(item) for item in raw_issues if isinstance(item, str)][:3]
        return OocVerdict(
            ooc_score=_clamp01(data.get("ooc_score"), default=1.0),
            character_match=_clamp01(data.get("character_match"), default=0.0),
            naturalness=_clamp01(data.get("naturalness"), default=0.0),
            copied_inner_monologue=bool(data.get("copied_inner_monologue", False)),
            issues=issues,
        )

    def _supports_structured_output(self) -> bool:
        return self._model_client.supports(ProviderCapability.STRUCTURED_OUTPUT)
