"""工具基类和装饰器"""

# GensokyoAI\tools\base.py

import inspect
from collections.abc import Callable
from enum import Enum
from typing import Any, get_type_hints

from msgspec import Struct


class ToolParameterType(Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


class ToolParameter(Struct):
    """工具参数"""

    name: str
    type: ToolParameterType
    description: str = ""
    required: bool = True
    default: Any = None
    items: dict | None = None  # for array type
    properties: dict | None = None  # for object type


class ToolDefinition(Struct):
    """工具定义"""

    name: str
    description: str
    parameters: dict[str, ToolParameter]
    func: Callable
    is_async: bool = False
    # 并行安全：只读工具为 True，可与同批其他工具并发执行；
    # 写状态工具（记忆写入、scene_switch 等）为 False，同一 Actor 内按调用顺序串行，
    # 避免并发修改 Actor 私有状态导致竞态。
    parallel_safe: bool = True

    def to_openai_schema(self, strict: bool = False) -> dict:
        """转换为 OpenAI 工具格式

        Args:
            strict: 是否启用 strict 模式（OpenAI 官方推荐启用，
                    但第三方兼容服务可能不支持）。启用时会添加
                    ``strict: true`` 和 ``additionalProperties: false``。
        """
        properties = {}
        required = []

        for name, param in self.parameters.items():
            prop: dict = {"type": param.type.value, "description": param.description}
            if param.default is not None:
                prop["default"] = param.default
            if param.items:
                prop["items"] = param.items  # type: ignore
            if param.properties:
                prop["properties"] = param.properties  # type: ignore
            properties[name] = prop
            if param.required:
                required.append(name)

        parameters: dict = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

        # strict 模式要求 additionalProperties: false
        if strict:
            parameters["additionalProperties"] = False

        function_def: dict = {
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
        }

        # strict 模式标记
        if strict:
            function_def["strict"] = True

        return {
            "type": "function",
            "function": function_def,
        }


# 全局工具注册表（由 registry 管理）：只盛放内置工具（tool_builtin 模块
# 导入时经 @tool 装饰器写入），供 ToolRegistry._load_builtin 白名单加载。
# 运行时注入的适配器/租户工具**绝不入此表**（registry.register 改为本地
# 构建）——否则闭包会泄漏给之后新建的所有注册表（多角色/多租户串台）。
_TOOL_REGISTRY: dict[str, ToolDefinition] = {}


def build_tool_definition(
    func: Callable,
    *,
    name: str | None = None,
    description: str | None = None,
    parallel_safe: bool = True,
) -> ToolDefinition:
    """从函数签名与文档串构建 ToolDefinition（纯函数，不写任何全局表）。

    schema 生成逻辑的单源：`tool()` 装饰器（内置工具，写全局表）与
    `ToolRegistry.register()`（运行时注入，实例级）都走这里。
    """
    tool_name = name or func.__name__
    tool_desc = description or (func.__doc__ or "").strip()

    # 解析参数
    sig = inspect.signature(func)
    type_hints = get_type_hints(func)
    parameters = {}

    for param_name, param in sig.parameters.items():
        param_type = type_hints.get(param_name, str)

        # 映射 Python 类型到 JSON Schema
        type_map = {
            str: ToolParameterType.STRING,
            int: ToolParameterType.INTEGER,
            float: ToolParameterType.NUMBER,
            bool: ToolParameterType.BOOLEAN,
            list: ToolParameterType.ARRAY,
            dict: ToolParameterType.OBJECT,
        }
        tool_type = type_map.get(param_type, ToolParameterType.STRING)

        parameters[param_name] = ToolParameter(
            name=param_name,
            type=tool_type,
            required=param.default is inspect.Parameter.empty,
            default=None if param.default is inspect.Parameter.empty else param.default,
        )

    # 检查是否是异步函数
    is_async = inspect.iscoroutinefunction(func)

    return ToolDefinition(
        name=tool_name,
        description=tool_desc,
        parameters=parameters,
        func=func,
        is_async=is_async,
        parallel_safe=parallel_safe,
    )


def tool(
    name: str | None = None,
    description: str | None = None,
    parallel_safe: bool = True,
) -> Callable:
    """工具装饰器

    Args:
        parallel_safe: 是否可与同批其他工具并发执行。写状态工具（记忆写入、
            scene_switch 等）应设为 False，由 ToolExecutor 按调用顺序串行，避免竞态。
    """

    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        _TOOL_REGISTRY[tool_name] = build_tool_definition(
            func, name=name, description=description, parallel_safe=parallel_safe
        )
        return func

    return decorator


def get_tool(name: str) -> ToolDefinition | None:
    """获取工具定义"""
    return _TOOL_REGISTRY.get(name)


def list_tools() -> dict[str, ToolDefinition]:
    """列出所有工具"""
    return _TOOL_REGISTRY.copy()
