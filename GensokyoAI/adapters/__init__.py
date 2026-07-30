"""适配器约定：第三方接入 GensokyoAI 的公开组装面。

一个适配器 = 实现 `RuntimeAdapter` 协议的类（QQ bot、Discord bot、Web UI……）。
适配器拿到进程内 `RuntimeHost`，自行决定如何驱动多租户会话；组装只需：

```python
from GensokyoAI.adapters import run_adapters
from gskai_nb2 import Nonebot2Adapter

run_adapters(Nonebot2Adapter())  # 想挂几个挂几个
```

`run_adapters` 负责创建宿主、启动所有适配器、Ctrl+C 时逆序停止并保存全部租户会话。
适配器自己的事件循环任务在 `start()` 内以 asyncio task 形式挂载（不要阻塞 start）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from ..runtime.host import RuntimeHost
from ..utils.logger import logger

__all__ = ["RuntimeAdapter", "RuntimeHost", "run_adapters", "serve_adapters"]


class RuntimeAdapter(Protocol):
    """适配器协议：实现两个方法即可接入，无需继承。"""

    name: str  # 适配器名（日志用）

    async def start(self, host: RuntimeHost) -> None:
        """启动适配器。在此建立平台连接并把后台任务挂为 asyncio task（勿阻塞）。"""
        ...

    async def stop(self) -> None:
        """停止适配器并释放平台连接。必须可重入、不得抛出。"""
        ...


async def serve_adapters(*adapters: RuntimeAdapter, root_dir: Path | None = None) -> None:
    """异步形态：创建宿主并运行所有适配器，直到被取消（供自定义入口组装）。"""
    host = RuntimeHost(root_dir)
    started: list[RuntimeAdapter] = []
    try:
        for adapter in adapters:
            await adapter.start(host)
            started.append(adapter)
            logger.info(f"[adapters] 适配器已启动: {getattr(adapter, 'name', type(adapter).__name__)}")
        await asyncio.Event().wait()  # 永久驻留，直至被取消
    finally:
        for adapter in reversed(started):
            try:
                await adapter.stop()
            except Exception as error:
                logger.warning(f"[adapters] 适配器停止异常（忽略）: {error}")
        await host.close()


def run_adapters(*adapters: RuntimeAdapter, root_dir: Path | None = None) -> None:
    """同步入口：组装并运行全部适配器，Ctrl+C 优雅退出（保存所有租户会话）。"""
    try:
        asyncio.run(serve_adapters(*adapters, root_dir=root_dir))
    except KeyboardInterrupt:
        logger.info("[adapters] 收到中断信号，已全部退出")
