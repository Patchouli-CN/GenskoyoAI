"""主动发言纯调度器：只负责「延迟到点 → 触发回调」，不含任何 LLM 与角色逻辑。

计划内容（要不要说、何时说、围绕什么说）由对话主循环（DialogueLoop）决定；
本调度器依赖一个 trigger_callback 执行到点触发，供单角色 Agent 与多角色
World 共用——编排层共享抽象，而非两份拷贝。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from ..utils.helpers import utc_now
from ..utils.logger import logger
from .dialogue_loop import InitiativePlan
from .events import Event, EventBus, SystemEvent

TriggerCallback = Callable[[InitiativePlan, str], Awaitable[Any]]


@dataclass
class _ScheduledPlan:
    """调度器内部状态：一个待触发的计划。"""

    timer_id: str
    generation: int
    plan: InitiativePlan
    created_at: Any
    due_at: Any


class InitiativeScheduler:
    """主动发言纯调度器（替换式：新计划总是取代旧计划）。"""

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        min_delay_seconds: int = 30,
        max_delay_seconds: int = 3600,
        trigger_callback: TriggerCallback | None = None,
        event_source: str = "initiative_scheduler",
    ) -> None:
        self._event_bus = event_bus
        self._min_delay = min_delay_seconds
        self._max_delay = max_delay_seconds
        self._trigger_callback = trigger_callback
        self._event_source = event_source
        self._state: _ScheduledPlan | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._generation = 0
        # 已触发未完成的 fire id：回调在锁外等待（如回合互斥）期间若计划
        # 被取消/取代，凭此校验失效，避免补一次过期触发
        self._active_fire_id: str | None = None

    # ==================== 调度 ====================

    async def schedule(self, plan: InitiativePlan) -> dict[str, Any] | None:
        """按计划调度触发；已有计划时被取代。`should_schedule=False` 等于取消。"""
        if not plan.should_schedule or not plan.summary.strip():
            await self.cancel(reason="no_schedule_or_empty_summary")
            return None
        delay = self._clamp_delay(plan.delay_seconds)
        async with self._lock:
            await self._discard_locked(reason="replaced_by_new_plan")
            self._generation += 1
            now = utc_now()
            state = _ScheduledPlan(
                timer_id=uuid4().hex[:8],
                generation=self._generation,
                plan=plan,
                created_at=now,
                due_at=now + timedelta(seconds=delay),
            )
            self._state = state
            self._task = asyncio.create_task(self._run_timer(state.timer_id, state.generation))
            self._publish(SystemEvent.INITIATIVE_TIMER_CREATED, state)
            logger.info(
                f"[InitiativeScheduler] 已调度主动计划 {state.timer_id}，"
                f"{delay}s 后触发: {plan.summary[:40]}"
            )
            return self._payload(state)

    async def cancel(self, *, reason: str = "cancelled") -> bool:
        """取消当前计划；无计划时返回 False。"""
        async with self._lock:
            return await self._discard_locked(reason=reason) is not None

    def current(self) -> dict[str, Any] | None:
        """当前计划的前端可见 payload；无计划返回 None。"""
        if self._state is None:
            return None
        return self._payload(self._state)

    def is_active_fire(self, fire_id: str) -> bool:
        """判断某次触发是否仍未被取代（供回调在锁外等待后做时效校验）。"""
        return self._active_fire_id is not None and self._active_fire_id == fire_id

    async def shutdown(self) -> None:
        """关闭并取消后台定时任务。"""
        async with self._lock:
            await self._discard_locked(reason="shutdown")

    # ==================== 内部 ====================

    async def _discard_locked(self, *, reason: str) -> dict[str, Any] | None:
        state = self._state
        if state is None:
            # fired-pending（计划已被 fire 清掉、回调在锁外等待执行）也要让
            # 在途 fire 失效——否则取消/重规划后，锁外等待的过期回调凭旧
            # fire id 仍会执行（M1：stale fire 竞态）
            self._active_fire_id = None
            return None
        self._generation += 1
        self._state = None
        self._cancel_task()
        self._active_fire_id = None
        self._publish(SystemEvent.INITIATIVE_TIMER_DISCARDED, state, reason=reason)
        logger.debug(f"[InitiativeScheduler] 计划 {state.timer_id} 被丢弃: {reason}")
        return self._payload(state)

    def _cancel_task(self) -> None:
        task = self._task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._task = None

    async def _run_timer(self, timer_id: str, generation: int) -> None:
        try:
            while True:
                fire_plan: InitiativePlan | None = None
                async with self._lock:
                    state = self._state
                    if not state or state.timer_id != timer_id or state.generation != generation:
                        return
                    remaining = (state.due_at - utc_now()).total_seconds()
                    if remaining <= 0:
                        # 锁内完成状态变更，回调在锁外执行
                        fire_plan = state.plan
                        self._state = None
                        self._cancel_task()
                        self._active_fire_id = timer_id
                        self._publish(SystemEvent.INITIATIVE_TIMER_TRIGGERED, state)
                        logger.info(f"[InitiativeScheduler] 计划 {timer_id} 到点触发")
                if fire_plan is not None:
                    try:
                        if self._trigger_callback is not None:
                            await self._trigger_callback(fire_plan, timer_id)
                    except Exception as error:
                        logger.error(f"[InitiativeScheduler] 触发回调异常: {error}")
                    finally:
                        if self._active_fire_id == timer_id:
                            self._active_fire_id = None
                    return
                await asyncio.sleep(min(remaining, 1.0))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(f"[InitiativeScheduler] 定时任务异常: {error}")

    def _clamp_delay(self, value: Any) -> int:
        try:
            seconds = int(value)
        except TypeError, ValueError:
            seconds = self._min_delay
        return max(self._min_delay, min(self._max_delay, seconds))

    def _payload(self, state: _ScheduledPlan) -> dict[str, Any]:
        remaining = max(0, int((state.due_at - utc_now()).total_seconds()))
        return {
            "timer_id": state.timer_id,
            "generation": state.generation,
            "created_at": state.created_at.isoformat(),
            "due_at": state.due_at.isoformat(),
            "delay_seconds": state.plan.delay_seconds,
            "remaining_seconds": remaining,
            "summary": state.plan.summary,
            "reason": state.plan.reason,
        }

    def _publish(self, event_type: SystemEvent, state: _ScheduledPlan, *, reason: str = "") -> None:
        if self._event_bus is None:
            return
        try:
            data = self._payload(state)
            if reason:
                data["reason_detail"] = reason
            self._event_bus.publish(Event(type=event_type, source=self._event_source, data=data))
        except Exception as error:
            logger.warning(f"[InitiativeScheduler] 事件发布失败: {error}")
