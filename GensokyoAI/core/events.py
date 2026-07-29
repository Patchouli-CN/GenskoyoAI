"""事件系统 - 解耦所有组件"""

import asyncio
import contextlib
import inspect
from collections.abc import Callable, Iterable
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from msgspec import Struct, field

from ..utils.helpers import utc_now
from ..utils.logger import logger


class EventPriority(Enum):
    """事件优先级"""

    HIGHEST = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    LOWEST = 4


class SystemEvent(Enum):
    """系统事件类型"""

    # 生命周期事件
    AGENT_STARTED = "agent.started"
    AGENT_SHUTDOWN = "agent.shutdown"
    AGENT_SHUTDOWN_COMPLETE = "agent.shutdown.complete"

    # 会话事件
    SESSION_CREATED = "session.created"
    SESSION_RESUMED = "session.resumed"
    SESSION_DELETED = "session.deleted"
    SESSION_SWITCHED = "session.switched"

    # 对话事件
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_PROCESSING = "message.processing"
    MESSAGE_SENT = "message.sent"
    MESSAGE_STREAM_CHUNK = "message.stream.chunk"

    # 响应生成事件（替代直接调用）
    GENERATE_RESPONSE = "generate.response"  # 请求生成响应
    RESPONSE_STREAMING = "response.streaming"  # 流式响应中
    RESPONSE_COMPLETED = "response.completed"  # 响应完成

    # 记忆事件
    MEMORY_WORKING_ADDED = "memory.working.added"
    MEMORY_EPISODIC_COMPRESSED = "memory.episodic.compressed"
    MEMORY_SEMANTIC_ADDED = "memory.semantic.added"
    MEMORY_SEMANTIC_RECALLED = "memory.semantic.recalled"
    MEMORY_SEMANTIC_UPDATED = "memory.semantic.updated"
    MEMORY_SEMANTIC_ADDED_RESPONSE = "memory.semantic.added.response"
    MEMORY_SEMANTIC_RECALLED_RESPONSE = "memory.semantic.recalled.response"

    # 工具事件
    TOOL_CALL_SELECTED = "tool.call.selected"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_PROGRESS = "tool.call.progress"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    TOOL_CALL_FAILED = "tool.call.failed"

    # 外部工具源生命周期事件
    EXTERNAL_TOOL_STARTING = "external_tool.starting"
    EXTERNAL_TOOL_RUNNING = "external_tool.running"
    EXTERNAL_TOOL_STOPPING = "external_tool.stopping"
    EXTERNAL_TOOL_FAILED = "external_tool.failed"
    EXTERNAL_TOOL_RECONNECTING = "external_tool.reconnecting"

    # 持久化事件
    PERSISTENCE_SAVE_STARTED = "persistence.save.started"
    PERSISTENCE_SAVE_COMPLETED = "persistence.save.completed"
    PERSISTENCE_SAVE_FAILED = "persistence.save.failed"

    # 错误事件
    ERROR_OCCURRED = "error.occurred"
    MODEL_ERROR = "error.model"
    TOOL_ERROR = "error.tool"

    # 模型观测事件
    MODEL_CALL_TIMING = "model.call.timing"
    MODEL_AUTH = "model.auth"
    MODEL_REQUEST_STARTED = "model.request_started"
    MODEL_RETRY_SCHEDULED = "model.retry_scheduled"
    MODEL_FIRST_TOKEN = "model.first_token"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"

    # 后台任务事件
    BACKGROUND_TASK_SUBMITTED = "background.task.submitted"
    BACKGROUND_TASK_COMPLETED = "background.task.completed"
    BACKGROUND_TASK_FAILED = "background.task.failed"
    BACKGROUND_WORKER_STARTED = "background.worker.started"
    BACKGROUND_WORKER_IDLE = "background.worker.idle"
    BACKGROUND_WORKER_FAILED = "background.worker.failed"

    # 思考引擎事件
    THINK_ENGINE_THOUGHT = "think.engine.thought"  # 静默思考完成
    THINK_ENGINE_INITIATIVE = "think.engine.initiative"  # 产生主动消息（完整）
    THINK_ENGINE_INITIATIVE_CHUNK = "think.engine.initiative.chunk"  # 主动消息流式片段

    # 安全事件
    SECURITY_PROMPT_INJECTION_DETECTED = "security.prompt_injection_detected"

    # 主动定时器事件
    INITIATIVE_TIMER_CREATED = "initiative_timer.created"
    INITIATIVE_TIMER_UPDATED = "initiative_timer.updated"
    INITIATIVE_TIMER_CANCELLED = "initiative_timer.cancelled"
    INITIATIVE_TIMER_TRIGGERED = "initiative_timer.triggered"
    INITIATIVE_TIMER_DISCARDED = "initiative_timer.discarded"

    # 行动决策事件
    ACTION_DECIDED = "action.decided"  # ActionPlanner 决策完成
    ACTION_EXECUTED = "action.executed"  # ActionExecutor 执行完成

    # 场景事件
    SCENE_SWITCH_REQUESTED = "scene.switch_requested"  # 工具请求切换场景（内部 request/response）
    SCENE_QUERY_CURRENT = "scene.query_current"  # 工具查询当前场景（内部 request/response）
    SCENE_SWITCHED = "scene.switched"  # 场景切换成功后广播，供前端提示当前场景

    # World 编排事件
    WORLD_DIRECTOR_DECISION = "world.director.decision"  # 导演完成一次选角决策
    WORLD_STARTED = "world.started"  # World 完成装配与开场
    WORLD_SHUTDOWN = "world.shutdown"  # World 关闭
    WORLD_ACTOR_TURN_STARTED = "world.actor.started"  # 某 Actor 开始一回合发言
    WORLD_ACTOR_TURN_CHUNK = "world.actor.chunk"  # Actor 发言的流式片段
    WORLD_ACTOR_TURN_COMPLETED = "world.actor.completed"  # 某 Actor 回合完成
    WORLD_SCENE_MOVED = "world.scene.moved"  # 舞台上角色/用户位置变化（含用户跟随）
    WORLD_WAITING_USER = "world.waiting_user"  # 一段自动表演结束，等待用户


