"""主动定时器编排：Agent 侧的调度、触发与主动消息生成管线。

`InitiativeTimerManager` 负责定时器状态与调度；本协调器负责 Agent 侧的
编排——到点后构建消息、调用模型、发布事件。与 `CoreListeners` 同型：
持有 Agent 引用并访问其服务，使 `_impl.py` 不必承载整段管线。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ...utils.logger import logger
from ..config import ConfigLoader
from ..events import Event, SystemEvent
from ..exceptions import AgentError
from .drive_accumulator import DriveAccumulator
from .initiative_timer import InitiativeTimerManager

if TYPE_CHECKING:
    from ._impl import Agent


class InitiativeCoordinator:
    """Agent 主动定时器编排协调器。"""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._manager: InitiativeTimerManager | None = None
        self._last_payload: dict | None = None
        self._drive_accumulator: DriveAccumulator | None = None

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
        if config.drive_enabled:
            # 对话欲路径（§7.3）：纯算术累积，跨阈值才调一次 LLM 生成意图摘要
            return await self._schedule_drive(assistant_response)
        return await self._ensure_manager().schedule_after_response(assistant_response)

    # ==================== 对话欲调度（§7.3） ====================

    def _ensure_drive_accumulator(self) -> DriveAccumulator:
        """取当前会话的对话欲累积器（从 session.metadata 恢复）。"""
        if self._drive_accumulator is None:
            session = self._agent.session_manager.get_current_session()
            data = session.metadata.get("initiative_drive") if session is not None else None
            self._drive_accumulator = DriveAccumulator.from_dict(
                self._agent.config.initiative_timer, data
            )
        return self._drive_accumulator

    def reset_drive_state(self) -> None:
        """会话切换后丢弃对话欲缓存（下次访问时从新会话 metadata 恢复）。"""
        self._drive_accumulator = None

    def _persist_drive(self, accumulator: DriveAccumulator) -> None:
        """把对话欲状态写进会话 metadata（随后会话落盘时一并持久化）。"""
        session = self._agent.session_manager.get_current_session()
        if session is not None:
            session.metadata["initiative_drive"] = accumulator.to_dict()

    def drive_status(self) -> dict:
        """对话欲当前状态（测试与调试可见）。"""
        config = self._agent.config.initiative_timer
        status: dict = {"enabled": config.drive_enabled}
        if config.drive_enabled:
            accumulator = self._ensure_drive_accumulator()
            status["drive"] = accumulator.current_drive()
            status["mood"] = accumulator.mood
        return status

    async def _schedule_drive(self, assistant_response: str) -> dict | None:
        """对话欲路径：短期思考接入四维动机，一次 LLM 智能调度，动机四维回灌累积器。

        AI 决定不发言即不发言——没有强制 fallback、没有犹豫链。
        """
        agent = self._agent
        config = agent.config.initiative_timer
        if agent._think_engine is None:
            return None
        accumulator = self._ensure_drive_accumulator()
        # 先惰性结算时间效应（沉默低权重累积 + 心情非对称衰减），
        # 再把状态交给短期思考做智能调度
        accumulator.current_drive()

        recent = agent.working_memory.get_recent(6)
        decision = await agent._think_engine.decide_drive_initiative(
            assistant_response,
            recent,
            drive=accumulator.drive,
            mood=accumulator.mood,
            min_delay_seconds=config.min_delay_seconds,
            max_delay_seconds=config.max_delay_seconds,
            decision_max_tokens=config.decision_max_tokens,
            decision_temperature=config.decision_temperature,
        )
        if decision is None:
            return None  # 解析失败：本次不安排（无强制）

        valence = 0.0
        if agent._semantic_memory is not None:
            valence = agent._semantic_memory.recent_average_valence()
        scene_match = await self._check_scene_topic_match()
        drive = accumulator.record_turn(
            emotional_valence=valence,
            motivation=decision.motivation,
            scene_match=scene_match,
        )
        self._persist_drive(accumulator)
        logger.debug(
            f"[Agent] 对话欲累积: {drive:.2f} (mood {accumulator.mood:+.2f}, "
            f"{decision.motivation.to_prompt_context()})"
        )

        if not decision.should_schedule or not decision.summary:
            logger.debug(
                f"[Agent] 对话欲决策不主动发言，尊重决定（无强制 fallback）: {decision.reason}"
            )
            return None

        delay = InitiativeTimerManager._apply_enthusiasm(
            decision.delay_seconds, decision.enthusiasm
        )
        return await self._ensure_manager().schedule_intent(
            summary=decision.summary,
            delay_seconds=delay,
            reason=decision.reason or "对话欲路径主动调度",
            source="drive",
        )

    async def _check_scene_topic_match(self) -> bool:
        """挂心话题与当前场景匹配检查（纯查表，零 LLM）。"""
        agent = self._agent
        try:
            if agent._semantic_memory is None or not agent.scene_manager.enabled:
                return False
            scene = await agent.scene_manager.get_current_scene()
            if scene is None:
                return False
            scene_text = f"{scene.name} {scene.description}".lower()
            return any(
                topic.name.lower() in scene_text
                for topic in agent._semantic_memory._store.recent_topics(5)
            )
        except Exception as error:
            logger.debug(f"[Agent] 场景话题匹配检查失败（按不匹配处理）: {error}")
            return False

    async def _handle_trigger(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """定时器到点后：委托 ThinkEngine 说话前思考，再生成真正主动消息。

        整个生成过程持有 Agent 的回合信号量——定时器到点不得与进行中的
        回复并发生成（否则私历顺序错乱）；等待期间若用户发言/新计划取代
        了本次触发，则放弃而不是补一条过期主动消息。
        """
        agent = self._agent
        pending_summary = str(payload.get("pending_summary") or "").strip()
        timer_id = str(payload.get("timer_id") or "").strip()
        logger.debug(f"[Agent] 主动定时器 {timer_id} 触发，待表达摘要: {pending_summary[:60]}...")
        if not pending_summary:
            logger.debug("[Agent] 主动定时器触发时摘要为空，跳过生成")
            return None

        async with agent._request_semaphore:
            manager = self._ensure_manager()
            if not manager.is_active_trigger(timer_id):
                logger.debug(f"[Agent] 主动触发 {timer_id} 已被新消息/新计划取代，放弃本次生成")
                return {"sent": False, "timer_id": timer_id, "aborted": True}
            return await self._generate_initiative_message(
                timer_id=timer_id, pending_summary=pending_summary
            )

    async def _generate_initiative_message(
        self, *, timer_id: str, pending_summary: str
    ) -> dict[str, Any] | None:
        """生成并发布主动消息（调用方必须已持有回合信号量并完成时效校验）。"""
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

        system_contexts = [
            "【主动定时器触发 · 无新用户输入】\n"
            "用户没有发送任何新消息。这是你自己在之前的回复中决定要说的话，现在到了该开口的时刻。\n"
            "你的任务是：衔接你刚才的最后一句话，自然地把话题延续下去，而不是回应一个新的问题。\n"
            "不要重复你刚才已经说过的内容；不要反问用户“为什么又问一遍”或表现出被重复打扰；"
            "不要解释定时器、摘要或内部思考；直接以你的角色口吻自然开口。\n"
            f"待表达意图摘要：{pending_summary}\n"
            f"说话前内部思考：{thought or '无'}"
        ]
        system_contexts = await agent._prepend_scene_context(system_contexts)
        messages = agent.message_builder.build("", system_contexts)
        # 工作记忆末尾是助手自己的上一条回复，必须补一条 user 消息让模型继续生成下一句
        messages.append(
            {
                "role": "user",
                "content": "（没有新用户输入，这是你自己决定要说的话，请按照上面的摘要和内部思考自然地主动开口。）",
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

        # 主动发言成功：对话欲泄压（说完话，表达欲部分释放）
        if (
            self._agent.config.initiative_timer.drive_enabled
            and self._drive_accumulator is not None
        ):
            self._drive_accumulator.vent()
            self._persist_drive(self._drive_accumulator)

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

    def hesitation_status(self) -> dict:
        config = self._agent.config.initiative_timer
        return {
            "enabled": config.hesitation_enabled,
            "max_rounds": config.hesitation_max_rounds,
            "delay_seconds": config.hesitation_delay_seconds,
        }

    def set_hesitation_enabled(self, enabled: bool, *, persist: bool = True) -> dict:
        self._agent.config.initiative_timer.hesitation_enabled = bool(enabled)
        config_path: str | None = None
        if persist:
            path = ConfigLoader.set_initiative_hesitation_enabled(
                getattr(self._agent, "config_file", None),
                bool(enabled),
            )
            config_path = str(path)
        payload = self.hesitation_status()
        payload["config_path"] = config_path
        return payload

    async def update(
        self,
        *,
        timer_id: str | None = None,
        delay_seconds: int | float | None = None,
        due_at: str | None = None,
        pending_summary: str | None = None,
    ) -> dict:
        payload = await self._ensure_manager().update(
            timer_id=timer_id,
            delay_seconds=delay_seconds,
            due_at=due_at,
            pending_summary=pending_summary,
        )
        self._last_payload = payload
        return payload

    async def cancel(self, *, timer_id: str | None = None, reason: str = "cancelled") -> dict:
        self._last_payload = None
        return await self._ensure_manager().cancel(timer_id=timer_id, reason=reason)

    async def trigger(self, *, timer_id: str | None = None) -> dict:
        self._last_payload = None
        return await self._ensure_manager().trigger(timer_id=timer_id)

    async def shutdown(self) -> None:
        if self._manager:
            await self._manager.shutdown()
