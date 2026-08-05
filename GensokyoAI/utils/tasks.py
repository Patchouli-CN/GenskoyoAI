"""fire-and-forget 后台任务的强引用工具。

事件循环对 task 只持弱引用——挂在长 await 上的无引用 task 可能被 GC
提前回收（asyncio.create_task 官方文档明确警告）。凡是「发了不管」的任务
一律经 tracked_task 进集合，done_callback 自清（防 GC 也防集合泄漏）。
"""

import asyncio
from collections.abc import Coroutine
from typing import Any


def tracked_task(
    coro: Coroutine[Any, Any, Any],
    store: set[asyncio.Task[Any]],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """create_task + 强引用登记 + done 自清。"""
    task = asyncio.create_task(coro, name=name)
    store.add(task)
    task.add_done_callback(store.discard)
    return task
