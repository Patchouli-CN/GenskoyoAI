"""后台任务管理器 - 基于队列的事件驱动模式"""

# GensokyoAI\background\manager.py

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..utils.logger import logger
from .types import (
    BackgroundTask,
    PersistenceTaskData,
    TaskPriority,
    TaskResult,
    TaskType,
)
from .workers import PersistenceWorker
from .workers.base import BaseWorker

if TYPE_CHECKING:
    from ..core.events import EventBus


class TaskContext:
    """任务上下文管理器，自动处理 task_done"""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue
        self._task = None

    async def __aenter__(self):
        self._task = await self._queue.get()
        return self._task

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._queue.task_done()


class BackgroundManager:
    """后台任务管理器

    职责：
    - 管理任务队列
    - 委托任务给对应的工作器
    - 控制并发数量
    - 不处理具体业务逻辑
    """

    def __init__(
        self,
        max_workers: int = 3,
        max_queue_size: int = 100,
        event_bus: EventBus | None = None,
        label: str | None = None,
    ):
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size
        # 日志租户后缀（Runtime 多租户下区分各租户的同款关闭日志）
        self._log_suffix = f" (租户: {label})" if label else ""

        self._task_queue: asyncio.Queue[BackgroundTask] = asyncio.Queue(maxsize=max_queue_size)

        self._workers: dict[TaskType, BaseWorker] = {}

        self._running = False
        self._accepting_tasks = False
        self._stopping = False
        self._worker_tasks: list[asyncio.Task] = []
        self._result_callbacks: list[Callable[[TaskResult], Awaitable[None]]] = []
        self._event_bus = event_bus

        self._stats = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "timeout": 0,
            "dropped": 0,
        }
        self._stats_lock = asyncio.Lock()

    # ==================== 工作器注册 ====================

    def register_worker(self, task_type: TaskType, worker: BaseWorker) -> BackgroundManager:
        """注册工作器"""
        self._workers[task_type] = worker
        logger.debug(f"注册工作器: {task_type.name}")
        return self

    def register_persistence_worker(self, worker: PersistenceWorker) -> BackgroundManager:
        """注册持久化工作器"""
        return self.register_worker(TaskType.PERSISTENCE, worker)

    def set_event_bus(self, event_bus: EventBus) -> None:
        """注入事件总线。"""
        self._event_bus = event_bus

    # ==================== 回调注册 ====================

    def on_complete(self, callback: Callable[[TaskResult], Awaitable[None]]) -> BackgroundManager:
        """注册完成回调"""
        self._result_callbacks.append(callback)
        return self

    # ==================== 任务提交 ====================

    def submit(self, task: BackgroundTask) -> bool:
        """提交任务到队列"""
        # 计数直接同步自增：事件循环单线程，put_nowait/+= 之间无让出，本就原子
        if not self._accepting_tasks:
            self._stats["dropped"] += 1
            logger.warning(f"后台管理器已停止接收任务，拒绝任务: {task.name}")
            return False

        try:
            self._task_queue.put_nowait(task)
            self._stats["submitted"] += 1
            logger.trace(f"提交任务: {task.name} (优先级: {task.priority.name})")
            return True
        except asyncio.QueueFull:
            self._stats["dropped"] += 1
            logger.warning(f"任务队列已满 ({self.max_queue_size})，丢弃任务: {task.name}")
            return False

    def submit_persistence_task(
        self,
        operation: str,
        data: dict,
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout: float = 10.0,
    ) -> bool:
        """提交持久化任务"""
        task = BackgroundTask(
            type=TaskType.PERSISTENCE,
            priority=priority,
            name=f"persist_{operation}",
            data=PersistenceTaskData(
                operation=operation,
                data=data,
            ),
            timeout=timeout,
        )
        return self.submit(task)

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        """启动管理器"""
        if self._running:
            return

        self._running = True
        self._accepting_tasks = True
        self._stopping = False

        for i in range(self.max_workers):
            task = asyncio.create_task(self._worker_loop(i))
            self._worker_tasks.append(task)

        logger.info(f"后台管理器已启动 ({self.max_workers} 个工作器)")

    async def stop(self, wait: bool = True) -> None:
        """停止管理器

        停止分两步：先停止接收新任务，再等待已有队列 drain。
        注意不要在 join 前把 _running 置 False，否则 worker 会提前退出，队列无法真正清空。
        """
        if not self._running:
            self._accepting_tasks = False
            return

        self._accepting_tasks = False
        self._stopping = True

        if wait:
            timeout = 5.0
            try:
                await asyncio.wait_for(self._task_queue.join(), timeout=timeout)
            except TimeoutError:
                logger.warning(f"等待队列清空超时，剩余 {self._task_queue.qsize()} 个任务将被丢弃")

        self._running = False

        for task in self._worker_tasks:
            task.cancel()

        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

        async with self._stats_lock:
            stats = self._stats.copy()

        logger.info(
            f"后台管理器已停止{self._log_suffix} "
            f"(提交: {stats['submitted']}, "
            f"完成: {stats['completed']}, "
            f"失败: {stats['failed']}, "
            f"超时: {stats['timeout']}, "
            f"丢弃: {stats['dropped']})"
        )

    def _publish_worker_event(
        self,
        status: str,
        worker_id: int,
        *,
        task: BackgroundTask | None = None,
        error: Exception | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        from ..core.events import Event, SystemEvent
        from ..runtime.event_contract import sanitize_event_payload

        if status == "started":
            event_type = SystemEvent.BACKGROUND_WORKER_STARTED
        elif status == "idle":
            event_type = SystemEvent.BACKGROUND_WORKER_IDLE
        else:
            event_type = SystemEvent.BACKGROUND_WORKER_FAILED

        data: dict = {
            "worker_id": worker_id,
            "queue_size": self._task_queue.qsize(),
        }
        if task is not None:
            data.update(
                {
                    "task_id": task.id,
                    "task_name": task.name,
                    "task_type": task.type.name,
                }
            )
        if error is not None:
            data.update({"error": str(error), "error_type": type(error).__name__})

        self._event_bus.publish(
            Event(
                type=event_type,
                source="background_manager",
                data=sanitize_event_payload(data),
            )
        )

    # ==================== 工作循环 ====================

    async def _worker_loop(self, worker_id: int) -> None:
        """工作器循环 - 使用上下文管理器自动处理 task_done"""
        logger.debug(f"工作器 {worker_id} 已启动")
        self._publish_worker_event("started", worker_id)

        while self._running:
            try:
                async with TaskContext(self._task_queue) as task:
                    worker = self._workers.get(task.type)
                    if worker is None:
                        logger.warning(f"未找到工作器: {task.type.name}")
                        continue

                    try:
                        result = await worker.process(task)
                        await self._update_stats(result)

                        for callback in self._result_callbacks:
                            try:
                                await callback(result)
                            except Exception as e:
                                logger.warning(f"回调执行失败: {e}")

                        if self._task_queue.empty():
                            self._publish_worker_event("idle", worker_id)

                    except asyncio.CancelledError:
                        logger.debug(f"工作器 {worker_id} 任务被取消{self._log_suffix}")
                        raise
                    except Exception as e:
                        logger.error(f"任务执行异常{self._log_suffix}: {e}")
                        self._publish_worker_event("failed", worker_id, task=task, error=e)

            except asyncio.CancelledError:
                logger.debug(f"工作器 {worker_id} 已取消{self._log_suffix}")
                break
            except TimeoutError:
                continue
            except Exception as e:
                logger.error(f"工作器 {worker_id} 发生未预期异常{self._log_suffix}: {e}")
                self._publish_worker_event("failed", worker_id, error=e)
                continue

        logger.debug(f"工作器 {worker_id} 已停止{self._log_suffix}")

    async def _update_stats(self, result: TaskResult) -> None:
        """更新统计信息"""
        async with self._stats_lock:
            self._stats["completed"] += 1
            if not result.success:
                self._stats["failed"] += 1
                if result.error == "timeout":
                    self._stats["timeout"] += 1

    # ==================== 状态查询 ====================

    @property
    def queue_size(self) -> int:
        """当前队列大小"""
        return self._task_queue.qsize()

    @property
    def is_accepting_tasks(self) -> bool:
        """是否仍接收新任务"""
        return self._accepting_tasks

    @property
    def is_stopping(self) -> bool:
        """是否正在停止"""
        return self._stopping

    @property
    def stats(self) -> dict:
        """获取统计信息"""
        return self._stats.copy()
