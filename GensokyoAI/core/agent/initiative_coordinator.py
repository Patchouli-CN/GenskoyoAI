"""主动定时器编排：Agent 侧的调度、触发与主动消息生成管线。

`InitiativeTimerManager` 负责定时器状态与调度；本协调器负责 Agent 侧的
编排——到点后构建消息、调用模型、发布事件。与 `CoreListeners` 同型：
持有 Agent 引用并访问其服务，使 `_impl.py` 不必承载整段管线。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ...utils.logger import logger
from ..events import Event, SystemEvent
from ..exceptions import AgentError
from .actions import ActionFactory
from .initiative_timer import InitiativeTimerManager
from .prompts import build_initiative_message_context

if TYPE_CHECKING:
    from ._impl import Agent


class InitiativeCoordinator:
    """Agent 主动定时器编排协调器。"""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._manager: InitiativeTimerManager | None = None
        self._last_payload: dict | None = None

    async def schedule_bg(self, full_response: str) -> None:
        """后台调度主动定时器，不阻塞主流程。"""
        try:
            self._last_payload = await self.schedule(full_response)
        except Exception as e:
            logger.error(f"后台调度主动定时器失败: {e}")

    def _ensure_manager(self) -> InitiativeTimerManager:
        if self._manager is None:
            agent = self._agent
            if agent._think_engine is None:
                raise AgentError("ThinkEngine not initialized")
            self._manager = InitiativeTimerManager(
                config=agent.config.initiative_timer,
                think_engine=agent._think_engine,
                event_bus=agent.event_bus,
                character_name=agent.character_name,
                working_memory=agent.working_memory,
                debug_silent_output=agent.config.debug_silent_output,
                trigger_handler=self._handle_trigger,
            )
        return self._manager

    async def schedule(self, assistant_response: str) -> dict | None:
        config = self._agent.config.initiative_timer
        if not config.enabled:
            return None
        # 对话欲路径（§7.3，2026-07-30 用户定稿）：ThinkEngine 四维心情打分，
        # total_drive 超阈值即排定时器，否则沉默——无累积器、无犹豫链、无强制
        return await self._schedule_by_drive(assistant_response)

    # ==================== 对话欲调度（§7.3） ====================

    async def _schedule_by_drive(self, assistant_response: str) -> dict | None:
        """对话欲调度：ThinkEngine 四维评估，超阈值排主动定时器。

        AI 不想说即不说——没有强制 fallback、没有犹豫链、没有累积器。
        """
        agent = self._agent
        config = agent.config.initiative_timer
        if agent._think_engine is None:
            return None

        recent = agent.working_memory.get_recent(6)
        decision = await agent._think_engine.evaluate_speaking_drive(
            assistant_response,
            recent,
            min_delay_seconds=config.min_delay_seconds,
            max_delay_seconds=config.max_delay_seconds,
            decision_max_tokens=config.decision_max_tokens,
            decision_temperature=config.decision_temperature,
        )
        if decision is None:
            return None  # 评估失败：本次不安排（无强制）

        if not decision.want_speak:
            logger.debug(
                f"[Agent] 对话欲不足（{decision.total_drive:.2f} 未达阈值），"
                f"尊重决定保持沉默: {decision.reason}"
            )
            return None

        delay = self._ensure_manager()._apply_enthusiasm(
            decision.delay_seconds, decision.enthusiasm
        )
        return await self._ensure_manager().schedule_intent(
            summary=decision.message,
            delay_seconds=delay,
            reason=decision.reason or "对话欲路径主动调度",
            source="drive",
        )

    async def _handle_trigger(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """定时器到点后：委托 ThinkEngine 说话前思考，再生成真正主动消息。

        整个生成过程持有 Agent 的回合信号量——定时器到点不得与进行中的
        回复并发生成（否则私历顺序错乱）；等待期间若用户发言/新计划取代
        了本次触发，则放弃而不是补一条过期主动消息。

        主动说话统一经 ActionPlanner 的 INITIATIVE_SPEAK 动作出口（SPEAK 只
        服务「回复用户」）：本方法只把触发翻译成动作，生成与发送由
        ActionExecutor 统一执行。
        """
        agent = self._agent
        pending_summary = str(payload.get("pending_summary") or "").strip()
        timer_id = str(payload.get("timer_id") or "").strip()
        logger.debug(f"[Agent] 主动定时器 {timer_id} 触发，待表达摘要: {pending_summary[:60]}...")
        if not pending_summary:
            logger.debug("[Agent] 主动定时器触发时摘要为空，跳过生成")
            return None

        action = ActionFactory.initiative_speak(
            content=pending_summary,
            reason=str(payload.get("reason") or "主动定时器触发"),
        )
        if agent._action_planner is not None:
            agent._action_planner.record_action(action)
        if agent._action_executor is None:
            # 防卫：执行器未就绪时退回本管线直接生成
            async with agent._request_semaphore:
                manager = self._ensure_manager()
                if not manager.is_active_trigger(timer_id):
                    return {"sent": False, "timer_id": timer_id, "aborted": True}
                return await self.generate_initiative_message(
                    timer_id=timer_id, pending_summary=pending_summary
                )
        return await agent._action_executor.execute_initiative_speak(
            action.to_dict(), timer_id=timer_id
        )

    async def generate_initiative_message(
        self, *, timer_id: str, pending_summary: str
    ) -> dict[str, Any] | None:
        """生成并发布主动消息（调用方必须已持有回合信号量并完成时效校验）。

        主动定时器触发与 ActionPlanner 主动说话（思考冲动）共用本管线：
        入参统一为「待表达意图摘要」（存意图不存话术），经说话前思考 +
        即时生成产出真正发给用户的消息。
        """
        agent = self._agent
        await agent._ensure_background_manager()
        tool_build_result = await agent._build_tools()
        agent.message_builder.update_tool_build_result(tool_build_result)

        # 委托 ThinkEngine 进行说话前思考
        recent_messages = agent.working_memory.get_recent(8)
        recent_context = "\n".join(
            f"{item.get('role', 'unknown')}: {item.get('content', '')}"
            for item in recent_messages
            if isinstance(item.get("content"), str)
        )
        if agent._think_engine is None:
            raise AgentError("ThinkEngine not initialized")
        thought = await agent._think_engine.pre_speak_thought(
            pending_summary=pending_summary,
            recent_context=recent_context,
            max_tokens=agent.config.think_engine.think_max_tokens,
            temperature=agent.config.think_engine.think_temperature,
        )

        system_contexts = [build_initiative_message_context(pending_summary, thought)]
        system_contexts = await agent._prepend_scene_context(system_contexts)
        messages = agent.message_builder.build("", system_contexts)
        # 工作记忆末尾是助手自己的上一条回复，必须补一条 user 消息让模型继续生成下一句
        messages.append(
            {
                "role": "user",
                "content": "（此刻没有新的用户消息。把上面想好的内容，用你自己的口吻自然地说出来——就像你刚好想到了、随口开口那样。）",
            }
        )
        max_tokens = agent.config.think_engine.initiative_max_tokens
        initiative_options: dict[str, Any] = {
            "temperature": agent.config.think_engine.initiative_temperature,
        }
        if max_tokens > 0:
            initiative_options["num_predict"] = max_tokens
            initiative_options["max_tokens"] = max_tokens
        use_stream = agent.config.model.stream

        logger.trace(
            f"[Agent] 主动消息生成请求 messages:\n"
            f"{json.dumps(messages, ensure_ascii=False, indent=2, default=str)}"
        )

        message = ""
        try:
            if use_stream:
                chunks: list[str] = []
                async for chunk in agent._model_client.chat_stream(
                    messages=messages,
                    options=initiative_options,
                ):
                    if agent.is_shutting_down:
                        break
                    chunk_text = chunk.content if hasattr(chunk, "content") else ""
                    if chunk_text:
                        chunks.append(chunk_text)
                        logger.trace(f"[Agent] 主动消息流式 chunk: {chunk_text!r}")
                        agent.event_bus.publish(
                            Event(
                                type=SystemEvent.THINK_ENGINE_INITIATIVE_CHUNK,
                                source="initiative_timer",
                                data={"content": chunk_text, "done": False},
                            )
                        )
                message = "".join(chunks).strip()
                logger.debug(f"[Agent] 主动消息流式生成完成，长度: {len(message)}")
                # 发送流式结束标记
                agent.event_bus.publish(
                    Event(
                        type=SystemEvent.THINK_ENGINE_INITIATIVE_CHUNK,
                        source="initiative_timer",
                        data={"content": "", "done": True},
                    )
                )
            else:
                response = await agent._model_client.chat(
                    messages=messages,
                    options=initiative_options,
                )
                content = response.message.content
                message = content.strip() if isinstance(content, str) else ""
                logger.debug(f"[Agent] 主动消息非流式生成完成，长度: {len(message)}")
        except Exception as error:
            logger.error(f"主动定时器主动消息生成失败: {error}")
            message = ""

        if not message:
            return {
                "sent": False,
                "timer_id": timer_id,
                "pending_summary": pending_summary,
                "thought": thought,
            }

        # 发布完整消息事件（供持久化/记忆记录等下游消费）
        agent.event_bus.publish(
            Event(
                type=SystemEvent.THINK_ENGINE_INITIATIVE,
                source="initiative_timer",
                data={
                    "message": message,
                    "timer_id": timer_id,
                    "pending_summary": pending_summary,
                    "thought": thought,
                },
            )
        )
        agent.event_bus.publish(
            Event(
                type=SystemEvent.MESSAGE_SENT,
                source="initiative_timer",
                data={
                    "content": message,
                    "initiative": True,
                    "timer_id": timer_id,
                    "pending_summary": pending_summary,
                },
            )
        )
        logger.info(
            f"[Agent] 主动消息已发送，timer_id={timer_id}, 长度={len(message)}, "
            f"内容: {message[:80]}..."
        )

        # 主动发言成功：递增计数，并在未达上限时继续调度下一轮主动定时器
        self._ensure_manager().increment_consecutive_initiative_count()
        if self._manager is not None and not self._manager._has_reached_initiative_limit():
            logger.debug("[Agent] 未达连续主动上限，继续调度下一轮主动定时器")
            self._last_payload = await self.schedule(message)

        return {
            "sent": True,
            "timer_id": timer_id,
            "pending_summary": pending_summary,
            "message": message,
            "thought": thought,
        }

    async def discard(self, *, reason: str = "discarded", source: str = "system") -> dict | None:
        if self._manager is None:
            return None
        self._last_payload = None
        if source == "user":
            self._manager.reset_consecutive_initiative_count()
        return await self._manager.discard(reason=reason, source=source)

    def current(self) -> dict | None:
        if self._manager is None:
            return None
        return self._manager.current_payload()

    async def update(
        self,
        *,
        timer_id: str | None = None,
        delay_seconds: int | float | None = None,
        due_at: str | None = None,
        pending_summary: str | None = None,
        enabled: bool | None = None,
    ) -> dict:
        # enabled 是进程内运行时开关（不落盘）：关闭即废止当前待发计划，
        # 之后 schedule()/schedule_intent() 会在 config.enabled 检查处直接短路，
        # 供 QQ Bot 等无主动消息投递通道的接入方彻底停用主动发言。
        if enabled is None:
            payload = await self._ensure_manager().update(
                timer_id=timer_id,
                delay_seconds=delay_seconds,
                due_at=due_at,
                pending_summary=pending_summary,
            )
            self._last_payload = payload
            return payload
        self._agent.config.initiative_timer.enabled = bool(enabled)
        if not enabled:
            await self.discard(reason="initiative_timer.disabled", source="runtime")
        if any(value is not None for value in (timer_id, delay_seconds, due_at, pending_summary)):
            payload = await self._ensure_manager().update(
                timer_id=timer_id,
                delay_seconds=delay_seconds,
                due_at=due_at,
                pending_summary=pending_summary,
            )
            self._last_payload = payload
        else:
            payload = {"timer": self.current()}
        return {**payload, "enabled": self._agent.config.initiative_timer.enabled}

    async def cancel(self, *, timer_id: str | None = None, reason: str = "cancelled") -> dict:
        self._last_payload = None
        return await self._ensure_manager().cancel(timer_id=timer_id, reason=reason)

    async def trigger(self, *, timer_id: str | None = None) -> dict:
        self._last_payload = None
        return await self._ensure_manager().trigger(timer_id=timer_id)

    async def shutdown(self) -> None:
        if self._manager:
            await self._manager.shutdown()
