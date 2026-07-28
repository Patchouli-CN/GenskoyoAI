"""行动执行器 - 执行 ActionPlanner 的决策"""

# GensokyoAI/core/agent/action_executor.py

import asyncio
from typing import TYPE_CHECKING, Any

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
            case "REMEMBER":
                await self._execute_remember(event)
            case "RECALL":
                await self._execute_recall(event)
            case _:
                logger.debug(f"⚡ [ActionExecutor] 未知行动: {action_type}")

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
        data: dict[str, Any] = {
            "user_input": user_input,
            "request_id": event.id,
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
        """执行 INITIATIVE_SPEAK - 主动说话"""
        action_data = event.data.get("action", {})
        message = action_data.get("content", "")

        if message:
            self.event_bus.publish(
                Event(
                    type=SystemEvent.THINK_ENGINE_INITIATIVE,
                    source="action_executor",
                    data={"message": message},
                )
            )
            self.event_bus.publish(
                Event(
                    type=SystemEvent.MESSAGE_SENT,
                    source="agent",
                    data={"content": message, "initiative": True},
                )
            )

    async def _execute_wait(self, event: Event) -> None:
        """执行 WAIT - 什么都不做"""
        action_data = event.data.get("action", {})
        logger.debug(f"🤫 [ActionExecutor] WAIT: {action_data.get('reason', '')}")

        if self._response_future and not self._response_future.done():
            self._response_future.set_result("")
            self._cleanup_response()

    async def _execute_remember(self, event: Event) -> None:
        """执行 REMEMBER - 调用记忆工具"""
        action_data = event.data.get("action", {})
        self.event_bus.publish(
            Event(
                type=SystemEvent.MEMORY_SEMANTIC_ADDED,
                source="action_executor",
                data={
                    "content": action_data.get("content", ""),
                    "importance": action_data.get("params", {}).get("importance", 0.5) / 10.0,
                    "topic_name": action_data.get("params", {}).get("topic"),
                },
            )
        )

    async def _execute_recall(self, event: Event) -> None:
        """执行 RECALL - 回忆"""
        action_data = event.data.get("action", {})
        keyword = action_data.get("content", "")

        response = await self.event_bus.request(
            Event(
                type=SystemEvent.MEMORY_SEMANTIC_RECALLED,
                source="action_executor",
                data={"keyword": keyword},
            ),
            timeout=5.0,
        )

        if response:
            self.event_bus.publish(
                Event(type=SystemEvent.MESSAGE_SENT, source="agent", data={"content": response})
            )

    # ==================== 流式响应支持 ====================

    def prepare_response(self) -> asyncio.Future:
        """准备接收响应。"""
        self._response_future = asyncio.Future()
        self._stream_queue = asyncio.Queue()
        return self._response_future

    async def feed_chunk(self, chunk: StreamChunk) -> None:
        """喂入流式块。"""
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

    def complete_response(self, full_response: str = "") -> None:
        """响应完成。只解析 future，不清空流式队列——消费方可能还没排完
        最后几个 chunk，清空会丢失流尾；队列随下一次 prepare_response 整体替换。"""
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