class Event(Struct):
    """事件"""

    type: SystemEvent
    # 全量 uuid：8 位短 id（32 bit）在并发 pending 请求下会撞键，
    # 导致 respond 串到错误的等待方（跨工具串答）
    id: str = field(default_factory=lambda: uuid4().hex)
    source: str = "unknown"
    data: Any = None
    timestamp: datetime = field(default_factory=utc_now)
    metadata: dict = field(default_factory=dict)

    @property
    def type_str(self) -> str:
        return self.type.value


class Subscription:
    """订阅信息"""

    def __init__(
        self,
        handler: Callable,
        priority: EventPriority = EventPriority.NORMAL,
        once: bool = False,
        filter_func: Callable[[Event], bool] | None = None,
    ):
        self.id = str(uuid4())[:8]
        self.handler = handler
        self.priority = priority
        self.once = once
        self.filter_func = filter_func
        self.handler_name = self._get_handler_name(handler)

    def _get_handler_name(self, handler: Callable) -> str:
        if hasattr(handler, "__name__"):
            return handler.__name__
        elif hasattr(handler, "__class__"):
            return f"{handler.__class__.__name__}.handle"
        elif hasattr(handler, "__self__"):
            return f"{handler.__self__.__class__.__name__}.{getattr(handler, '__name__', 'handle')}"  # type: ignore
        else:
            return str(handler)[:50]


