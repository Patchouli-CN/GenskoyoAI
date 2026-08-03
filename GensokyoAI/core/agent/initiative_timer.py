"""主动定时器 - 纯定时器调度，思考逻辑由 ThinkEngine 负责"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ...utils.helpers import utc_now
from ...utils.logger import logger
from ..config import InitiativeTimerConfig
from ..events import Event, EventBus, SystemEvent

if TYPE_CHECKING:
    from .think_engine import ThinkEngine


@dataclass
class InitiativeTimerState:
    timer_id: str
    status: str
    generation: int
    source: str
    created_at: datetime
    updated_at: datetime
    due_at: datetime
    delay_seconds: int
    pending_summary: str
    reason: str = ""
    user_modified: bool = False


class InitiativeTimerManager:
    """管理回答后由 AI 生成的积存主动发言摘要与定时器。

    职责：
    - 定时器调度（创建、取消、触发、管理）
    - 委托 ThinkEngine 进行所有思考决策
    - 不直接调用 LLM
    """

    def __init__(
        self,
        *,
        config: InitiativeTimerConfig,
        think_engine: ThinkEngine,
        event_bus: EventBus,
        character_name: str,
        debug_silent_output: bool = False,
        trigger_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> None:
        self.config = config
        self.think_engine = think_engine
        self.event_bus = event_bus
        self.character_name = character_name
        self.debug_silent_output = debug_silent_output
        self.trigger_handler = trigger_handler
        self._state: InitiativeTimerState | None = None
        self._generation = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # 已触发但未完成的回调 id：用于回合互斥后的时效校验——等待期间用户
        # 发言/新计划会让本次触发失效，回调凭此放弃而不是补一条过期主动消息。
        self._active_trigger_id: str | None = None
        self._consecutive_initiative_count = 0  # 用户未回复期间已连续主动发言次数

    def current_payload(self) -> dict[str, Any] | None:
        """返回当前定时器对前端可见的 payload。"""
        if self._state is None or self._state.status != "scheduled":
            return None
        return self._payload(self._state)

    # ------------------------------------------------------------------
    # 对话欲直接调度（§7.3：跳过 LLM 决策/犹豫/兜底）
    # ------------------------------------------------------------------

    async def schedule_intent(
        self,
        *,
        summary: str,
        delay_seconds: int,
        reason: str = "",
        source: str = "drive",
    ) -> dict[str, Any] | None:
        """直接按给定摘要与延迟调度主动定时器。

        供对话欲路径使用：意图与时机已由累积器决定，不再调用 ThinkEngine
        决策、不进入犹豫链与兜底定时器（不违背角色意愿强行安排）。
        """
        if not self.config.enabled or not summary.strip():
            return None
        if self._has_reached_initiative_limit():
            logger.debug("[InitiativeTimer] 已达连续主动上限，对话欲调度跳过")
            return None

        summary = self._trim_summary(summary)
        async with self._lock:
            await self._discard_locked(reason="replaced_by_drive_plan", source=source)
            state = self._create_state(
                delay_seconds=self._clamp_delay(delay_seconds),
                pending_summary=summary,
                reason=reason,
                source=source,
                user_modified=False,
            )
            self._state = state
            self._task = asyncio.create_task(self._run_timer(state.timer_id, state.generation))
            self._publish(SystemEvent.INITIATIVE_TIMER_CREATED, state)
            logger.info(
                f"[InitiativeTimer] 已创建对话欲定时器 {state.timer_id}, "
                f"延迟: {state.delay_seconds}s, 摘要: {summary[:40]}..."
            )
            return self._payload(state)

    def reset_consecutive_initiative_count(self) -> None:
        """用户回复后重置连续主动发言计数器。"""
        if self._consecutive_initiative_count != 0:
            logger.debug(
                f"[InitiativeTimer] 用户回复，重置连续主动计数: "
                f"{self._consecutive_initiative_count} -> 0"
            )
        self._consecutive_initiative_count = 0

    def increment_consecutive_initiative_count(self) -> None:
        """主动消息成功发送后递增计数器。"""
        self._consecutive_initiative_count += 1
        logger.debug(
            f"[InitiativeTimer] 连续主动计数递增: "
            f"{self._consecutive_initiative_count}/{self.config.max_initiative_times}"
        )

    def _has_reached_initiative_limit(self) -> bool:
        """是否已达到当前用户回合允许的最大连续主动发言次数。"""
        max_times = self.config.max_initiative_times
        return max_times > 0 and self._consecutive_initiative_count >= max_times

    # ------------------------------------------------------------------
    # 公共操作
    # ------------------------------------------------------------------

    async def discard(
        self, *, reason: str = "discarded", source: str = "system"
    ) -> dict[str, Any] | None:
        """丢弃当前积存摘要。用户新消息进入时调用。"""
        logger.debug(f"[InitiativeTimer] 外部请求丢弃定时器，原因: {reason}, 来源: {source}")
        if source == "user":
            self.reset_consecutive_initiative_count()
        async with self._lock:
            return await self._discard_locked(reason=reason, source=source)

    async def cancel(
        self, *, timer_id: str | None = None, reason: str = "cancelled", source: str = "user"
    ) -> dict[str, Any]:
        """取消当前定时器并丢弃积存摘要。"""
        async with self._lock:
            state = self._require_current(timer_id)
            self._generation += 1
            state.status = "cancelled"
            state.updated_at = utc_now()
            payload = self._payload(state)
            self._state = None
            self._cancel_task()
            self._active_trigger_id = None
            self._publish(
                SystemEvent.INITIATIVE_TIMER_CANCELLED,
                state,
                extra={"reason": reason, "source": source},
            )
            logger.info(f"[InitiativeTimer] 定时器 {state.timer_id} 被取消，原因: {reason}")
            return {
                "cancelled": True,
                "timer_id": state.timer_id,
                "status": "cancelled",
                "timer": payload,
            }

    async def update(
        self,
        *,
        timer_id: str | None = None,
        delay_seconds: int | float | None = None,
        due_at: str | None = None,
        pending_summary: str | None = None,
    ) -> dict[str, Any]:
        """更新当前定时器触发时间或积存摘要。"""
        if delay_seconds is not None and due_at is not None:
            raise ValueError("delay_seconds and due_at cannot be provided together")

        async with self._lock:
            state = self._require_current(timer_id)
            now = utc_now()
            changed = False
            if due_at is not None:
                parsed_due_at = self._parse_due_at(due_at)
                seconds = max(1, int((parsed_due_at - now).total_seconds()))
                state.delay_seconds = self._clamp_delay(seconds)
                state.due_at = now + timedelta(seconds=state.delay_seconds)
                changed = True
            elif delay_seconds is not None:
                state.delay_seconds = self._clamp_delay(delay_seconds)
                state.due_at = now + timedelta(seconds=state.delay_seconds)
                changed = True

            if pending_summary is not None:
                if not self.config.allow_frontend_edit_summary:
                    raise ValueError("Frontend editing pending_summary is disabled")
                summary = self._trim_summary(pending_summary.strip())
                if not summary:
                    raise ValueError("pending_summary cannot be empty")
                state.pending_summary = summary
                changed = True

            if changed:
                self._generation += 1
                state.generation = self._generation
                state.updated_at = now
                state.user_modified = True
                self._cancel_task()
                self._task = asyncio.create_task(self._run_timer(state.timer_id, state.generation))
                self._publish(SystemEvent.INITIATIVE_TIMER_UPDATED, state, extra={"source": "user"})
                logger.info(
                    f"[InitiativeTimer] 定时器 {state.timer_id} 已更新，"
                    f"新触发时间: {state.due_at.isoformat()}, "
                    f"摘要: {state.pending_summary[:40]}..."
                )
            return self._payload(state)

    async def trigger(self, *, timer_id: str | None = None, source: str = "user") -> dict[str, Any]:
        """立即触发当前积存主动摘要。

        状态变更在锁内、回调在锁外——trigger_handler 可能是一次完整 LLM
        生成，持锁执行会阻塞所有 discard/update/cancel（用户输入卡一整轮）。
        """
        logger.debug(f"[InitiativeTimer] 外部请求立即触发定时器，来源: {source}")
        async with self._lock:
            state = self._require_current(timer_id)
            trigger_args = self._prepare_trigger_locked(state, source=source)
        return await self._execute_trigger_handler(trigger_args)

    async def shutdown(self) -> None:
        """关闭并取消后台定时任务。"""
        logger.debug("[InitiativeTimer] 关闭并清理定时器")
        async with self._lock:
            await self._discard_locked(reason="shutdown", source="system")

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    def _create_state(
        self,
        *,
        delay_seconds: int,
        pending_summary: str,
        reason: str,
        source: str,
        user_modified: bool,
    ) -> InitiativeTimerState:
        now = utc_now()
        self._generation += 1
        state = InitiativeTimerState(
            timer_id=str(uuid4())[:8],
            status="scheduled",
            generation=self._generation,
            source=source,
            created_at=now,
            updated_at=now,
            due_at=now + timedelta(seconds=delay_seconds),
            delay_seconds=delay_seconds,
            pending_summary=pending_summary,
            reason=reason,
            user_modified=user_modified,
        )
        logger.trace(
            f"[InitiativeTimer] 创建状态 #{self._generation} {state.timer_id}: "
            f"source={source}, delay={delay_seconds}s, reason={reason}"
        )
        return state

    def _apply_enthusiasm(self, base_delay: int, enthusiasm: float | None) -> int:
        """根据热情度调整等待时间，再按配置的 min/max_delay_seconds 收敛。

        越想说越快说（delay × (1 - enthusiasm)）；上下限必须走配置而非
        硬编码——硬编码 [30, 600] 会把高热情组合全部钳平成固定 30s，
        也让配置的 max_delay_seconds 永远失效。
        """
        if enthusiasm is None:
            logger.trace("[InitiativeTimer] 未提供热情度，不调整延迟")
            return base_delay
        enthusiasm = max(0.0, min(1.0, float(enthusiasm)))
        adjusted = int(base_delay * (1.0 - enthusiasm))
        result = self._clamp_delay(adjusted)
        logger.trace(
            f"[InitiativeTimer] 热情度调整: base={base_delay}s, enthusiasm={enthusiasm:.2f}, "
            f"adjusted={adjusted}s, clamped={result}s"
        )
        return result

    async def _run_timer(self, timer_id: str, generation: int) -> None:
        try:
            while True:
                trigger_args: dict[str, Any] | None = None
                async with self._lock:
                    state = self._state
                    if not state or state.timer_id != timer_id or state.generation != generation:
                        logger.trace(
                            f"[InitiativeTimer] 定时器 {timer_id} 已失效或代数不匹配，退出循环"
                        )
                        return
                    remaining = (state.due_at - utc_now()).total_seconds()
                    if remaining <= 0:
                        trigger_args = self._prepare_trigger_locked(state, source="timer")
                        logger.info(f"[InitiativeTimer] 定时器 {timer_id} 到期，准备触发主动消息")
                    else:
                        trigger_args = None
                if trigger_args is not None:
                    await self._execute_trigger_handler(trigger_args)
                    return
                await asyncio.sleep(min(remaining, 1.0))
        except asyncio.CancelledError:
            logger.trace(f"[InitiativeTimer] 定时器 {timer_id} 任务被取消")
            raise
        except Exception as error:
            logger.error(f"主动定时器任务异常: {error}")

    def is_active_trigger(self, timer_id: str) -> bool:
        """判断指定触发是否仍未被取代（供回调在回合互斥后做时效校验）。"""
        return self._active_trigger_id is not None and self._active_trigger_id == timer_id

    def _prepare_trigger_locked(
        self, state: InitiativeTimerState, *, source: str
    ) -> dict[str, Any]:
        """在锁内完成状态变更，返回触发参数供锁外回调使用。"""
        self._generation += 1
        state.status = "triggered"
        state.updated_at = utc_now()
        pending_summary = state.pending_summary
        payload = self._payload(state)
        self._state = None
        self._cancel_task()
        self._active_trigger_id = state.timer_id
        self._publish(
            SystemEvent.INITIATIVE_TIMER_TRIGGERED,
            state,
            extra={"pending_summary": pending_summary, "source": source},
        )
        logger.debug(
            f"[InitiativeTimer] 定时器 {state.timer_id} 状态变为 triggered, source={source}"
        )
        return {
            "timer_id": state.timer_id,
            "pending_summary": pending_summary,
            "reason": state.reason,
            "source": source,
            "timer": payload,
        }

    async def _execute_trigger_handler(self, trigger_args: dict[str, Any]) -> dict[str, Any]:
        """在锁外执行 trigger_handler（可能调用 LLM，耗时较长）。"""
        logger.debug(
            f"[InitiativeTimer] 执行 trigger_handler, timer_id={trigger_args.get('timer_id')}"
        )
        result: dict[str, Any] | None = None
        try:
            if self.trigger_handler is not None:
                result = await self.trigger_handler(trigger_args)
            logger.trace(f"[InitiativeTimer] trigger_handler 返回: {result}")
        finally:
            # 回调完成即结束本次触发的有效期；若期间已被取代则不覆盖新状态
            if self._active_trigger_id == trigger_args.get("timer_id"):
                self._active_trigger_id = None
        return {
            "triggered": True,
            "timer_id": trigger_args.get("timer_id", ""),
            "pending_summary": trigger_args.get("pending_summary", ""),
            "timer": trigger_args.get("timer", {}),
            "result": result,
        }

    async def _discard_locked(self, *, reason: str, source: str) -> dict[str, Any] | None:
        state = self._state
        if state is None:
            return None
        self._generation += 1
        state.status = "discarded"
        state.updated_at = utc_now()
        payload = self._payload(state)
        self._state = None
        self._cancel_task()
        # 任何进行中的触发一并失效（用户发言/新计划取代了它）
        self._active_trigger_id = None
        self._publish(
            SystemEvent.INITIATIVE_TIMER_DISCARDED,
            state,
            extra={"reason": reason, "source": source},
        )
        logger.debug(
            f"[InitiativeTimer] 定时器 {state.timer_id} 被丢弃，原因: {reason}, 来源: {source}"
        )
        return payload

    def _require_current(self, timer_id: str | None = None) -> InitiativeTimerState:
        state = self._state
        if state is None or state.status != "scheduled":
            raise ValueError("No active initiative timer")
        if timer_id and timer_id != state.timer_id:
            raise ValueError("initiative timer id does not match current timer")
        return state

    def _cancel_task(self) -> None:
        task = self._task
        if task and not task.done() and task is not asyncio.current_task():
            logger.trace("[InitiativeTimer] 取消旧定时任务")
            task.cancel()
        self._task = None

    def _payload(self, state: InitiativeTimerState) -> dict[str, Any]:
        now = utc_now()
        remaining = max(0, int((state.due_at - now).total_seconds()))
        payload: dict[str, Any] = {
            "timer_id": state.timer_id,
            "status": state.status,
            "generation": state.generation,
            "source": state.source,
            "created_at": state.created_at.isoformat(),
            "updated_at": state.updated_at.isoformat(),
            "due_at": state.due_at.isoformat(),
            "delay_seconds": state.delay_seconds,
            "remaining_seconds": remaining,
            "reason": state.reason,
            "user_modified": state.user_modified,
            "editable_fields": ["due_at", "delay_seconds", "pending_summary"]
            if self.config.allow_frontend_edit_summary
            else ["due_at", "delay_seconds"],
        }
        if self.config.expose_pending_summary:
            payload["pending_summary"] = state.pending_summary
        return payload

    def _publish(
        self,
        event_type: SystemEvent,
        state: InitiativeTimerState,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        data = self._payload(state)
        if extra:
            data.update(extra)
        self.event_bus.publish(Event(type=event_type, source="initiative_timer", data=data))

    def _clamp_delay(self, value: Any) -> int:
        try:
            seconds = int(value)
        except TypeError, ValueError:
            seconds = self.config.min_delay_seconds
        return max(self.config.min_delay_seconds, min(self.config.max_delay_seconds, seconds))

    def _trim_summary(self, summary: str) -> str:
        return summary[: self.config.max_pending_summary_chars].strip()

    @staticmethod
    def _parse_due_at(value: str) -> datetime:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
