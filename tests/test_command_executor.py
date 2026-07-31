"""框架 commands 执行器测试：四级权限闸门、默认 OWNER、本地注册表隔离、执行日志。"""

import asyncio
import unittest

from GensokyoAI.commands import (
    CommandContext,
    CommandExecutor,
    CommandResult,
    CommandStatus,
    PermissionLevel,
    command,
)
from GensokyoAI.utils.logger import logger


def _local_registry() -> dict:
    return {}


class PermissionGateTests(unittest.TestCase):
    def setUp(self):
        self.registry = _local_registry()
        self.calls: list[str] = []

    def _register(self):
        @command(name="owneronly", registry=self.registry)
        async def _owner_cmd(ctx: CommandContext) -> CommandResult:
            self.calls.append("owner")
            return CommandResult.success("owneronly", "ok")

        @command(name="opencmd", permission=PermissionLevel.USER, registry=self.registry)
        async def _user_cmd(ctx: CommandContext) -> CommandResult:
            self.calls.append("user")
            return CommandResult.success("opencmd", "ok")

    def test_unspecified_permission_defaults_to_owner(self):
        self._register()
        self.assertEqual(self.registry["owneronly"].permission, PermissionLevel.OWNER)
        self.assertEqual(self.registry["opencmd"].permission, PermissionLevel.USER)

    def test_denied_below_required_level(self):
        self._register()
        executor = CommandExecutor(registry=self.registry)

        async def run():
            results, _ = await executor.execute(
                "/opencmd", CommandContext(source="test", permission=PermissionLevel.VISITOR)
            )
            return results

        results = asyncio.run(run())
        self.assertEqual(results[0].status, CommandStatus.FAILURE)
        self.assertIn("权限不足", results[0].message)
        self.assertEqual(self.calls, [])  # handler 未执行

    def test_allowed_at_required_level(self):
        self._register()
        executor = CommandExecutor(registry=self.registry)

        async def run():
            return (await executor.execute(
                "/opencmd", CommandContext(source="test", permission=PermissionLevel.USER)
            ))[0]

        results = asyncio.run(run())
        self.assertEqual(results[0].status, CommandStatus.SUCCESS)
        self.assertEqual(self.calls, ["user"])

    def test_context_default_permission_is_owner(self):
        """向后兼容：不显式给权限的调用方（console 等本地后端）按 OWNER 放行。"""
        self._register()
        executor = CommandExecutor(registry=self.registry)

        async def run():
            return (await executor.execute("/owneronly", CommandContext(source="console")))[0]

        results = asyncio.run(run())
        self.assertEqual(results[0].status, CommandStatus.SUCCESS)
        self.assertEqual(self.calls, ["owner"])


class LocalRegistryIsolationTests(unittest.TestCase):
    def test_local_registry_not_in_global(self):
        from GensokyoAI.commands import get_command

        registry = _local_registry()

        @command(name="isolatedcmd", registry=registry)
        async def _cmd(ctx: CommandContext) -> CommandResult:
            return CommandResult.success("isolatedcmd", "ok")

        self.assertIn("isolatedcmd", registry)
        self.assertIsNone(get_command("isolatedcmd"))  # 全局注册表未被污染

        executor = CommandExecutor(registry=registry)
        results = asyncio.run(executor.execute("/isolatedcmd", CommandContext(source="test")))
        self.assertEqual(results[0][0].status, CommandStatus.SUCCESS)


class ExecutionLogTests(unittest.TestCase):
    def test_issued_command_logged_minecraft_style(self):
        registry = _local_registry()

        @command(name="logcmd", permission=PermissionLevel.VISITOR, registry=registry)
        async def _cmd(ctx: CommandContext) -> CommandResult:
            return CommandResult.success("logcmd", "done")

        executor = CommandExecutor(registry=registry)
        lines: list[str] = []
        handler_id = logger.add(lambda m: lines.append(str(m)), format="{message}", level="INFO")
        try:
            ctx = CommandContext(
                source="nb2", issuer="wzb(3072252442)", permission=PermissionLevel.VISITOR
            )
            asyncio.run(executor.execute("/logcmd", ctx))
        finally:
            logger.remove(handler_id)
        self.assertIn("[NB2] wzb(3072252442) issued command: /logcmd\n", lines)
        self.assertIn("[NB2] Command 'logcmd' succeeded: done\n", lines)


if __name__ == "__main__":
    unittest.main()
