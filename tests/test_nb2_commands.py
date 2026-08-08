"""nb2 指令测试：权限解析、本地注册表、/help 与 /quota 处理器、执行器对接。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from GensokyoAI.backends.nb2.commands import (
    NB2_COMMANDS,
    _format_quota,
    _format_quota_health,
    _format_status,
    _format_uptime,
    cmd_help,
    cmd_quota,
    resolve_level,
)
from GensokyoAI.commands import CommandContext, CommandExecutor, CommandStatus, PermissionLevel
from GensokyoAI.core.config_schema import HealthConfig
from GensokyoAI.core.health import HealthCenter
from GensokyoAI.runtime.host import RuntimeHost

_HEALTH_CENTER = HealthCenter(HealthConfig())  # 默认阈值 20/5


class _Sender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


def _ctx(level: PermissionLevel, send, host: RuntimeHost | None = None) -> CommandContext:
    return CommandContext(
        source="nb2",
        issuer="tester(123)",
        permission=level,
        metadata={
            "host": host or RuntimeHost(),
            "config": SimpleNamespace(character="KirisameMarisa"),
            "member_qq": 123,
            "send": send,
            "health_center": _HEALTH_CENTER,
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
                "tenants": {"groups": 5, "users": 3, "other": 0},
                "active_operations": 2,
                "latency": {
                    "count": 12,
                    "median_ms": 3200.0,
                    "avg_ms": 3500.0,
                    "last_ms": 2100.0,
                    "max_ms": 9000.0,
                },
                "gates": [
                    {
                        "name": "runtime",
                        "max_concurrent": 16,
                        "active": 2,
                        "waiting": 0,
                        "instances": 1,
                    },
                    {
                        "name": "model",
                        "max_concurrent": 2,
                        "active": 2,
                        "waiting": 1,
                        "instances": 4,
                    },
                    {
                        "name": "stream",
                        "max_concurrent": 4,
                        "active": 0,
                        "waiting": 0,
                        "instances": 4,
                    },
                ],
                "load_level": {
                    "level": "critical",
                    "reason": "闸门利用率最高 100%，1 个请求排队中",
                },
            },
            health_center=_HEALTH_CENTER,
        )
        # 会话租户总数 = 群 + 私聊 + 其他（nb2-meta 元租户已删）：5+3+0=8
        self.assertIn("🔴 临界", text)
        self.assertIn("1 个请求排队中", text)
        self.assertIn("5 群 / 3 私聊（共 8 个会话租户）", text)
        self.assertIn("处理中：2 个会话正在生成", text)
        self.assertIn("runtime 2/16", text)
        self.assertIn("model 2/2×4（排队 1）", text)
        self.assertNotIn("stream", text)  # 空闲闸门不显示
        self.assertIn("中位 3.2s", text)
        self.assertIn("近 12 次内心思考", text)

    def test_format_status_no_latency_samples(self):
        text = _format_status(
            {
                "tenants": {"groups": 0, "users": 0, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
                "gates": [{"name": "runtime", "max_concurrent": 8, "active": 0, "waiting": 0}],
                "load_level": {"level": "healthy", "reason": "运行正常"},
            },
            health_center=_HEALTH_CENTER,
        )
        self.assertIn("🟢 健康", text)
        self.assertIn("思考延迟：预计中…", text)
        self.assertIn("runtime 0/8", text)  # runtime 总闸常驻显示

    def test_status_handler_sends_formatted_text(self):
        async def run():
            host = RuntimeHost()
            host.get_system_status = lambda: {
                "tenants": {"groups": 1, "users": 0, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
            }
            host.get_quota = AsyncMock(return_value=None)
            from GensokyoAI.backends.nb2 import commands as nb2_commands
            from GensokyoAI.backends.nb2.commands import cmd_status

            nb2_commands._quota_cache = None  # 避免跨测试缓存污染
            sender = _Sender()
            await cmd_status(_ctx(PermissionLevel.USER, sender, host=host))
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("1 群", sender.messages[0])
            self.assertIn("暂不可用", sender.messages[0])  # get_quota 返回 None 的口径

        asyncio.run(run())

    def test_format_status_with_all_extras(self):
        text = _format_status(
            {
                "tenants": {"groups": 2, "users": 1, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
                "gates": [],
                "load_level": {"level": "healthy", "reason": "运行正常"},
                "memory": {"topics": 42, "memories": 137},
                "cost": {"count": 9, "burn_per_day": 1.63, "total_cost": 0.8},
                "uptime_seconds": 90061.0,
                "version": {"package": "2026.8.8.0", "protocol": "2.2.0"},
            },
            quota={"available_balance": 36.5, "cash_balance": 30.0, "voucher_balance": 6.5},
            quota_fetched=True,
            health_center=_HEALTH_CENTER,
            repeat_guard=SimpleNamespace(stats=lambda: {"muted": 2, "watching": 1, "tracked": 9}),
        )
        # 健康判定走 HealthCenter 静态阈值；日耗计量仅附加展示
        self.assertIn(
            "额度：🟢 健康指数 100（余额 ¥36.50（现金 ¥30.00，代金券 ¥6.50，日耗 ¥1.63））",
            text,
        )
        self.assertIn("复读防护：2 人冷却中 · 1 人观察中", text)
        self.assertIn("记忆：42 个话题 / 137 条珍贵记忆", text)
        self.assertIn("版本：v2026.8.8.0（协议 2.2.0） · 已运行 1 天 1 小时", text)

    def test_format_status_minimal_dict_skips_new_lines(self):
        text = _format_status(
            {
                "tenants": {"groups": 0, "users": 0, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
                "gates": [],
                "load_level": {"level": "healthy", "reason": "运行正常"},
            },
            health_center=_HEALTH_CENTER,
        )
        self.assertNotIn("额度", text)
        self.assertNotIn("复读防护", text)
        self.assertNotIn("记忆", text)
        self.assertNotIn("版本", text)

    def test_quota_health_levels(self):
        self.assertIn(
            "🟢 健康指数 100",
            _format_quota_health({"available_balance": 25.0}, _HEALTH_CENTER),
        )
        self.assertIn(
            "🟡 健康指数 50", _format_quota_health({"available_balance": 10.0}, _HEALTH_CENTER)
        )
        self.assertIn(
            "🔴 健康指数 20", _format_quota_health({"available_balance": 4.0}, _HEALTH_CENTER)
        )
        self.assertIn("🟣 耗尽", _format_quota_health({"available_balance": 0.0}, _HEALTH_CENTER))
        self.assertIn("暂不可用", _format_quota_health(None, _HEALTH_CENTER))

    def test_format_uptime(self):
        self.assertEqual(_format_uptime(90061), "1 天 1 小时")
        self.assertEqual(_format_uptime(3661), "1 小时 1 分")
        self.assertEqual(_format_uptime(59), "0 分钟")

    def test_quota_cache_avoids_refetch(self):
        async def run():
            from GensokyoAI.backends.nb2 import commands as nb2_commands

            nb2_commands._quota_cache = None
            host = RuntimeHost()
            host.get_quota = AsyncMock(return_value={"available_balance": 99.0})
            first = await nb2_commands._get_quota_cached(host, "KirisameMarisa")
            second = await nb2_commands._get_quota_cached(host, "KirisameMarisa")
            self.assertEqual(first, second)
            self.assertEqual(host.get_quota.await_count, 1)  # 第二次命中缓存

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
