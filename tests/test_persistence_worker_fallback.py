"""PersistenceWorker 兜底异常路径回归：task.data 非法时必须返回失败 TaskResult
（不得抛 UnboundLocalError——否则完成回调丢失、_save_pending 永久卡死）。
"""

import asyncio
import unittest

from GensokyoAI.background.types import BackgroundTask, TaskPriority, TaskType
from GensokyoAI.background.workers.persistence_worker import PersistenceWorker


class PersistenceWorkerFallbackTests(unittest.TestCase):
    def test_invalid_task_data_returns_failure_result(self):
        worker = PersistenceWorker(persistence=None)  # 不会触达 persistence
        task = BackgroundTask(
            type=TaskType.PERSISTENCE,
            priority=TaskPriority.LOW,
            name="persist_save_messages",
            data=object(),  # 非法数据（非 PersistenceTaskData）
        )
        result = asyncio.run(worker.process(task))
        self.assertFalse(result.success)
        self.assertIn("Invalid task data type", result.error)
        self.assertIsNone(result.result["operation"])


if __name__ == "__main__":
    unittest.main()
