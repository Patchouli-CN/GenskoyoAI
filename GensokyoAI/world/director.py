"""Director - 智能选角：决定下一轮谁开口，而不是轮流。

导演复用共享 `ModelClient.chat()` 与 ThinkEngine 的 JSON schema/解析降级模式，
每次决策是一次独立模型调用。它只看到公开信息（角色公开摘要、共享剧本、调度
计数），绝不读取任何 Actor 的私有 prompt 与记忆。

硬约束（宁可降级也不出错）：
- `switch` 目标必须在候选列表内、不是当前角色、不是用户，否则按配置降级；
- `continue` 要求当前角色仍在场，且未达 `max_same_actor_turns`；
- 达到 `max_auto_turns` 直接强制 `wait_user`（不调模型，省 token）；
- JSON 解析失败（重试一次后仍失败）、模型异常/超时、空候选一律 `wait_user`。
任何失败路径都收敛为合法决策返回，绝不抛出、绝不死循环。
"""

from __future__ import annotations

from typing import Any

from ..core.agent.model_client import ModelClient
from ..core.agent.prompts import build_director_decision_prompts
from ..core.agent.types import DECISION_MIN_MAX_TOKENS
from ..core.config import WorldDirectorConfig
from ..core.events import Event, EventBus, SystemEvent
from ..utils.logger import logger
from ._llm_json import clamp01_number, extract_json_object, supports_structured_output
from .types import (
    USER_OCCUPANT_ID,
    DirectorAction,
    DirectorContext,
    DirectorDecision,
    DirectorPhase,
)

_DIRECTOR_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                DirectorAction.CONTINUE.value,
                DirectorAction.SWITCH.value,
                DirectorAction.WAIT_USER.value,
            ],
        },
        "next_character": {"type": ["string", "null"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["action", "next_character", "reason", "confidence"],
    "additionalProperties": False,
}
_DIRECTOR_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "world_director_decision",
        "strict": True,
        "schema": _DIRECTOR_DECISION_SCHEMA,
    },
}

# 解析失败后给模型的一次自我修正提示（与 ThinkEngine 同一降级模式）
_RETRY_HINT = (
    "你上一条回复不是合法的 JSON。请严格按照要求只输出 JSON 对象，"
    "不要写成角色台词、对白或解释。请重试。"
)


