"""工具注册中心"""

# GensokyoAI\tools\registry.py

import importlib
from collections.abc import Callable
from pathlib import Path

from ..utils.logger import logger
from .base import ToolDefinition, build_tool_definition, get_tool, list_tools


class ToolRegistry:
    """工具注册中心"""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._load_builtin()

    def _load_builtin(self) -> None:
        """自动发现并加载内置工具"""

        builtin_dir = Path(__file__).parent / "tool_builtin"
        for py_file in builtin_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            module_name = py_file.stem
            try:
                importlib.import_module(f".tool_builtin.{module_name}", package=__package__)
                logger.debug(f"加载内置工具: {module_name}")
            except Exception as e:
                logger.warning(f"加载 {module_name} 失败: {e}")

        self._tools.update(list_tools())

    def register(self, func: Callable, name: str | None = None, parallel_safe: bool = True) -> None:
        """注册工具到本实例注册表（非装饰器方式）。

        实例级、不写进程级全局表：运行时注入的适配器/租户闭包（如
        set_reminder 捕获 agent_id）若入全局表，会被之后新建注册表的
        _load_builtin 吸进无关 Agent（多角色/多租户串台）。仅当 func
        本身就是 @tool 装饰过的内置工具（全局表中同一个函数对象）时
        复用其定义，保留装饰器给的描述。
        """
        tool_name = name or func.__name__
        existing = get_tool(tool_name)
        if existing is not None and existing.func is func:
            self._tools[tool_name] = existing
            return
        self._tools[tool_name] = build_tool_definition(func, name=name, parallel_safe=parallel_safe)

    def get(self, name: str) -> ToolDefinition | None:
        """获取工具（仅查本实例注册表）。

        不回退进程级全局注册表：否则 unregister 形同虚设，且任何一处
        register() 都会把工具泄漏给所有 Actor 的 registry，破坏多角色隔离。
        """
        return self._tools.get(name)

    def list(self) -> list[ToolDefinition]:
        """列出所有工具"""
        return list(self._tools.values())
