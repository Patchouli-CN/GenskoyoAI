"""适配器配置目录约定：框架只给「家」，不管「家具」。

`config/{adapter_name}/` 是各适配器的私有配置目录：配置格式（env /
yaml / json / toml…）与加载器完全归适配器自己——有自有加载器的适配器
框架让渡加载，框架只做路径约定，零格式耦合（2026-08-02 用户定稿）。
"""

from pathlib import Path


def adapter_config_dir(adapter_name: str, root_dir: Path | None = None) -> Path:
    """适配器私有配置目录（root_dir 为项目根解析基准，默认 cwd）。"""
    return (root_dir or Path.cwd()) / "config" / adapter_name
