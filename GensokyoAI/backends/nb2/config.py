"""NoneBot2 适配器配置（环境变量驱动）。

GensokyoAI 的 config/local.yaml 不接受未知顶层键，因此适配器配置全部走
环境变量；启动入口（__main__）会先用 python-dotenv 加载 .env，
所以这些键与 NoneBot 自身配置（HOST/PORT/DRIVER 等）写在同一个 .env 即可。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "是"}


@dataclass(frozen=True)
class Nb2Config:
    """适配器运行配置；字段默认值即开箱默认值。"""

    character: str = "KirisameMarisa"
    data_dir: Path = Path("nb2_data")
    root_dir: Path | None = None  # GensokyoAI 项目根（characters/config 解析基准）；None=cwd
    group_whitelist: frozenset[int] = frozenset()  # 空 = 响应所有群
    initiative: bool = True  # 角色主动发言（事件订阅队列进程内投递到群/私聊）

    @classmethod
    def from_env(cls, get: Callable[[str], str | None] = os.environ.get) -> Nb2Config:
        """从环境变量读取配置；`get` 可注入 dict.get 便于测试。"""
        whitelist_raw = (get("GSK_NB2_GROUP_WHITELIST") or "").replace("，", ",")
        whitelist = frozenset(
            int(part)
            for part in (piece.strip() for piece in whitelist_raw.split(","))
            if part.isdigit()
        )
        root_raw = (get("GSK_NB2_ROOT_DIR") or "").strip()
        return cls(
            character=(get("GSK_NB2_CHARACTER") or cls.character),
            data_dir=Path((get("GSK_NB2_DATA_DIR") or "").strip() or cls.data_dir),
            root_dir=Path(root_raw) if root_raw else None,
            group_whitelist=whitelist,
            initiative=_parse_bool(get("GSK_NB2_INITIATIVE"), cls.initiative),
        )
