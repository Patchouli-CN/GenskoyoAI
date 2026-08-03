"""NoneBot2 适配器的 RuntimeAdapter 实现。

把 NoneBot2（OneBot 11 反向 WS）嵌进 GensokyoAI 的适配器组装体系：
uvicorn 以 asyncio task 形态运行（不阻塞），由 `run_adapters` 统一托管生命周期。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

import nonebot
import uvicorn
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.log import logger_id

from ...adapters import RuntimeAdapter
from ...runtime.host import RuntimeHost
from ...utils.logger import logger, setup_logging
from .config import resolve_env_file, seed_local_env

# uvicorn 的 std logging 不加自己的 handler，全部传播到 root，
# 经项目 LoguruHandler 桥接进 loguru（享受第三方噪音过滤）
_UVICORN_LOG_CONFIG = {"version": 1, "disable_existing_loggers": False}


class Nonebot2Adapter(RuntimeAdapter):
    """GensokyoAI RuntimeAdapter：QQ（NoneBot2 + OneBot 11）。"""

    name = "nonebot2"

    def __init__(self, env_file: str | Path | None = None, root_dir: Path | None = None) -> None:
        # env_file 显式指定（向后兼容）；None 时按约定解析：
        # config/nb2/.env 优先，根 .env 兜底（resolve_env_file）
        self._env_file = Path(env_file) if env_file else None
        self._root_dir = root_dir
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None

    async def start(self, host: RuntimeHost | None = None) -> None:
        if host is None:
            raise RuntimeError("Nonebot2Adapter 需要 RuntimeHost（经 run_adapters 组装传入）")
        # 先解析并加载 dotenv——此阶段不打日志：nonebot/默认 sink 还挂着，
        # 我们的日志会和他们的一前一后双格式重复（用户实机反馈）
        if self._env_file is not None:
            env_file, is_fallback = self._env_file, False
        else:
            env_file, is_fallback = resolve_env_file(self._root_dir)
        if env_file is None:
            # 首次运行：从模板播种 config/nb2/local.env（只播种一次）
            env_file = seed_local_env(self._root_dir)
        load_dotenv(env_file)
        environment = os.environ.get("ENVIRONMENT")
        if environment:
            load_dotenv(env_file.with_name(f"{env_file.name}.{environment}"), override=True)
        # 移除 NoneBot 默认日志处理器（官方姿势：按 logger_id 精确摘除，
        # 不动我们自己的 sink，也不需要 LOG_LEVEL=CRITICAL 环境变量压制）；
        # init 前后各摘一次，防其 init 时重新挂载
        with contextlib.suppress(ValueError):
            logger.remove(logger_id)
        # nonebot 自己的配置（HOST/PORT/DRIVER/ONEBOT_ACCESS_TOKEN）也从同一文件读
        nonebot.init(_env_file=str(env_file))
        with contextlib.suppress(ValueError):
            logger.remove(logger_id)
        try:
            setup_logging(log_file=Path("logs/GENSOKYOAI.log"))
        except Exception:
            logger.add(sys.stderr)  # 保底：配置失败也不能让进程变哑巴
            raise
        # 现在才是说话的时候（单一格式）
        if is_fallback:
            logger.warning(
                "[nb2] 正在使用项目根 .env——建议迁移到适配器私有配置目录 "
                "config/nb2/（local.env 或 .env）"
            )
        logger.info(f"[nb2] 已加载配置文件: {env_file}")

        driver = nonebot.get_driver()
        driver.register_adapter(OneBotV11Adapter)
        # load_plugin 返回 Plugin 对象（模块在 .module 上）；先加载再绑定宿主
        # （直接 import 插件模块会被 nonebot 拒绝登记）
        plugin = nonebot.load_plugin("GensokyoAI.backends.nb2.plugin")
        bind_host = getattr(plugin.module, "bind_host", None) if plugin is not None else None
        if bind_host is None:
            raise RuntimeError("nb2 插件加载失败或缺少 bind_host")
        bind_host(host)

        # uvicorn 以任务形态嵌入当前事件循环（不用阻塞式 nonebot.run()）
        server_app = getattr(driver, "server_app", None)
        if server_app is None:
            raise RuntimeError("NoneBot 驱动缺少 server_app（需要 fastapi 系驱动）")
        config = uvicorn.Config(
            server_app,
            host=str(driver.config.host),
            port=int(driver.config.port),
            log_config=_UVICORN_LOG_CONFIG,
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        # bind 失败/启动异常落在 task future 里，默认 "Task exception was never retrieved"；
        # 显式收集并告警，避免「适配器已启动」假成功
        self._server_task.add_done_callback(
            lambda task: logger.error(f"[nb2] uvicorn 服务异常退出: {task.exception()}")
            if task.exception()
            else None
        )
        logger.info(
            f"[nb2] OneBot 反向 WS 监听中: ws://{driver.config.host}:{driver.config.port}/onebot/v11/ws"
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._server_task
        self._server = None
        self._server_task = None
