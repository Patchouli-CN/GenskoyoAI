# GensokyoAI/commands/decorators.py
"""命令装饰器 - 直接使用统一的 CommandType"""

import inspect
from collections.abc import Callable
from typing import get_type_hints

from ..utils.logger import logger
from .parser import CommandType  # 直接导入
from .permission import PermissionLevel

# 全局命令注册表
_COMMAND_REGISTRY: dict[str, CommandDefinition] = {}


class CommandDefinition:
    """命令定义"""

    def __init__(
        self,
        name: str,
        handler: Callable,
        cmd_type: CommandType = CommandType.CUSTOM,
        aliases: list[str] | None = None,
        description: str = "",
        usage: str = "",
        permission: PermissionLevel = PermissionLevel.OWNER,
    ):
        self.name = name
        self.handler = handler
        self.type = cmd_type
        self.aliases = aliases or []
        self.description = description
        self.permission = permission
        self._sig = inspect.signature(handler)
        self._type_hints = get_type_hints(handler)
        self._is_async = inspect.iscoroutinefunction(handler)
        self.usage = usage or self._generate_usage(handler)

    def _generate_usage(self, handler: Callable) -> str:
        params = []
        for name, param in self._sig.parameters.items():
            if name in ("cmd", "ctx"):
                continue
            if param.default is not inspect.Parameter.empty:
                params.append(f"[{name}={param.default}]")
            else:
                params.append(f"<{name}>")
        return f"/{self.name} " + " ".join(params) if params else f"/{self.name}"

    def parse_args(self, content: str) -> dict:
        args = {}
        param_names = [p for p in self._sig.parameters if p not in ("cmd", "ctx")]

        if not param_names:
            return args

        parts = content.strip().split()

        for i, name in enumerate(param_names):
            if i < len(parts):
                param = self._sig.parameters[name]
                hint = self._type_hints.get(name, str)

                try:
                    if hint is bool:
                        args[name] = parts[i].lower() in ("true", "1", "yes", "on")
                    elif hint is int:
                        args[name] = int(parts[i])
                    elif hint is float:
                        args[name] = float(parts[i])
                    else:
                        args[name] = parts[i]
                except ValueError:
                    args[name] = (
                        param.default if param.default is not inspect.Parameter.empty else None
                    )
            else:
                param = self._sig.parameters[name]
                if param.default is not inspect.Parameter.empty:
                    args[name] = param.default
                else:
                    args[name] = None

        return args

    @property
    def all_names(self) -> list[str]:
        return [self.name] + self.aliases


def command(
    name: str | None = None,
    cmd_type: CommandType = CommandType.CUSTOM,
    aliases: list[str] | None = None,
    description: str = "",
    usage: str = "",
    permission: PermissionLevel = PermissionLevel.OWNER,
    registry: dict[str, CommandDefinition] | None = None,
):
    """命令装饰器

    permission：所需权限级，默认 OWNER（未声明即仅主人可用，开放需显式降级）。
    registry：自定义注册表（默认全局注册表）——适配器用本地注册表可避免
    与全局注册表里的同名命令互相覆盖。
    """

    def decorator(func: Callable) -> Callable:
        cmd_name = name or func.__name__.replace("cmd_", "")

        cmd_def = CommandDefinition(
            name=cmd_name,
            handler=func,
            cmd_type=cmd_type,
            aliases=aliases,
            description=description,
            usage=usage,
            permission=permission,
        )

        target = _COMMAND_REGISTRY if registry is None else registry
        for n in cmd_def.all_names:
            target[n.lower()] = cmd_def
            logger.debug(f"注册命令: {n} (类型: {cmd_type.name}, 权限: {permission.name})")

        return func

    return decorator


def get_command(name: str) -> CommandDefinition | None:
    return _COMMAND_REGISTRY.get(name.lower())


def list_commands(cmd_type: CommandType | None = None) -> list[CommandDefinition]:
    seen = set()
    result = []
    for cmd in _COMMAND_REGISTRY.values():
        if cmd.name not in seen:
            seen.add(cmd.name)
            if cmd_type is None or cmd.type == cmd_type:
                result.append(cmd)
    return result