class Director:
    """World 导演：基于剧情节奏、在场角色与戏剧时机决定每轮谁发言。"""

    def __init__(
        self,
        model_client: ModelClient,
        config: WorldDirectorConfig,
        event_bus: EventBus | None = None,
    ) -> None:
        self._model_client = model_client
        self._config = config
        self._event_bus = event_bus

    # ==================== 决策入口 ====================

    async def decide(self, context: DirectorContext) -> DirectorDecision:
        """做一次选角决策；任何失败路径都收敛为合法决策，绝不抛出/死循环。"""
        # 硬约束先行：这两类情况不需要也不应该调用模型（省 token）
        decision = self._forced_decision(context)
        if decision is not None:
            self._publish_decision(context, decision)
            return decision

        raw = await self._request_decision(context)
        if raw is None:
            decision = DirectorDecision(
                action=DirectorAction.WAIT_USER,
                reason="导演模型调用失败或 JSON 解析失败，交还用户",
                fallback_applied=True,
            )
        else:
            decision = self._validate(context, raw)

        self._publish_decision(context, decision)
        return decision

    def _forced_decision(self, context: DirectorContext) -> DirectorDecision | None:
        """硬熔断：空候选 / 自动轮数达到上限时直接给出决策，不调用模型。"""
        if not context.candidates:
            return DirectorDecision(
                action=DirectorAction.WAIT_USER,
                reason="当前场景无候选角色，交还用户",
                fallback_applied=True,
            )
        if context.auto_turn_count >= self._config.max_auto_turns:
            return DirectorDecision(
                action=DirectorAction.WAIT_USER,
                reason=(f"达到 max_auto_turns={self._config.max_auto_turns} 熔断，强制交还用户"),
                fallback_applied=True,
            )
        return None

    # ==================== 校验与降级 ====================

    def _validate(self, context: DirectorContext, data: dict[str, Any]) -> DirectorDecision:
        """把模型输出校验为合法决策；非法选择按配置降级。"""
        action = DirectorAction(str(data["action"]))  # action 合法性已在解析阶段校验
        reason = data.get("reason")
        confidence = self._parse_confidence(data.get("confidence"))

        if action is DirectorAction.WAIT_USER:
            return DirectorDecision(
                action=DirectorAction.WAIT_USER,
                reason=reason if isinstance(reason, str) else "",
                confidence=confidence,
            )

        if action is DirectorAction.CONTINUE:
            if self._can_continue(context):
                return DirectorDecision(
                    action=DirectorAction.CONTINUE,
                    reason=reason if isinstance(reason, str) else "",
                    confidence=confidence,
                )
            return self._apply_fallback(context, "当前角色无法继续发言")

        # SWITCH：目标必须在候选列表内、不是当前角色、不是用户
        target = data.get("next_character")
        candidate_ids = {brief.actor_id for brief in context.candidates}
        if (
            isinstance(target, str)
            and target in candidate_ids
            and target != context.current_actor_id
            and target != USER_OCCUPANT_ID
        ):
            return DirectorDecision(
                action=DirectorAction.SWITCH,
                next_actor_id=target,
                reason=reason if isinstance(reason, str) else "",
                confidence=confidence,
            )
        return self._apply_fallback(context, f"switch 目标 {target!r} 不在候选列表内或非法")

    def _can_continue(self, context: DirectorContext) -> bool:
        """判断当前角色是否可以合法地继续发言。"""
        if context.current_actor_id is None:
            return False
        if context.same_actor_turn_count >= self._config.max_same_actor_turns:
            return False
        candidate_ids = {brief.actor_id for brief in context.candidates}
        return context.current_actor_id in candidate_ids

    def _apply_fallback(self, context: DirectorContext, reject_reason: str) -> DirectorDecision:
        """按配置降级非法决策；fallback 为 continue 时同样必须合法，否则交还用户。"""
        if self._config.fallback_action == "continue" and self._can_continue(context):
            return DirectorDecision(
                action=DirectorAction.CONTINUE,
                reason=f"{reject_reason}，按配置降级为 continue",
                fallback_applied=True,
            )
        return DirectorDecision(
            action=DirectorAction.WAIT_USER,
            reason=f"{reject_reason}，降级为 wait_user",
            fallback_applied=True,
        )

    # ==================== 模型调用 ====================

    async def _request_decision(self, context: DirectorContext) -> dict[str, Any] | None:
        """调用模型获取决策 JSON；解析失败重试一次，仍失败或异常返回 None。"""
        messages = self._build_messages(context)
        # thinking 模型的内部思考会消耗 max_tokens 预算，抬下限防止正文被挤空
        max_tok = max(self._config.max_tokens, DECISION_MIN_MAX_TOKENS)
        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                options: dict[str, Any] = {
                    "temperature": self._config.temperature,
                    "num_predict": max_tok,
                    "max_tokens": max_tok,
                }
                if self._supports_structured_output():
                    options["response_format"] = _DIRECTOR_RESPONSE_FORMAT

                response = await self._model_client.chat(messages=messages, options=options)
                content = response.message.content
                text = content.strip() if isinstance(content, str) else ""
                logger.trace(f"[Director] 决策原始响应: {text!r}")
                data = self._parse_decision_json(text)
                if data is not None:
                    return data

                if attempt < max_retries:
                    logger.warning("[Director] 决策未返回合法 JSON，准备重试一次")
                    messages.append({"role": "assistant", "content": text[:1000]})
                    messages.append({"role": "user", "content": _RETRY_HINT})
                    continue
                return None
            except Exception as error:
                logger.error(f"[Director] 决策调用失败: {error}")
                return None

        return None

    def _build_messages(self, context: DirectorContext) -> list[dict[str, str]]:
        """组装导演决策 prompt：只含公开信息，且明确告知当前被禁止的动作。"""
        candidate_lines = [
            f"- {brief.actor_id}（{brief.display_name}）"
            + (f"：{brief.summary}" if brief.summary else "")
            for brief in context.candidates
        ]
        candidates_text = "\n".join(candidate_lines)
        current_name = self._display_name_of(context, context.current_actor_id)

        status_lines = [
            f"本段自动表演已进行 {context.auto_turn_count}/{self._config.max_auto_turns} 轮；"
            f"当前角色已连续发言 {context.same_actor_turn_count}/{self._config.max_same_actor_turns} 轮。"
        ]
        if (
            context.current_actor_id is not None
            and context.same_actor_turn_count >= self._config.max_same_actor_turns
        ):
            status_lines.append('当前角色已达连发上限，本轮不允许 "continue"。')
        if context.current_actor_id is None:
            status_lines.append('当前没有发言角色，本轮不允许 "continue"。')
        status_text = "\n".join(status_lines)

        phase_instruction = self._phase_instruction(context)

        system_prompt, user_prompt = build_director_decision_prompts(
            scene_id=context.scene_id,
            scene_description=context.scene_description,
            candidates_text=candidates_text,
            current_name=current_name,
            transcript_text=context.transcript_text,
            status_text=status_text,
            phase_instruction=phase_instruction,
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    @staticmethod
    def _phase_instruction(context: DirectorContext) -> str:
        """按触发时机给出本轮调度的具体任务说明。"""
        if context.phase is DirectorPhase.AFTER_USER:
            return (
                "用户刚发了言。请从候选角色中选出最合适的「首个回应者」（用 switch 指定）；"
                "如果用户的话不需要角色回应（如自言自语、告别），选 wait_user。"
            )
        if context.phase is DirectorPhase.INITIATIVE:
            intent = context.initiative_summary or "（无具体意图）"
            return (
                "场面已沉默一段时间，世界有一个待表达的意图。请基于当下在场角色决定由谁开口"
                "（switch）、当前角色继续（continue），或此刻不适合说话（wait_user，放弃本次"
                f"主动）。\n【待表达意图】{intent}"
            )
        return (
            "一位角色刚说完。判断剧情应该让 TA 继续（continue）、换另一位候选角色接话"
            "（switch），还是把话筒交还用户（wait_user）。"
        )

    @staticmethod
    def _display_name_of(context: DirectorContext, actor_id: str | None) -> str:
        """把 actor_id 翻译成显示名；无当前角色时给出明确说明。"""
        if actor_id is None:
            return "无（等待首个回应者）"
        for brief in context.candidates:
            if brief.actor_id == actor_id:
                return f"{brief.display_name}（{actor_id}）"
        return actor_id

    # ==================== 解析与事件 ====================

    @staticmethod
    def _parse_decision_json(text: str) -> dict[str, Any] | None:
        """提取并解析决策 JSON；action 不在枚举内同样视为解析失败。"""
        data = extract_json_object(text, log_prefix="[Director] 决策")
        if data is None:
            return None
        action = data.get("action")
        if not isinstance(action, str) or action not in {item.value for item in DirectorAction}:
            logger.error(f"[Director] 决策 action 非法: {action!r}")
            return None
        return data

    @staticmethod
    def _parse_confidence(raw: Any) -> float:
        """解析置信度并钳制到 [0, 1]；缺失或非法一律 0.0。"""
        return clamp01_number(raw)

    def _supports_structured_output(self) -> bool:
        return supports_structured_output(self._model_client, log_prefix="[Director]")

    def _publish_decision(self, context: DirectorContext, decision: DirectorDecision) -> None:
        """发布决策事件（debug 可见 reason）；事件发布失败不影响决策返回。"""
        logger.debug(
            f"[Director] 决策: phase={context.phase.value} action={decision.action.value} "
            f"next={decision.next_actor_id} fallback={decision.fallback_applied} "
            f"reason={decision.reason}"
        )
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(
                Event(
                    type=SystemEvent.WORLD_DIRECTOR_DECISION,
                    source="world.director",
                    data={
                        "phase": context.phase.value,
                        "scene_id": context.scene_id,
                        "action": decision.action.value,
                        "next_actor_id": decision.next_actor_id,
                        "reason": decision.reason,
                        "confidence": decision.confidence,
                        "fallback_applied": decision.fallback_applied,
                        "candidates": [brief.actor_id for brief in context.candidates],
                        "current_actor_id": context.current_actor_id,
                        "auto_turn_count": context.auto_turn_count,
                        "same_actor_turn_count": context.same_actor_turn_count,
                    },
                )
            )
        except Exception as error:
            logger.warning(f"[Director] 决策事件发布失败: {error}")
