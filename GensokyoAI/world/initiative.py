"""World 对话主循环的主动计划与触发。

每次一段自动表演结束（wait_user）后，World 统一做一次主动规划——整个世界
只有一个主动定时器（`core/initiative_scheduler.py` 纯调度器）。到点后由
Director phase=initiative 基于触发**当下**的场景与在场角色决定谁开口：

定时器承担「世界何时再次推动剧情」，导演承担「那个时机谁最适合开口」，
不为「定时器到点」强行台词——无人适合说话时放弃本次主动。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ..core.agent.prompts import build_world_initiative_prompts
from ..core.agent.types import ProviderCapability
from ..core.dialogue_loop import InitiativePlan
from ..core.initiative_scheduler import InitiativeScheduler
from ..utils.logger import logger

if TYPE_CHECKING:
    from .world import GensokyoWorld

_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)

_WORLD_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_schedule": {"type": "boolean"},
        "delay_seconds": {"type": "integer"},
        "summary": {"type": "string"},
        "reason": {"type": "string"},
        "enthusiasm": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["should_schedule", "delay_seconds", "summary", "reason", "enthusiasm"],
    "additionalProperties": False,
}
_WORLD_PLAN_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "world_initiative_plan",
        "strict": True,
        "schema": _WORLD_PLAN_SCHEMA,
    },
}

# 主动触发时给演员的通用舞台提示（意图摘要由 World 注入导演与演员上下文）
_INITIATIVE_CUE = "（沉默持续了一段时间，现在轮到你主动开口。）"


class WorldInitiativeLoop:
    """World 对话主循环的主动规划器与触发器。"""

    def __init__(
        self,
        world: GensokyoWorld,
        *,
        temperature: float = 0.3,
        max_tokens: int = 320,
    ) -> None:
        self._world = world
        self._temperature = temperature
        self._max_tokens = max_tokens
        timer_config = world._config.initiative_timer
        self._scheduler = InitiativeScheduler(
            event_bus=world.event_bus,
            min_delay_seconds=timer_config.min_delay_seconds,
            max_delay_seconds=timer_config.max_delay_seconds,
            trigger_callback=self._on_trigger,
            event_source="world.initiative",
        )

    # ==================== 计划 ====================

    async def plan_after_segment(self) -> None:
        """一段自动表演结束后规划下一次世界主动；不安排则取消旧计划。"""
        if self._world._shutdown:
            return
        try:
            plan = await self._request_plan()
        except Exception as error:
            logger.warning(f"[WorldInitiative] 主动规划失败（本次放弃，不影响对话）: {error}")
            plan = None
        if plan is None or not plan.should_schedule or not plan.summary.strip():
            await self._scheduler.cancel(reason="no_plan")
            return
        await self._scheduler.schedule(plan)

    async def cancel(self, reason: str) -> None:
        """取消当前主动计划（用户发言、关机等）。"""
        await self._scheduler.cancel(reason=reason)

    async def shutdown(self) -> None:
        await self._scheduler.shutdown()

    def current_plan(self) -> dict[str, Any] | None:
        """当前计划 payload（快照/RPC 用）。"""
        return self._scheduler.current()

    # ==================== 触发 ====================

    async def _on_trigger(self, plan: InitiativePlan, fire_id: str) -> None:
        """定时器到点：获取回合锁后由导演从当下在场角色中选角开口。"""
        world = self._world
        async with world._turn_lock:
            # 等待锁期间若用户发言/段落刚结束并已重规划，本次触发已过期
            if not self._scheduler.is_active_fire(fire_id):
                logger.debug("[WorldInitiative] 触发已被新计划/用户输入取代，放弃")
                return
            if world._shutdown:
                return
            logger.info(f"[WorldInitiative] 世界主动触发: {plan.summary[:60]}")
            await world._run_initiative_turn(plan, cue=_INITIATIVE_CUE)

    # ==================== 规划调用 ====================

    async def _request_plan(self) -> InitiativePlan | None:
        """基于当前场景剧本与在场角色做一次世界级主动规划（一次模型调用）。"""
        world = self._world
        user_scene = world._stage.scene_of("__user__") or "world_default"
        candidates = [
            brief.display_name
            for aid in world._stage.characters_in(user_scene)
            if (brief := world._briefs.get(aid))
        ]
        transcript_text = world._transcript.render_for_scene(
            user_scene, limit=world._world_config.transcript.context_entries
        )

        system_prompt, user_prompt = build_world_initiative_prompts(
            user_scene=user_scene,
            candidates_text="、".join(candidates) if candidates else "（无）",
            transcript_text=transcript_text,
        )
        options: dict[str, Any] = {
            "temperature": self._temperature,
            "num_predict": self._max_tokens,
            "max_tokens": self._max_tokens,
        }
        if self._supports_structured_output():
            options["response_format"] = _WORLD_PLAN_RESPONSE_FORMAT
        response = await world._model_client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options=options,
        )
        content = response.message.content
        text = content.strip() if isinstance(content, str) else ""
        return self._parse_plan(text)

    def _parse_plan(self, text: str) -> InitiativePlan | None:
        match = _JSON_OBJECT_PATTERN.search(text)
        raw = match.group(0) if match else text
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            logger.warning(f"[WorldInitiative] 规划 JSON 解析失败: {error}")
            return None
        if not isinstance(data, dict):
            return None
        summary = data.get("summary")
        enthusiasm = data.get("enthusiasm")
        raw_delay = data.get("delay_seconds")
        return InitiativePlan(
            should_schedule=bool(data.get("should_schedule")),
            delay_seconds=raw_delay if isinstance(raw_delay, int) else 300,
            summary=summary.strip() if isinstance(summary, str) else "",
            reason=str(data.get("reason") or "").strip(),
            enthusiasm=max(0.0, min(1.0, float(enthusiasm)))
            if isinstance(enthusiasm, int | float) and not isinstance(enthusiasm, bool)
            else 0.5,
        )

    def _supports_structured_output(self) -> bool:
        supports = getattr(self._world._model_client, "supports", None)
        if callable(supports):
            try:
                return bool(supports(ProviderCapability.STRUCTURED_OUTPUT))
            except Exception as error:
                logger.warning(f"[WorldInitiative] 结构化输出能力判断失败: {error}")
        return False
