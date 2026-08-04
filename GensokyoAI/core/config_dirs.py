"""适配器配置目录约定：框架只给「家」，不管「家具」。

`config/{adapter_name}/` 是各适配器的私有配置目录：配置格式（env /
yaml / json / toml…）与加载器完全归适配器自己——有自有加载器的适配器
框架让渡加载，框架只做路径约定，零格式耦合（2026-08-02 用户定稿）。

另含框架本地配置的首次运行播种（ensure_local_config）：新人一把跑不起来
不如直接生成模板让他改——只播种一次，用户改过的配置绝不覆盖。
"""

import shutil
from pathlib import Path

from ..utils.logger import logger
from .release_resources import resolve_resource_path


def adapter_config_dir(adapter_name: str, root_dir: Path | None = None) -> Path:
    """适配器私有配置目录（root_dir 为项目根解析基准，默认 cwd）。"""
    return (root_dir or Path.cwd()) / "config" / adapter_name


def ensure_local_config(root_dir: Path | None = None) -> tuple[Path, bool]:
    """框架本地配置（config/local.yaml）不存在时从发行模板播种一份。

    返回 (配置路径, 是否本次新生成)。模板缺失时只返回路径不报错
    （加载层会按缺省继续）；已存在则原样返回、绝不覆盖。
    """
    base = root_dir or Path.cwd()
    local_path = base / "config" / "local.yaml"
    if local_path.exists():
        return local_path, False
    template = resolve_resource_path(base, "tmp", "template-conf.yaml")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if template.exists():
        shutil.copyfile(template, local_path)
        logger.info(f"首次运行已生成本地配置: {local_path}（请修改它，不要改 tmp/ 模板）")
        return local_path, True
    return local_path, False
