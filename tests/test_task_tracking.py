"""tracked_task 强引用范式（04#7）：fire-and-forget 任务防 GC 回收。

事件循环对 task 只持弱引用——挂在长 await 上的无引用 task 可能被 GC
提前回收（asyncio.create_task 官方文档明确警告）。全仓规则：
raw `asyncio.create_task(...)` 只许出现在赋值/return 形式（调用方自持
强引用），「发了不管」的一律 tracked_task 进集合。
"""

import asyncio
import unittest
from pathlib import Path

from GensokyoAI.utils.tasks import tracked_task

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "GensokyoAI"


class TrackedTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_and_self_cleans(self):
        store: set[asyncio.Task] = set()

        async def noop():
            await asyncio.sleep(0)

        task = tracked_task(noop(), store)
        self.assertIn(task, store)
        await task
        await asyncio.sleep(0)  # done_callback 跑一拍
        self.assertNotIn(task, store)

    async def test_source_scan_no_untracked_create_task(self):
        """全仓源码扫描：raw create_task 必须是赋值/return 形式（强引用自持），
        否则一律走 tracked_task。"""
        violations = []
        for path in _PACKAGE_ROOT.rglob("*.py"):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "asyncio.create_task(" not in line:
                    continue
                if (
                    "= asyncio.create_task" in line
                    or "= (asyncio.create_task" in line  # 元组赋值入集合，如 host 事件泵
                    or "tracked_task" in line
                    or "return" in line
                ):
                    continue
                violations.append(f"{path.relative_to(_PACKAGE_ROOT)}:{lineno}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
