"""适配器约定：第三方接入 GensokyoAI 的公开组装面。

一个适配器 = 继承 `RuntimeAdapter` 基类的类（QQ bot、Discord bot、Web UI……）。
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
from abc import ABC, abstractmethod
from pathlib import Path

from ..core.config_env import ensure_local_config
from ..runtime.host import RuntimeHost
from ..utils.logger import logger

__all__ = ["RuntimeAdapter", "RuntimeHost", "run_adapters", "serve_adapters"]


class RuntimeAdapter(ABC):
    """适配器基类（唯一基类概念，2026-08-02 用户定稿：废弃 BaseBackend）。

    契约：`name` + `start(host)` + `stop()`。经 `run_adapters` 组装的
    适配器会收到进程内 RuntimeHost；独立入口的适配器传 None，自行装配依赖。
    """

    name: str = "adapter"  # 适配器名（日志用）

    @abstractmethod
    async def start(self, host: RuntimeHost | None = None) -> None:
        """启动适配器。在此建立平台连接并把后台任务挂为 asyncio task（勿阻塞）。"""
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """停止适配器并释放平台连接。必须可重入、不得抛出。"""
        raise NotImplementedError


async def serve_adapters(*adapters: RuntimeAdapter, root_dir: Path | None = None) -> None:
    """异步形态：创建宿主并运行所有适配器，直到被取消（供自定义入口组装）。"""
    # 首次运行播种框架本地配置（config/local.yaml；只播种一次，绝不覆盖）
    ensure_local_config(root_dir)
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
