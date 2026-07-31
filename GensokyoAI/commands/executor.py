# GensokyoAI/commands/executor.py
"""命令执行器 - 无需类型转换"""

from ..utils.logger import logger
from .context import CommandContext
from .decorators import CommandDefinition, get_command, list_commands
from .parser import CommandParser, CommandType, ParsedCommand
from .result import CommandResult, CommandStatus


class CommandExecutor:
    """命令执行器"""

    def __init__(
        self,
        mode: str = "smart",
        registry: dict[str, CommandDefinition] | None = None,
    ):
        self.parser = CommandParser(mode=mode)
        # 本地注册表（适配器隔离用）；None = 框架全局注册表
        self._registry = registry
        self._sync_parser_tags()

    def _iter_commands(self) -> list[CommandDefinition]:
        if self._registry is None:
            return list_commands()
        seen: set[str] = set()
        result = []
        for cmd in self._registry.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                result.append(cmd)
        return result

    def _get_command(self, name: str) -> CommandDefinition | None:
        if self._registry is not None:
            return self._registry.get(name.lower())
        return get_command(name)

    def _sync_parser_tags(self) -> None:
        """同步命令定义到解析器（直接传递，无需转换）"""
        for cmd in self._iter_commands():
            self.parser.register_tag(cmd.name, cmd.aliases, cmd.type, cmd.description)
            self.parser.register_prefix(cmd.name, cmd.aliases, cmd.type, cmd.description)

    async def execute(
        self,
        input_text: str,
        context: CommandContext,
    ) -> tuple[list[CommandResult], str]:
        """执行命令"""
        parsed_commands = self.parser.parse(input_text)
        results = []

        for parsed in parsed_commands:
            result = await self._execute_single(parsed, context)
            results.append(result)

            # Minecraft 风格日志
            self._log_execution(parsed, result, context)

            if result.should_exit:
                break

        clean_text = self.parser.extract_clean_text(input_text)
        return results, clean_text

    async def _execute_single(
        self,
        parsed: ParsedCommand,
        context: CommandContext,
    ) -> CommandResult:
        cmd_def = self._get_command(parsed.name)

        if not cmd_def:
            return CommandResult.no_handler(parsed.name)

        # 四级权限闸门：调用方等级不足直接失败（日志审计留在执行器，是否告知用户由调用方决定）
        if context.permission < cmd_def.permission:
            return CommandResult.failure(
                parsed.name,
                f"权限不足：/{parsed.name} 需要 {cmd_def.permission.name} 及以上权限",
            )

        try:
            args = cmd_def.parse_args(parsed.content)
            kwargs = {"ctx": context, "cmd": parsed, **args}
            sig = cmd_def._sig
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

            if cmd_def._is_async:
                result = await cmd_def.handler(**filtered_kwargs)
            else:
                result = cmd_def.handler(**filtered_kwargs)

            if not isinstance(result, CommandResult):
                result = CommandResult.success(parsed.name, str(result) if result else "")

            return result

        except Exception as e:
            logger.error(f"命令执行异常 [{parsed.name}]: {e}")
            return CommandResult.failure(parsed.name, str(e))

    def _log_execution(
        self,
        parsed: ParsedCommand,
        result: CommandResult,
        context: CommandContext,
    ) -> None:
        """记录命令执行日志"""
        args_str = parsed.content if parsed.content else ""
        cmd_str = f"/{parsed.name} {args_str}".strip()

        logger.info(f"[{context.source.upper()}] {context.issuer} issued command: {cmd_str}")

        if result.status == CommandStatus.SUCCESS:
            if result.message:
                logger.info(
                    f"[{context.source.upper()}] Command '{parsed.name}' succeeded: {result.message}"
                )
        elif result.status == CommandStatus.FAILURE:
            logger.warning(
                f"[{context.source.upper()}] Command '{parsed.name}' failed: {result.message}"
            )
        else:
            logger.warning(f"[{context.source.upper()}] Command '{parsed.name}' unknown")

    def list_commands(self, cmd_type: CommandType | None = None) -> list[CommandDefinition]:
        commands = self._iter_commands()
        if cmd_type is None:
            return commands
        return [cmd for cmd in commands if cmd.type == cmd_type]

    def has_prompt_commands(self, text: str) -> bool:
        return self.parser.has_prompt_commands(text)

    def extract_prompt_context(self, text: str) -> str:
        return self.parser.extract_prompt_context(text)
