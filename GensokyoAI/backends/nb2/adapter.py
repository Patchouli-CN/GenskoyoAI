"""NoneBot2 适配器的 RuntimeAdapter 实现。

把 NoneBot2（OneBot 11 反向 WS）嵌进 GensokyoAI 的适配器组装体系：
uvicorn 以 asyncio task 形态运行（不阻塞），由 `run_adapters` 统一托管生命周期。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import nonebot
import uvicorn
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from ...runtime.host import RuntimeHost
from ...utils.logger import logger, setup_logging
from .config import resolve_env_file

# uvicorn 的 std logging 不加自己的 handler，全部传播到 root，
# 经项目 LoguruHandler 桥接进 loguru（享受第三方噪音过滤）
_UVICORN_LOG_CONFIG = {"version": 1, "disable_existing_loggers": False}


class Nonebot2Adapter:
    """GensokyoAI RuntimeAdapter：QQ（NoneBot2 + OneBot 11）。"""

    name = "nonebot2"

    def __init__(self, env_file: str | Path | None = None, root_dir: Path | None = None) -> None:
        # env_file 显式指定（向后兼容）；None 时按约定解析：
        # config/nb2/.env 优先，根 .env 兜底（resolve_env_file）
        self._env_file = Path(env_file) if env_file else None
        self._root_dir = root_dir
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None

    async def start(self, host: RuntimeHost) -> None:
        # 先加载 dotenv：NoneBot 配置（HOST/PORT/DRIVER）与适配器 GSK_* 键都在里面
        if self._env_file is not None:
            env_file, is_fallback = self._env_file, False
        else:
            env_file, is_fallback = resolve_env_file(self._root_dir)
        if env_file is not None:
            load_dotenv(env_file)
            if is_fallback:
                logger.warning(
                    "[nb2] 正在使用项目根 .env——建议迁移到适配器私有配置目录 "
                    f"config/nb2/.env（{env_file}）"
                )
            else:
                logger.info(f"[nb2] 已加载配置文件: {env_file}")
            environment = os.environ.get("ENVIRONMENT")
            if environment:
                load_dotenv(
                    env_file.with_name(f"{env_file.name}.{environment}"), override=True
                )
        else:
            logger.info("[nb2] 未找到 config/nb2/.env（或根 .env），使用全部默认配置")
        # nonebot 初始化日志只有前几行受其 log_level 过滤——压到 CRITICAL 让它们闭嘴
        os.environ.setdefault("LOG_LEVEL", "CRITICAL")
        # nonebot 自己的配置（HOST/PORT/DRIVER/ONEBOT_ACCESS_TOKEN）也从同一
        # 文件读：适配器相关配置全部住进 config/nb2/，根 .env 彻底退役
        nonebot.init(_env_file=str(env_file) if env_file is not None else ".env")
        # nonebot 会挂自己的 loguru sink（格式自成一套、与项目重复）：直接全部清掉，
        # 日志统一走 GensokyoAI 体系（nonebot 的 WARNING+ 仍经我们的 sink 显示）
        logger.remove()
        setup_logging(log_file=Path("logs/GENSOKYOAI.log"))

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
