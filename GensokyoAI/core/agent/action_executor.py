"""行动执行器 - 执行 ActionPlanner 的决策"""

# GensokyoAI/core/agent/action_executor.py

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ...utils.logger import logger
from ..events import Event, EventBus, EventPriority, SystemEvent
from .types import StreamChunk

if TYPE_CHECKING:
    from ._impl import Agent


class ActionExecutor:
    """
    行动执行器 - 执行决策

    咲夜：行动要快，比我的时停还快！
    """

    def __init__(self, agent: Agent, event_bus: EventBus):
        self.agent = agent
        self.event_bus = event_bus

        # 流式响应管理
        self._stream_queue: asyncio.Queue[StreamChunk] | None = None
        self._response_future: asyncio.Future | None = None
        # 当前响应槽位绑定的请求 id：超时/取消后旧的「孤儿生成」仍在事件 worker
        # 里运行，其 feed/complete 必须凭 id 判定为过期并丢弃，否则会把下一个
        # 请求的 future 用旧回复解决（详见 _impl.py 的 60s 超时路径）。
        self._current_request_id: str | None = None

        self._subscribe_events()
        logger.debug("⚡ [ActionExecutor] 初始化完成")

    def _subscribe_events(self) -> None:
        """订阅行动决策事件"""
        self.event_bus.subscribe(
            SystemEvent.ACTION_DECIDED, self._on_action_decided, priority=EventPriority.HIGH
        )

    # ==================== 事件处理 ====================

    async def _on_action_decided(self, event: Event) -> None:
        """收到行动决策 - 执行它"""
        action_data = event.data.get("action", {})
        action_type = action_data.get("type")
        user_input = event.data.get("user_input", "")

        logger.info(f"⚡ [ActionExecutor] 执行: {action_type}")

        match action_type:
            case "SPEAK":
                await self._execute_speak(event, user_input)
            case "INITIATIVE_SPEAK":
                await self._execute_initiative_speak(event)
            case "WAIT":
                await self._execute_wait(event)
            case _:
                logger.debug(f"⚡ [ActionExecutor] 未知行动: {action_type}")

        if action_type != "INITIATIVE_SPEAK":
            # INITIATIVE_SPEAK 的 ACTION_EXECUTED 由 execute_initiative_speak 统一发布
            # （定时器转发路径不过本事件链，避免双发）
            self.event_bus.publish(
                Event(
                    type=SystemEvent.ACTION_EXECUTED,
                    source="action_executor",
                    data={"action": action_data},
                )
            )

    # ==================== 执行方法 ====================

    async def _execute_speak(self, event: Event, user_input: str) -> None:
        """执行 SPEAK - 请求生成响应"""
        # 发布生成响应事件，由 ResponseHandler 订阅处理；
        # 透传本轮系统上下文与 world 标记（World 舞台/在场/共享剧本）。
        # request_id 优先取发送方在 MESSAGE_RECEIVED 时铸造的绑定 id（用于
        # 超时后识别孤儿生成），无绑定的旧路径回退为本事件 id。
        data: dict[str, Any] = {
            "user_input": user_input,
            "request_id": event.data.get("request_id") or event.id,
        }
        if system_contexts := event.data.get("system_contexts"):
            data["system_contexts"] = system_contexts
        if event.data.get("world_turn"):
            data["world_turn"] = True
        self.event_bus.publish(
            Event(
                type=SystemEvent.GENERATE_RESPONSE,
                source="action_executor",
                data=data,
            )
        )

    async def _execute_initiative_speak(self, event: Event) -> None:
        """执行 INITIATIVE_SPEAK（事件链入口）。"""
        action_data = event.data.get("action", {})
        await self.execute_initiative_speak(action_data)

    async def execute_initiative_speak(
        self, action: dict[str, Any], *, timer_id: str | None = None
    ) -> dict[str, Any] | None:
        """主动说话统一出口：意图摘要即时生成真正消息并返回结果。

        action.content 的语义是「待表达意图摘要」（存意图不存话术）：
        统一走说话前思考 + 即时生成管线（与主动定时器触发同一条路径），
        杜绝评估时预写的话术被直接当作定稿发送。
        ActionPlanner 主动说话（事件链）与主动定时器触发（coordinator 转发）
        都经本方法发出；timer_id 用于定时器触发的时效校验。
        """
        intent = str(action.get("content") or "").strip()
        if not intent:
            return {"sent": False, "reason": "empty_intent"}
        agent = self.agent
        if agent._think_engine is None:
            return {"sent": False, "reason": "think_engine_unavailable"}
        # 与进行中的回复互斥：主动生成不得插队写乱私历
        async with agent._request_semaphore:
            if timer_id:
                manager = agent._initiative_coordinator._ensure_manager()
                if not manager.is_active_trigger(timer_id):
                    logger.debug(
                        f"⚡ [ActionExecutor] 主动触发 {timer_id} 已被新消息/新计划取代，放弃"
                    )
                    return {"sent": False, "timer_id": timer_id, "aborted": True}
            result = await agent._initiative_coordinator.generate_initiative_message(
                timer_id=timer_id or f"thought-{uuid4().hex[:8]}",
                pending_summary=intent,
            )
        self.event_bus.publish(
            Event(
                type=SystemEvent.ACTION_EXECUTED,
                source="action_executor",
                data={"action": action},
            )
        )
        return result

    async def _execute_wait(self, event: Event) -> None:
        """执行 WAIT - 什么都不做"""
        action_data = event.data.get("action", {})
        logger.debug(f"🤫 [ActionExecutor] WAIT: {action_data.get('reason', '')}")

        # 过期请求的 WAIT 不得解决新请求的 future
        if not self.is_current_request(event.data.get("request_id")):
            logger.debug("🤫 [ActionExecutor] 忽略过期请求的 WAIT")
            return
        if self._response_future and not self._response_future.done():
            self._response_future.set_result("")
            self._cleanup_response()

    # ==================== 流式响应支持 ====================

    def prepare_response(self, request_id: str | None = None) -> asyncio.Future:
        """准备接收响应，并把新槽位绑定到该请求 id。"""
        self._response_future = asyncio.Future()
        self._stream_queue = asyncio.Queue()
        self._current_request_id = request_id
        return self._response_future

    def is_current_request(self, request_id: str | None) -> bool:
        """判断给定请求是否仍持有当前响应槽位。

        `request_id=None` 表示旧式无绑定事件，视为当前（保持兼容）。
        """
        if request_id is None:
            return True
        return self._response_future is not None and self._current_request_id == request_id

    async def feed_chunk(self, chunk: StreamChunk, request_id: str | None = None) -> None:
        """喂入流式块；过期请求的 chunk 直接丢弃。"""
        if not self.is_current_request(request_id):
            logger.debug(f"⚡ [ActionExecutor] 丢弃过期请求的流式块: {request_id}")
            return
        if self._stream_queue:
            await self._stream_queue.put(chunk)

    async def get_chunk(self) -> StreamChunk | None:
        """获取下一个流式块。"""
        if self._stream_queue:
            return await self._stream_queue.get()
        return None

    def get_chunk_nowait(self) -> StreamChunk | None:
        """非阻塞获取下一个流式块；无队列或队列为空时返回 None。"""
        if self._stream_queue:
            try:
                return self._stream_queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
        return None

    def complete_response(self, full_response: str = "", request_id: str | None = None) -> None:
        """响应完成。只解析 future，不清空流式队列——消费方可能还没排完
        最后几个 chunk，清空会丢失流尾；队列随下一次 prepare_response 整体替换。
        过期请求的 complete 直接忽略，不得解决新请求的 future。"""
        if not self.is_current_request(request_id):
            logger.debug(f"⚡ [ActionExecutor] 忽略过期请求的 complete: {request_id}")
            return
        if self._response_future and not self._response_future.done():
            self._response_future.set_result(full_response)

    def cancel_response(self, reason: str = "cancelled") -> None:
        """取消当前响应并清理队列，避免半截流继续污染下一轮请求。"""
        if self._response_future and not self._response_future.done():
            self._response_future.cancel(reason)
        self._cleanup_response()

    def _cleanup_response(self) -> None:
        if self._stream_queue:
            while not self._stream_queue.empty():
                try:
                    self._stream_queue.get_nowait()
                    self._stream_queue.task_done()
                except asyncio.QueueEmpty, ValueError:
                    break
        self._response_future = None
        self._stream_queue = None
        self._current_request_id = None
