"""工具注册中心"""

# GensokyoAI\tools\registry.py

import importlib
import importlib.util
import pkgutil
from collections.abc import Callable
from pathlib import Path

from ..utils.logger import logger
from .base import ToolDefinition, get_tool, list_tools, tool


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
        """注册工具（非装饰器方式）"""

        # tool() 装饰器返回原函数（用于装饰器场景），真正的 ToolDefinition
        # 被登记进全局注册表，这里回取它，避免误用函数对象。
        tool(name=name, parallel_safe=parallel_safe)(func)
        tool_name = name or func.__name__
        definition = get_tool(tool_name)
        if definition is None:
            raise ValueError(f"工具注册失败: {tool_name}")
        self._tools[tool_name] = definition

    def register_module(self, module_path: Path) -> None:
        """注册模块中的所有工具"""
        spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            logger.debug(f"加载模块: {module_path}")

    def register_package(self, package_path: Path) -> None:
        """注册包中的所有工具"""
        for _, name, _ in pkgutil.iter_modules([str(package_path)]):
            try:
                importlib.import_module(f"tools.builtin.{name}")
            except ImportError as e:
                logger.warning(f"导入模块 {name} 失败: {e}")

    def get(self, name: str) -> ToolDefinition | None:
        """获取工具（仅查本实例注册表）。

        不回退进程级全局注册表：否则 unregister 形同虚设，且任何一处
        register() 都会把工具泄漏给所有 Actor 的 registry，破坏多角色隔离。
        """
        return self._tools.get(name)

    def list(self) -> list[ToolDefinition]:
        """列出所有工具"""
        return list(self._tools.values())

    def get_schemas(self) -> list[dict]:
        """获取所有工具的 OpenAI schema"""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def unregister(self, name: str) -> bool:
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
            return True
        return False
