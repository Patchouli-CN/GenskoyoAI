"""HTTP/WebSocket Runtime 适配器（aiohttp）——RuntimeAdapter 家族成员。

`http_adapter` 是传输层（create_app 及全部路由/安全逻辑）；本类是装配层：
封装 AppRunner/TCPSite 生命周期，让 web_server 与其它适配器同一形状
（name + start(host) + stop()）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from ...adapters import RuntimeAdapter
from ...runtime.host import RuntimeHost
from ...utils.logger import logger
from .http_adapter import create_app


class WebAdapter(RuntimeAdapter):
    """HTTP/WebSocket Runtime 适配器。

    独立入口（`python -m GensokyoAI.backends.web_server`）host=None 时
    自建 RuntimeService；经 `run_adapters` 组装时复用宿主的 service
    （runtime 包内契约访问，单 service 模型）。
    """

    name = "web"

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        **app_kwargs: Any,
    ) -> None:
        self._root_dir = root_dir
        self._bind_host = host
        self._bind_port = port
        self._app_kwargs = app_kwargs  # 透传 create_app 的安全/限额参数
        self._runner: web.AppRunner | None = None

    async def start(self, host: RuntimeHost | None = None) -> None:
        """启动 HTTP/WebSocket 服务（非阻塞：站点挂为 runner，start 即返回）。"""
        service = host._service if host is not None else None
        app = create_app(self._root_dir, service=service, **self._app_kwargs)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._bind_host, self._bind_port)
        await site.start()
        logger.info(
            f"[web] HTTP/WebSocket Runtime 监听中: http://{self._bind_host}:{self._bind_port}"
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
