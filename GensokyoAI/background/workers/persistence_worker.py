"""持久化工作器"""

# GensokyoAI\background\workers\persistence_worker.py

import asyncio
import time
from typing import TYPE_CHECKING

from ...utils.logger import logger
from ..types import BackgroundTask, PersistenceTaskData, TaskResult
from .base import BaseWorker

if TYPE_CHECKING:
    from ...session.persistence import SessionPersistence


class PersistenceWorker(BaseWorker):
    """持久化工作器 - 负责异步文件 I/O"""

    def __init__(self, persistence: SessionPersistence):
        self.persistence = persistence

    async def process(self, task: BackgroundTask) -> TaskResult:
        """处理持久化任务"""
        start_time = time.time()

        try:
            if not isinstance(task.data, PersistenceTaskData):
                raise ValueError(f"Invalid task data type: {type(task.data)}")

            data: PersistenceTaskData = task.data

            try:
                async with asyncio.timeout(task.timeout):
                    if data.operation == "save_session":
                        await self.persistence.save_session_async(data.data)
                    elif data.operation == "save_messages":
                        session_id = data.data.get("session_id")
                        messages = data.data.get("messages", [])
                        if session_id is None:
                            raise ValueError("Missing session_id in save_messages task")
                        await self.persistence.async_save_message(session_id, messages)
                    else:
                        raise ValueError(f"Unknown operation: {data.operation}")
            except TimeoutError:
                duration_ms = (time.time() - start_time) * 1000
                logger.warning(f"⏱️ 持久化超时 ({task.timeout}s)")
                return TaskResult(
                    task_id=task.id,
                    success=False,
                    error="timeout",
                    result={"operation": data.operation},  # 失败也带操作名，供协调器识别
                    duration_ms=duration_ms,
                )

            duration_ms = (time.time() - start_time) * 1000

            return TaskResult(
                task_id=task.id,
                success=True,
                result={"operation": data.operation},
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"❌ 持久化失败: {e}")
            return TaskResult(
                task_id=task.id,
                success=False,
                error=str(e),
                result={"operation": data.operation},  # 失败也带操作名，供协调器识别
                duration_ms=duration_ms,
            )