class EventBus:
    """事件总线 - 完全解耦的发布订阅，带完整追踪日志"""

    def __init__(
        self,
        enable_trace: bool = True,
        *,
        max_queue_size: int = 1000,
        stop_drain_timeout: float = 5.0,
        critical_events: Iterable[SystemEvent] | None = None,
    ):
        self._subscribers: dict[SystemEvent, list[Subscription]] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._event_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue_size)
        self._worker_task: asyncio.Task | None = None
        self._stats = {
            "published": 0,
            "delivered": 0,
            "errors": 0,
            "filtered": 0,
            "dropped": 0,
            "critical_flushed": 0,
        }

        self.enable_trace = enable_trace
        self._max_queue_size = max_queue_size
        self._stop_drain_timeout = stop_drain_timeout
        self._critical_events = set(
            critical_events
            or {
                SystemEvent.PERSISTENCE_SAVE_STARTED,
                SystemEvent.PERSISTENCE_SAVE_COMPLETED,
                SystemEvent.PERSISTENCE_SAVE_FAILED,
                SystemEvent.ERROR_OCCURRED,
                SystemEvent.AGENT_SHUTDOWN,
                SystemEvent.AGENT_SHUTDOWN_COMPLETE,
            }
        )

        # 请求-响应机制
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._response_timeout = 30.0

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._event_worker())

        if self.enable_trace:
            logger.info("🚀 [EventBus] 事件总线已启动")

    async def stop(self) -> None:
        """停止事件总线"""
        if not self._running:
            return

        if self.enable_trace:
            logger.info(f"🛑 [EventBus] 正在停止... (队列剩余: {self._event_queue.qsize()})")

        self._running = False

        for _request_id, future in list(self._pending_requests.items()):
            if not future.done():
                future.set_exception(asyncio.CancelledError("EventBus stopped"))
        self._pending_requests.clear()

        await self.flush_critical(timeout=self._stop_drain_timeout)

        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task

        remaining = self._event_queue.qsize()
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
                self._event_queue.task_done()
            except asyncio.QueueEmpty, ValueError:
                break

        if self.enable_trace:
            logger.info(f"🛑 [EventBus] 事件总线已停止 (丢弃 {remaining} 个未处理事件)")
            logger.info(
                f"   📊 统计: 发布 {self._stats['published']}, "
                f"投递 {self._stats['delivered']}, "
                f"过滤 {self._stats['filtered']}, "
                f"错误 {self._stats['errors']}"
            )

    async def flush_critical(self, timeout: float | None = None) -> None:
        """尽量在停机前处理关键事件，避免保存/错误等事件直接丢弃。

        非关键事件先暂存、最后按原序放回队列——不得让排在关键事件之前的
        普通事件把关键事件挡在停机点之外。
        """
        if self._event_queue.empty():
            return
        deadline = asyncio.get_running_loop().time() + (timeout or self._stop_drain_timeout)
        stashed: list[Event] = []
        while not self._event_queue.empty() and asyncio.get_running_loop().time() < deadline:
            try:
                event = self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if event.type in self._critical_events:
                    await self._process_event(event)
                    self._stats["critical_flushed"] += 1
                else:
                    stashed.append(event)
            finally:
                with contextlib.suppress(ValueError):
                    self._event_queue.task_done()
        for event in stashed:
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                break

    async def _event_worker(self) -> None:
        """事件处理工作器"""
        logger.debug("🔄 [EventBus] 工作器线程已启动")

        while self._running:
            try:
                event = await self._event_queue.get()

                if self.enable_trace:
                    logger.debug(
                        f"📬 [EventBus] 从队列取出事件: {event.type.value} (队列剩余: {self._event_queue.qsize()})"
                    )

                await self._process_event(event)
                self._event_queue.task_done()

            except asyncio.CancelledError:
                logger.debug("🛑 [EventBus] 工作器收到取消信号")
                break
            except Exception as e:
                logger.error(f"❌ [EventBus] 事件处理异常: {e}", exc_info=True)
                self._stats["errors"] += 1
                with contextlib.suppress(ValueError):
                    self._event_queue.task_done()

        logger.debug(f"🛑 [EventBus] 工作器线程已停止 (running={self._running})")

    # ==================== 订阅管理 ====================

    def subscribe(
        self,
        event_type: SystemEvent,
        handler: Callable[[Event], Any],
        priority: EventPriority = EventPriority.NORMAL,
        once: bool = False,
        filter_func: Callable[[Event], bool] | None = None,
    ) -> str:
        sub = Subscription(handler, priority, once, filter_func)

        if event_type not in self._subscribers:
            self._subscribers[event_type] = []

        self._subscribers[event_type].append(sub)
        self._subscribers[event_type].sort(key=lambda s: s.priority.value)

        if self.enable_trace:
            filter_info = " (带过滤)" if filter_func else ""
            once_info = " (一次性)" if once else ""
            logger.debug(
                f"📌 [EventBus] 订阅: '{event_type.value}' -> {sub.handler_name}"
                f"{filter_info}{once_info} [优先级: {priority.name}]"
            )

        return sub.id

    def unsubscribe(self, subscription_id: str) -> bool:
        for _event_type, subs in self._subscribers.items():
            for sub in subs:
                if sub.id == subscription_id:
                    subs.remove(sub)
                    if self.enable_trace:
                        logger.debug(
                            f"🔕 [EventBus] 取消订阅: {subscription_id} ({sub.handler_name})"
                        )
                    return True
        return False

    # ==================== 事件发布 ====================

    def publish(self, event: Event, immediate: bool = False) -> None:
        """发布事件"""
        if self.enable_trace:
            data_preview = self._format_data_preview(event.data)
            logger.info(
                f"📢 [EventBus] 事件触发: '{event.type.value}' "
                f"(来源: {event.source}, ID: {event.id}) {data_preview}"
            )

        if immediate:
            asyncio.create_task(self._process_event(event))
        else:
            try:
                self._event_queue.put_nowait(event)
                self._stats["published"] += 1
            except asyncio.QueueFull:
                self._stats["dropped"] += 1
                logger.warning(f"⚠️ [EventBus] 事件队列已满，丢弃事件: {event.type.value}")

    async def request(self, event: Event, timeout: float | None = None) -> Any:
        """发送请求事件并等待响应"""
        request_id = event.id
        future: asyncio.Future = asyncio.Future()
        self._pending_requests[request_id] = future

        try:
            self.publish(event)
            timeout_val = timeout or self._response_timeout
            result = await asyncio.wait_for(future, timeout=timeout_val)
            return result
        except TimeoutError:
            if self.enable_trace:
                logger.warning(f"⏰ [EventBus] 请求超时: {event.type.value}")
            return None
        finally:
            self._pending_requests.pop(request_id, None)

    def respond(self, request_event: Event, result: Any) -> None:
        """响应请求事件"""
        request_id = request_event.id
        future = self._pending_requests.get(request_id)

        if future and not future.done():
            future.set_result(result)
            if self.enable_trace:
                logger.debug(f"✅ [EventBus] 请求响应: {request_event.type.value}")

    async def _process_event(self, event: Event) -> list[Any]:
        if event.type not in self._subscribers:
            if self.enable_trace:
                logger.debug(f"👻 [EventBus] 事件 '{event.type.value}' 无订阅者")
            return []

        subscribers = self._subscribers[event.type]

        if self.enable_trace:
            logger.debug(
                f"🔄 [EventBus] 处理事件 '{event.type.value}' -> {len(subscribers)} 个订阅者"
            )

        results = []
        to_remove = []

        for sub in sorted(subscribers, key=lambda s: s.priority.value):
            if sub.filter_func:
                try:
                    if not sub.filter_func(event):
                        self._stats["filtered"] += 1
                        continue
                except Exception as e:
                    logger.warning(f"⚠️ [EventBus] 过滤器异常 {sub.handler_name}: {e}")
                    continue

            try:
                if self.enable_trace:
                    logger.info(f"   📨 [EventBus] 推送给处理器: {sub.handler_name} 执行")

                start_time = utc_now()
                if inspect.iscoroutinefunction(sub.handler):
                    result = await sub.handler(event)
                else:
                    result = await asyncio.to_thread(sub.handler, event)

                elapsed = (utc_now() - start_time).total_seconds() * 1000

                if self.enable_trace:
                    logger.info(f"   ✅ [EventBus] {sub.handler_name} 执行完成 ({elapsed:.1f}ms)")

                results.append(result)
                self._stats["delivered"] += 1

                if sub.once:
                    to_remove.append(sub)

            except Exception as e:
                self._stats["errors"] += 1
                if self.enable_trace:
                    logger.error(f"   ❌ [EventBus] {sub.handler_name} 执行失败: {e}")

                self.publish(
                    Event(
                        type=SystemEvent.ERROR_OCCURRED,
                        source="eventbus",
                        data={"handler": sub.handler_name, "error": str(e)},
                    )
                )

        for sub in to_remove:
            self._subscribers[event.type].remove(sub)

        return results

    def _format_data_preview(self, data: Any) -> str:
        if data is None:
            return ""
        if isinstance(data, dict):
            if "content" in data:
                content = str(data["content"])
                preview = content[:50] + "..." if len(content) > 50 else content
                return f"[content: {preview}]"
            elif "name" in data:
                return f"[name: {data['name']}]"
        if isinstance(data, str):
            preview = data[:50] + "..." if len(data) > 50 else data
            return f'"{preview}"'
        return f"({type(data).__name__})"

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "queue_size": self._event_queue.qsize(),
            "queue_max_size": self._max_queue_size,
            "subscriber_count": sum(len(subs) for subs in self._subscribers.values()),
        }
