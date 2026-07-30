"""NoneBot2 QQ 机器人入口。

用法：
    1. 安装可选依赖：uv sync --extra nb2（或 pip install -e ".[nb2]"）
    2. 复制 tmp/nb2.env.example 为 .env 并按需修改
    3. 启动本机器人：python -m GensokyoAI.backends.nb2
    4. 协议端（NapCat 等）反向 WS 指向 ws://127.0.0.1:8080/onebot/v11/ws

等价于自定义组装：

    from GensokyoAI.adapters import run_adapters
    from GensokyoAI.backends.nb2.adapter import Nonebot2Adapter

    run_adapters(Nonebot2Adapter())
"""

from __future__ import annotations

from ...adapters import run_adapters
from .adapter import Nonebot2Adapter


def main() -> None:
    run_adapters(Nonebot2Adapter())


if __name__ == "__main__":
    main()
