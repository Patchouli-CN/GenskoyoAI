"""NoneBot2 QQ 机器人入口。

用法：
    1. 安装可选依赖：uv sync --extra nb2（或 pip install -e ".[nb2]"）
    2. 复制 tmp/nb2.env.example 为 .env 并按需修改
    3. 先启动 Runtime：python -m GensokyoAI.backends.web_server
    4. 启动本机器人：python -m GensokyoAI.backends.nb2
    5. 协议端（NapCat 等）反向 WS 指向 ws://127.0.0.1:8080/onebot/v11/ws
"""

from __future__ import annotations

import os

import nonebot
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


def main() -> None:
    # 先把 .env 加载进环境变量，适配器的 GSK_* 配置键才能被 plugin 读取；
    # NoneBot 自身配置（HOST/PORT/DRIVER 等）由 nonebot.init() 自行解析。
    load_dotenv()
    environment = os.environ.get("ENVIRONMENT")
    if environment:
        load_dotenv(f".env.{environment}", override=True)
    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("GensokyoAI.backends.nb2.plugin")
    nonebot.run()


if __name__ == "__main__":
    main()
