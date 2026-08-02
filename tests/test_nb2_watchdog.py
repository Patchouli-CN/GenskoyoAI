"""NapCat 掉线守护（backends/nb2/watchdog）状态机定向测试

外部动作（杀树/启动/查 QQ 路径/睡眠/时钟）全部注入假实现，
只验证触发→节制→重启→确认恢复的状态流转与告警行为。
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from GensokyoAI.backends.nb2.config import Nb2Config
from GensokyoAI.backends.nb2.watchdog import NapCatWatchdog


def _make_watchdog(tmp: Path, **overrides) -> NapCatWatchdog:
    options = {
        "cooldown_seconds": 600.0,
        "max_restarts_per_day": 5,
        "recover_timeout_seconds": 0.2,
        "disconnect_grace_seconds": 0.05,
        "alert_path": tmp / "alert.json",
        "kill": lambda: None,
        "launch": lambda napcat_dir, qq_path, qq: 4242,
        "resolve_qq_path": lambda: Path("QQ.exe"),
        "platform": "win32",
    }
    options.update(overrides)
    watchdog = NapCatWatchdog(**options)
    watchdog.configure(napcat_dir=tmp)
    return watchdog


class TriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_restart_and_confirm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            watchdog = _make_watchdog(
                Path(tmpdir),
                kill=lambda: calls.append("kill"),
                launch=lambda d, p, q: calls.append("launch") or 4242,
            )
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()

            async def reconnect() -> None:
                await asyncio.sleep(0.05)
                watchdog.notify_connected(3779163297)

            task = asyncio.create_task(reconnect())
            result = await watchdog.trigger("bot_offline: kicked 登录已失效")
            await task
            self.assertEqual(result, "restarted")
            self.assertEqual(calls, ["kill", "launch"])

    async def test_recover_timeout_alerts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchdog = _make_watchdog(Path(tmpdir))
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            result = await watchdog.trigger("ws_disconnect")
            self.assertEqual(result, "recover_timeout")
            alert = json.loads((Path(tmpdir) / "alert.json").read_text(encoding="utf-8"))
            self.assertEqual(alert["kind"], "recover_timeout")
            # 回连后哨兵清除
            watchdog.notify_connected(3779163297)
            self.assertFalse((Path(tmpdir) / "alert.json").exists())

    async def test_cooldown_blocks_second_trigger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchdog = _make_watchdog(Path(tmpdir), recover_timeout_seconds=0.01)
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            first = await watchdog.trigger("a")
            self.assertEqual(first, "recover_timeout")
            second = await watchdog.trigger("b")
            self.assertEqual(second, "cooldown")

    async def test_daily_cap_stops_auto_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchdog = _make_watchdog(
                Path(tmpdir), cooldown_seconds=0.0, max_restarts_per_day=2,
                recover_timeout_seconds=0.01,
            )
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            self.assertEqual(await watchdog.trigger("1"), "recover_timeout")
            self.assertEqual(await watchdog.trigger("2"), "recover_timeout")
            self.assertEqual(await watchdog.trigger("3"), "daily_cap")
            alert = json.loads((Path(tmpdir) / "alert.json").read_text(encoding="utf-8"))
            self.assertEqual(alert["kind"], "daily_cap")
            self.assertEqual(alert["attempts_24h"], 2)

    async def test_not_windows_only_alerts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            watchdog = _make_watchdog(
                Path(tmpdir),
                platform="linux",
                kill=lambda: calls.append("kill"),
            )
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            result = await watchdog.trigger("bot_offline")
            self.assertEqual(result, "not_windows")
            self.assertEqual(calls, [])

    async def test_not_ready_without_bot_qq(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchdog = _make_watchdog(Path(tmpdir))  # 从未 connect，QQ 号未知
            result = await watchdog.trigger("bot_offline")
            self.assertEqual(result, "not_ready")

    async def test_restart_failure_alerts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            def bad_launch(d, p, q):
                raise FileNotFoundError("NapCatWinBootMain.exe 不存在")

            watchdog = _make_watchdog(Path(tmpdir), launch=bad_launch)
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            result = await watchdog.trigger("bot_offline")
            self.assertEqual(result, "restart_failed")
            alert = json.loads((Path(tmpdir) / "alert.json").read_text(encoding="utf-8"))
            self.assertEqual(alert["kind"], "restart_failed")

    async def test_disabled_after_close(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchdog = _make_watchdog(Path(tmpdir))
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            watchdog.close()
            self.assertEqual(await watchdog.trigger("bot_offline"), "disabled")


class DisconnectGraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_within_grace_skips_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            watchdog = _make_watchdog(Path(tmpdir), kill=lambda: calls.append("kill"))
            watchdog.notify_connected(3779163297)
            watchdog.notify_disconnected()
            await asyncio.sleep(0.01)
            watchdog.notify_connected(3779163297)  # 宽限期内自己连回来了
            await asyncio.sleep(0.1)
            self.assertEqual(calls, [])

    async def test_no_reconnect_triggers_restart_after_grace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            watchdog = _make_watchdog(
                Path(tmpdir),
                recover_timeout_seconds=0.05,
                kill=lambda: calls.append("kill"),
            )
            watchdog.notify_connected(3779163297)
            watchdog.notify_disconnected()
            await asyncio.sleep(0.3)  # 宽限期 + 恢复超时都过了
            self.assertEqual(calls, ["kill"])

    async def test_disconnect_during_restart_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchdog = _make_watchdog(Path(tmpdir), recover_timeout_seconds=0.05)
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            task = asyncio.create_task(watchdog.trigger("bot_offline"))
            await asyncio.sleep(0)  # 让 trigger 跑起来
            watchdog.notify_disconnected()  # 我们亲手杀的 → 不应再排队
            self.assertIsNone(watchdog._grace_task)
            await task


class BotOfflineEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_offline_spawns_single_recovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []

            def slow_launch(d, p, q):
                time.sleep(0.05)
                calls.append("launch")
                return 4242

            watchdog = _make_watchdog(
                Path(tmpdir), recover_timeout_seconds=0.05, launch=slow_launch
            )
            watchdog.notify_connected(3779163297)
            watchdog.notify_bot_offline("kicked", "登录已失效")
            watchdog.notify_bot_offline("kicked", "登录已失效")  # 单 flight：忽略重复
            self.assertIsNotNone(watchdog._recover_task)
            await watchdog._recover_task
            self.assertEqual(calls, ["launch"])


class WatchdogConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = Nb2Config.from_env({}.get)
        self.assertTrue(config.watchdog_enabled)
        self.assertEqual(config.napcat_dir, Path("ignore/NapCat.Shell"))
        self.assertEqual(config.watchdog_cooldown_seconds, 600.0)
        self.assertEqual(config.watchdog_max_restarts, 5)
        self.assertEqual(config.watchdog_recover_timeout, 300.0)
        self.assertEqual(config.watchdog_disconnect_grace, 60.0)

    def test_parse_from_env(self):
        env = {
            "GSK_NB2_WATCHDOG": "0",
            "GSK_NB2_NAPCAT_DIR": "D:/napcat",
            "GSK_NB2_WATCHDOG_COOLDOWN": "120",
            "GSK_NB2_WATCHDOG_MAX_RESTARTS": "3",
            "GSK_NB2_WATCHDOG_RECOVER_TIMEOUT": "90",
            "GSK_NB2_WATCHDOG_DISCONNECT_GRACE": "30",
        }
        config = Nb2Config.from_env(env.get)
        self.assertFalse(config.watchdog_enabled)
        self.assertEqual(config.napcat_dir, Path("D:/napcat"))
        self.assertEqual(config.watchdog_cooldown_seconds, 120.0)
        self.assertEqual(config.watchdog_max_restarts, 3)
        self.assertEqual(config.watchdog_recover_timeout, 90.0)
        self.assertEqual(config.watchdog_disconnect_grace, 30.0)


if __name__ == "__main__":
    unittest.main()
