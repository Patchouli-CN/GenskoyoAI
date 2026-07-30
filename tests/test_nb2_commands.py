"""nb2 指令系统测试：四级权限模型、别名索引、/help 与 /quota 处理器。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from GensokyoAI.backends.nb2.commands import (
    CommandContext,
    PermissionLevel,
    can_execute,
    find_command,
    resolve_level,
)
from GensokyoAI.runtime.host import RuntimeHost


class _Sender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


def _ctx(level: PermissionLevel, send, host: RuntimeHost | None = None) -> CommandContext:
    return CommandContext(
        host=host or RuntimeHost(),
        config=SimpleNamespace(character="KirisameMarisa"),
        member_qq=123,
        level=level,
        send=send,
    )


class PermissionLevelTests(unittest.TestCase):
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


class CommandRegistryTests(unittest.TestCase):
    def test_alias_index(self):
        self.assertIs(find_command("quota"), find_command("额度"))
        self.assertIs(find_command("help"), find_command("帮助"))
        self.assertIsNone(find_command("rm"))

    def test_can_execute_threshold(self):
        quota = find_command("quota")
        assert quota is not None
        self.assertTrue(can_execute(quota, PermissionLevel.USER))
        self.assertTrue(can_execute(quota, PermissionLevel.OWNER))
        self.assertFalse(can_execute(quota, PermissionLevel.VISITOR))
        help_cmd = find_command("help")
        assert help_cmd is not None
        self.assertTrue(can_execute(help_cmd, PermissionLevel.VISITOR))


class HelpHandlerTests(unittest.TestCase):
    def test_help_lists_only_permitted_commands(self):
        async def run():
            sender = _Sender()
            await find_command("help").handler(_ctx(PermissionLevel.VISITOR, sender))
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("/help", sender.messages[0])
            self.assertNotIn("quota", sender.messages[0])  # VISITOR 看不到 USER 级指令

            sender2 = _Sender()
            await find_command("help").handler(_ctx(PermissionLevel.USER, sender2))
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
            await find_command("quota").handler(_ctx(PermissionLevel.USER, sender, host=host))
            self.assertEqual(sender.messages, ["当前额度：¥49.59（现金 ¥3.00，代金券 ¥46.59）"])

        asyncio.run(run())

    def test_quota_unsupported_provider(self):
        async def run():
            host = RuntimeHost()
            host.get_quota = AsyncMock(return_value=None)
            sender = _Sender()
            await find_command("quota").handler(_ctx(PermissionLevel.USER, sender, host=host))
            self.assertEqual(sender.messages, ["当前 Provider 不支持额度查询。"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
