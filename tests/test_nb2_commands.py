"""nb2 指令测试：权限解析、本地注册表、/help 与 /quota 处理器、执行器对接。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from GensokyoAI.backends.nb2.commands import (
    NB2_COMMANDS,
    _format_quota,
    _format_status,
    cmd_help,
    cmd_quota,
    resolve_level,
)
from GensokyoAI.commands import CommandContext, CommandExecutor, CommandStatus, PermissionLevel
from GensokyoAI.runtime.host import RuntimeHost


class _Sender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


def _ctx(
    level: PermissionLevel, send, host: RuntimeHost | None = None
) -> CommandContext:
    return CommandContext(
        source="nb2",
        issuer="tester(123)",
        permission=level,
        metadata={
            "host": host or RuntimeHost(),
            "config": SimpleNamespace(character="KirisameMarisa"),
            "member_qq": 123,
            "send": send,
        },
    )


class ResolveLevelTests(unittest.TestCase):
    def test_owner_list_resolves_owner(self):
        self.assertEqual(resolve_level(123, frozenset({123}), None), PermissionLevel.OWNER)
        self.assertEqual(resolve_level(123, frozenset({999}), None), PermissionLevel.VISITOR)

    def test_group_admin_resolves_admin(self):
        self.assertEqual(resolve_level(456, frozenset(), "admin"), PermissionLevel.ADMIN)
        self.assertEqual(resolve_level(456, frozenset(), "owner"), PermissionLevel.ADMIN)

    def test_member_resolves_user_and_none_resolves_visitor(self):
        self.assertEqual(resolve_level(456, frozenset(), "member"), PermissionLevel.USER)
        self.assertEqual(resolve_level(456, frozenset(), None), PermissionLevel.VISITOR)

    def test_owner_beats_admin_role(self):
        self.assertEqual(resolve_level(123, frozenset({123}), "member"), PermissionLevel.OWNER)


class LocalRegistryTests(unittest.TestCase):
    def test_commands_registered_with_aliases_and_permissions(self):
        self.assertIs(NB2_COMMANDS["quota"], NB2_COMMANDS["额度"])
        self.assertIs(NB2_COMMANDS["help"], NB2_COMMANDS["帮助"])
        self.assertIs(NB2_COMMANDS["status"], NB2_COMMANDS["状态"])
        self.assertEqual(NB2_COMMANDS["quota"].permission, PermissionLevel.USER)
        self.assertEqual(NB2_COMMANDS["help"].permission, PermissionLevel.VISITOR)
        self.assertEqual(NB2_COMMANDS["status"].permission, PermissionLevel.USER)

    def test_local_registry_does_not_pollute_global(self):
        from GensokyoAI.commands import get_command

        # nb2 指令只进本地注册表；框架全局注册表里的同名指令不被覆盖
        self.assertIsNot(get_command("help"), NB2_COMMANDS["help"])


class HelpHandlerTests(unittest.TestCase):
    def test_help_lists_only_permitted_commands(self):
        async def run():
            sender = _Sender()
            await cmd_help(_ctx(PermissionLevel.VISITOR, sender))
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("/help", sender.messages[0])
            self.assertNotIn("quota", sender.messages[0])  # VISITOR 看不到 USER 级指令

            sender2 = _Sender()
            await cmd_help(_ctx(PermissionLevel.USER, sender2))
            self.assertIn("/quota", sender2.messages[0])

        asyncio.run(run())


class QuotaHandlerTests(unittest.TestCase):
    def test_quota_formats_moonshot_balance(self):
        async def run():
            host = RuntimeHost()
            host.get_quota = AsyncMock(
                return_value={
                    "available_balance": 49.59,
                    "voucher_balance": 46.59,
                    "cash_balance": 3.0,
                }
            )
            sender = _Sender()
            await cmd_quota(_ctx(PermissionLevel.USER, sender, host=host))
            self.assertEqual(sender.messages, ["当前额度：¥49.59（现金 ¥3.00，代金券 ¥46.59）"])

        asyncio.run(run())

    def test_quota_unsupported_provider(self):
        async def run():
            host = RuntimeHost()
            host.get_quota = AsyncMock(return_value=None)
            sender = _Sender()
            await cmd_quota(_ctx(PermissionLevel.USER, sender, host=host))
            self.assertEqual(sender.messages, ["当前 Provider 不支持额度查询。"])

        asyncio.run(run())

    def test_format_quota_fallback(self):
        self.assertEqual(_format_quota(None), "当前 Provider 不支持额度查询。")


class StatusCommandTests(unittest.TestCase):
    def test_format_status_full(self):
        text = _format_status(
            {
                "tenants": {"groups": 5, "users": 3, "meta": 1, "other": 0},
                "active_operations": 2,
                "latency": {"count": 12, "median_ms": 3200.0, "avg_ms": 3500.0, "last_ms": 2100.0, "max_ms": 9000.0},
            }
        )
        self.assertIn("5 群 / 3 私聊（共 9 个会话租户）", text)
        self.assertIn("处理中：2 个会话正在生成", text)
        self.assertIn("中位 3.2s", text)
        self.assertIn("近 12 次内心思考", text)
        self.assertIn("峰值 9.0s", text)

    def test_format_status_no_latency_samples(self):
        text = _format_status(
            {
                "tenants": {"groups": 0, "users": 0, "meta": 0, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
            }
        )
        self.assertIn("思考延迟：预计中…", text)

    def test_status_handler_sends_formatted_text(self):
        async def run():
            host = RuntimeHost()
            host.get_system_status = lambda: {
                "tenants": {"groups": 1, "users": 0, "meta": 1, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
            }
            from GensokyoAI.backends.nb2.commands import cmd_status

            sender = _Sender()
            await cmd_status(_ctx(PermissionLevel.USER, sender, host=host))
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("1 群", sender.messages[0])

        asyncio.run(run())


class ExecutorIntegrationTests(unittest.TestCase):
    """插件同款 CommandExecutor：权限闸门与执行日志由框架统一负责。"""

    def test_executor_enforces_permission_and_runs_handler(self):
        async def run():
            executor = CommandExecutor(mode="smart", registry=NB2_COMMANDS)
            # VISITOR 执行 USER 级 /quota：拒绝，handler 不发送
            denied_sender = _Sender()
            results, _ = await executor.execute(
                "/quota", _ctx(PermissionLevel.VISITOR, denied_sender)
            )
            self.assertEqual(results[0].status, CommandStatus.FAILURE)
            self.assertIn("权限不足", results[0].message)
            self.assertEqual(denied_sender.messages, [])

            # 同指令换 OWNER：放行并发送
            ok_sender = _Sender()
            host = RuntimeHost()
            host.get_quota = AsyncMock(return_value=None)
            results, _ = await executor.execute(
                "/quota", _ctx(PermissionLevel.OWNER, ok_sender, host=host)
            )
            self.assertEqual(results[0].status, CommandStatus.SUCCESS)
            self.assertEqual(ok_sender.messages, ["当前 Provider 不支持额度查询。"])

        asyncio.run(run())

    def test_executor_ignores_unregistered_command_silently(self):
        async def run():
            executor = CommandExecutor(mode="smart", registry=NB2_COMMANDS)
            sender = _Sender()
            results, clean = await executor.execute("/rm -rf", _ctx(PermissionLevel.OWNER, sender))
            self.assertEqual(results, [])  # 未注册指令：解析为空，静默
            self.assertEqual(clean, "")
            self.assertEqual(sender.messages, [])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
