"""命令处理模块 - 纯抽象，不依赖任何后端"""

# GensokyoAI/commands/__init__.py

from .context import CommandContext
from .decorators import CommandDefinition, command, get_command, list_commands
from .executor import CommandExecutor
from .parser import CommandParser, CommandType, ParsedCommand
from .permission import PermissionLevel
from .result import CommandResult, CommandStatus

__all__ = [
    "CommandParser",
    "CommandType",
    "ParsedCommand",
    "command",
    "CommandExecutor",
    "CommandContext",
    "CommandDefinition",
    "CommandResult",
    "CommandStatus",
    "PermissionLevel",
    "get_command",
    "list_commands",
]
