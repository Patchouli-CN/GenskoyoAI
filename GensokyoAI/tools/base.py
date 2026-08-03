"""工具基类和装饰器"""

# GensokyoAI\tools\base.py

import inspect
from collections.abc import Callable
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints

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


def _parse_docstring_args(docstring: str) -> dict[str, str]:
    """从 docstring 的 `Args:` 段解析「参数名: 描述」映射（06#7）。

    只认缩进的参数条目；遇到非缩进行（Returns:/Raises:/Example: 等新 section
    标题）即结束。多行描述追加到上一参数。
    """
    if not docstring:
        return {}
    args: dict[str, str] = {}
    in_args = False
    last_name: str | None = None
    for raw_line in docstring.splitlines():
        if raw_line.strip() == "Args:":
            in_args = True
            continue
        if not in_args:
            continue
        if raw_line.strip() and not raw_line[:1].isspace():
            # 新 section 标题（Returns: 等），Args 段结束
            break
        stripped = raw_line.strip()
        if not stripped:
            continue
        if ":" in stripped:
            name, _, desc = stripped.partition(":")
            name = name.strip()
            if name:
                args[name] = desc.strip()
                last_name = name
        elif last_name is not None:
            # 缩进续行：追加到上一参数描述
            args[last_name] = f"{args[last_name]} {stripped}".strip()
    return args


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
    # docstring Args 段 → 参数名→描述（06#7）：模型拿到的不再是空 description
    docstring_args = _parse_docstring_args(func.__doc__ or "")

    # 映射 Python 类型到 JSON Schema
    type_map = {
        str: ToolParameterType.STRING,
        int: ToolParameterType.INTEGER,
        float: ToolParameterType.NUMBER,
        bool: ToolParameterType.BOOLEAN,
        list: ToolParameterType.ARRAY,
        dict: ToolParameterType.OBJECT,
    }

    def resolve_param_type(param_type: Any) -> ToolParameterType:
        """把 typing 泛型解包成基础 JSON Schema 类型（06#6）。

        `list[str]`/`dict[str, int]` → ARRAY/OBJECT；`Optional[X]`/`X | None`
        → X（剥掉 None 成员）。不做此解包时这些类型全退化成 STRING，模型拿到的
        参数 schema 类型错误。
        """
        origin = get_origin(param_type)
        if origin is not None:
            if origin in type_map:
                return type_map[origin]
            args = [arg for arg in get_args(param_type) if arg is not type(None)]
            if args:
                return resolve_param_type(args[0])
            return ToolParameterType.STRING
        return type_map.get(param_type, ToolParameterType.STRING)

    for param_name, param in sig.parameters.items():
        param_type = type_hints.get(param_name, str)
        tool_type = resolve_param_type(param_type)

        parameters[param_name] = ToolParameter(
            name=param_name,
            type=tool_type,
            description=docstring_args.get(param_name, ""),
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
