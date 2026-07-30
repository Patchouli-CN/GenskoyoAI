"""NoneBot2 QQ 机器人入口。

用法：
    1. 安装可选依赖：uv sync --extra nb2（或 pip install -e ".[nb2]"）
    2. 复制 tmp/nb2.env.example 为 .env 并按需修改
    3. 启动本机器人：python -m GensokyoAI.backends.nb2
    4. 协议端（NapCat 等）反向 WS 指向 ws://127.0.0.1:8080/onebot/v11/ws
"""

from __future__ import annotations

import os
from pathlib import Path

import nonebot
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from ...utils.logger import logger, setup_logging


def main() -> None:
    # 先把 .env 加载进环境变量，适配器的 GSK_* 配置键才能被 plugin 读取；
    # NoneBot 自身配置（HOST/PORT/DRIVER 等）由 nonebot.init() 自行解析。
    load_dotenv()
    environment = os.environ.get("ENVIRONMENT")
    if environment:
        load_dotenv(f".env.{environment}", override=True)
    # 适配器是终端盯梢场景：强制打开控制台日志（local.yaml 为 CLI 的 TUI 关了它，
    # 否则首个租户初始化时会按配置把控制台 sink 撤掉）。env 已显式设置时尊重原值。
    os.environ.setdefault("GENSOKYOAI_LOG_CONSOLE", "true")
    nonebot.init()
    # nonebot.init() 会重排 loguru（移除既有 sink），必须在此之后才挂项目自己的
    # 日志（含第三方噪音过滤），终端与文件看到的才是 GensokyoAI 的格式；
    # nonebot 自身日志的级别由 .env 的 LOG_LEVEL 控制（建议 WARNING）。
    # 首个租户初始化加载应用配置时，会按 config/local.yaml 重新应用日志配置。
    setup_logging(log_file=Path("logs/GENSOKYOAI.log"))
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("GensokyoAI.backends.nb2.plugin")
    try:
        nonebot.run()
    except KeyboardInterrupt:
        logger.info("[nb2] 适配器已退出")


if __name__ == "__main__":
    main()
