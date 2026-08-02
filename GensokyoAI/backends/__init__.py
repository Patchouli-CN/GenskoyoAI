"""后端模块

本模块提供内置适配器实现（统一继承 `GensokyoAI.adapters.RuntimeAdapter`）。

RuntimeAdapter
    适配器抽象基类（自 GensokyoAI.adapters 再导出，定义唯一定位）。

ConsoleAdapter
    内置的控制台适配器实现，基于 Rich 库提供美化的终端交互。

    这个实现可作为开发自定义适配器的参考示例：
    - 如何集成 CommandExecutor
    - 如何处理 CommandResult
    - 如何管理提示词上下文
    - 如何处理流式/非流式输出

ConsoleBackendBuilder
    用于链式配置 ConsoleAdapter 的构建器。
"""

# GensokyoAI/backends/__init__.py

from ..adapters import RuntimeAdapter
from .console import ConsoleAdapter, ConsoleBackendBuilder

__all__ = [
    "RuntimeAdapter",
    "ConsoleAdapter",
    "ConsoleBackendBuilder",
]
