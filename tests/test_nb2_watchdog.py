"""NapCat 掉线守护（backends/nb2/watchdog）状态机定向测试

外部动作（精确杀/启动/存活探测/QQ 捕获/QQ 路径/睡眠/时钟）全部注入假实现，
只验证触发→节制→精确杀→孵化捕获→确认恢复的状态流转与告警行为。
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from GensokyoAI.backends.nb2.config import Nb2Config
from GensokyoAI.backends.nb2.watchdog import NapCatWatchdog, _windows_launch_napcat


async def _noop(*args):
    return None


def _make_watchdog(tmp: Path, **overrides) -> NapCatWatchdog:
    options = {
        "cooldown_seconds": 600.0,
        "max_restarts_per_day": 5,
        "recover_timeout_seconds": 0.2,
        "disconnect_grace_seconds": 0.05,
        "alert_path": tmp / "alert.json",
        "state_path": tmp / "bot.json",
        "kill": _noop,
        "launch": lambda napcat_dir, qq_path, qq: 4242,
        "pid_alive": lambda pid: _noop_alive(pid),
        "find_child_qq": lambda pid: _noop_qq(pid),
        "resolve_qq_path": lambda: Path("QQ.exe"),
        # 测试加速：所有 sleep 截断到 20ms（含孵化轮询与冷却重试）
        "sleep": lambda seconds: asyncio.sleep(min(seconds, 0.02)),
        "platform": "win32",
    }
    options.update(overrides)
    watchdog = NapCatWatchdog(**options)
    watchdog.configure(napcat_dir=tmp)
    return watchdog


async def _noop_alive(pid: int) -> bool:
    return True


async def _noop_qq(pid: int) -> int:
    return 987654  # 默认孵化期总能捕获到 bot QQ（写入追踪状态）


async def _drive_trigger(watchdog: NapCatWatchdog, clock: list[float], reason: str) -> str:
    """假时钟下驱动一次 trigger 到完成：假时钟冻结时 trigger 的回连确认轮询
    永远等不到 deadline——测试侧手动拨钟直到任务结束（模拟时间流逝）。"""
    task = asyncio.create_task(watchdog.trigger(reason))
    while not task.done():
        clock[0] += 0.05
        await asyncio.sleep(0.01)
    return task.result()


class TriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_restart_and_confirm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            watchdog = _make_watchdog(
                Path(tmpdir),
                kill=lambda qq_pid: calls.append(f"kill:{qq_pid}") or _noop(),
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
            self.assertEqual(calls, ["kill:None", "launch"])  # 无追踪记录时不盲杀
            # 捕获的 QQ pid 已持久化（后续精确管理的把手）
            tracked = json.loads((Path(tmpdir) / "bot.json").read_text(encoding="utf-8"))
            self.assertEqual(tracked["qq_pid"], 987654)

    async def test_tracked_bot_killed_precisely(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "bot.json").write_text(
                json.dumps({"qq_pid": 7777, "launcher_pid": 1}), encoding="utf-8"
            )
            kills: list[int | None] = []

            async def kill(qq_pid):
                kills.append(qq_pid)

            watchdog = _make_watchdog(Path(tmpdir), kill=kill)
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()

            async def reconnect() -> None:
                await asyncio.sleep(0.05)
                watchdog.notify_connected(3779163297)

            task = asyncio.create_task(reconnect())
            await watchdog.trigger("bot_offline")
            await task
            self.assertEqual(kills, [7777])  # 精确杀追踪的 bot QQ，不碰别的

    async def test_recover_timeout_alerts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchdog = _make_watchdog(Path(tmpdir))
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            result = await watchdog.trigger("ws_disconnect")
            self.assertEqual(result, "recover_timeout")
            alert = json.loads((Path(tmpdir) / "alert.json").read_text(encoding="utf-8"))
            self.assertEqual(alert["kind"], "recover_timeout")
            watchdog.notify_connected(3779163297)
            self.assertFalse((Path(tmpdir) / "alert.json").exists())

    async def test_cooldown_blocks_second_trigger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = [1000.0]
            watchdog = _make_watchdog(
                Path(tmpdir), recover_timeout_seconds=0.01, now=lambda: clock[0]
            )
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            first = await _drive_trigger(watchdog, clock, "a")
            self.assertEqual(first, "recover_timeout")
            second = await watchdog.trigger("b")
            self.assertEqual(second, "cooldown")
            # 冷却拒绝不再是丢弃：排了到期重试（仍离线才重试）
            self.assertIsNotNone(watchdog._cooldown_retry_task)
            watchdog.close()

    async def test_cooldown_retry_fires_when_still_offline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            clock = [1000.0]
            watchdog = _make_watchdog(
                Path(tmpdir),
                recover_timeout_seconds=0.01,
                launch=lambda d, p, q: calls.append("launch") or 4242,
                now=lambda: clock[0],
            )
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            await _drive_trigger(watchdog, clock, "a")
            self.assertEqual(await watchdog.trigger("b"), "cooldown")
            await asyncio.sleep(0.06)  # retry 已醒，但时钟未走 → 续排下一轮
            self.assertEqual(len(calls), 1)
            clock[0] += 700.0  # 冷却期满（默认 600s）
            await asyncio.sleep(0.15)
            self.assertEqual(len(calls), 2)
            watchdog.close()

    async def test_cooldown_retry_cancelled_on_reconnect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            clock = [1000.0]
            watchdog = _make_watchdog(
                Path(tmpdir),
                recover_timeout_seconds=0.01,
                launch=lambda d, p, q: calls.append("launch") or 4242,
                now=lambda: clock[0],
            )
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            await _drive_trigger(watchdog, clock, "a")
            self.assertEqual(await watchdog.trigger("b"), "cooldown")
            watchdog.notify_connected(3779163297)  # 回连：重试取消
            clock[0] += 700.0
            await asyncio.sleep(0.15)
            self.assertEqual(len(calls), 1)
            watchdog.close()

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
                launch=lambda d, p, q: calls.append("launch") or 4242,
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

    async def test_flash_exit_once_then_relaunch_recovers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            launches: list[int] = []
            captures = {"count": 0}

            def launch(d, p, q):
                launches.append(1)
                return 4000 + len(launches)

            async def find_child_qq(pid):
                captures["count"] += 1
                return None if pid == 4001 else 4002  # 第一次启动见不到 QQ

            async def pid_alive(pid):
                return pid != 4001  # 第一次启动的 launcher 已退（秒退）

            watchdog = _make_watchdog(
                Path(tmpdir),
                launch=launch,
                find_child_qq=find_child_qq,
                pid_alive=pid_alive,
            )
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()

            async def reconnect() -> None:
                await asyncio.sleep(0.1)
                watchdog.notify_connected(3779163297)

            task = asyncio.create_task(reconnect())
            result = await watchdog.trigger("bot_offline")
            await task
            self.assertEqual(result, "restarted")
            self.assertEqual(len(launches), 2)  # 秒退后自动重试了一次
            watchdog.close()

    async def test_process_died_alerts_after_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            launches: list[int] = []

            def launch(d, p, q):
                launches.append(1)
                return 4242

            watchdog = _make_watchdog(
                Path(tmpdir),
                recover_timeout_seconds=60.0,
                launch=launch,
                find_child_qq=_none_qq,  # 永远见不到 QQ
                pid_alive=_none_alive,  # launcher 也活不成（两次都秒退）
            )
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            result = await watchdog.trigger("bot_offline")
            self.assertEqual(result, "process_died")
            self.assertEqual(len(launches), 2)  # 秒退自动重试过一次
            alert = json.loads((Path(tmpdir) / "alert.json").read_text(encoding="utf-8"))
            self.assertEqual(alert["kind"], "process_died")

    async def test_captured_qq_dying_mid_wait_alerts_early(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchdog = _make_watchdog(
                Path(tmpdir),
                recover_timeout_seconds=60.0,
                pid_alive=lambda pid: _dead(pid),  # 捕获后 QQ 很快死掉
            )
            watchdog.notify_connected(3779163297)
            watchdog._connected.clear()
            result = await watchdog.trigger("bot_offline")
            self.assertEqual(result, "process_died")
            alert = json.loads((Path(tmpdir) / "alert.json").read_text(encoding="utf-8"))
            self.assertEqual(alert["kind"], "process_died")


async def _none_qq(pid):
    return None


async def _none_alive(pid):
    return False


async def _dead(pid):
    return False


class DisconnectGraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_within_grace_skips_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[str] = []
            watchdog = _make_watchdog(
                Path(tmpdir), launch=lambda d, p, q: calls.append("launch") or 4242
            )
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
                launch=lambda d, p, q: calls.append("launch") or 4242,
            )
            watchdog.notify_connected(3779163297)
            watchdog.notify_disconnected()
            await asyncio.sleep(0.3)  # 宽限期 + 恢复超时都过了
            self.assertEqual(calls, ["launch"])
            watchdog.close()

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
            watchdog.close()


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
            watchdog.close()


class TrackedStateTests(unittest.TestCase):
    def test_load_tracked_roundtrip_and_corrupt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            watchdog = _make_watchdog(Path(tmpdir))
            self.assertIsNone(watchdog._load_tracked())  # 文件不存在
            watchdog._save_tracked(qq_pid=4321, launcher_pid=1234)
            tracked = watchdog._load_tracked()
            self.assertEqual(tracked["qq_pid"], 4321)
            self.assertEqual(tracked["launcher_pid"], 1234)
            # 损坏文件 → None（不炸）
            (Path(tmpdir) / "bot.json").write_text("{bad json", encoding="utf-8")
            self.assertIsNone(watchdog._load_tracked())


class LaunchCommandTests(unittest.TestCase):
    def test_kill_aux_ps_covers_pause_stuck_bat_consoles(self):
        # 旧 main.bat 实例被杀后 cmd 卡在 pause：辅助杀脚本必须一并清理这些窗口
        from GensokyoAI.backends.nb2.watchdog import _KILL_AUX_PS

        self.assertIn("cmd.exe", _KILL_AUX_PS)
        self.assertIn("launcher-win10", _KILL_AUX_PS)
        self.assertIn("NapCatWinBootMain.exe", _KILL_AUX_PS)

    def test_launch_wraps_cmd_with_utf8_codepage(self):
        # 守护拉起的控制台必须先 chcp 65001（launcher bat 同款），否则中文日志乱码
        with tempfile.TemporaryDirectory() as tmpdir:
            captured: dict = {}

            def fake_popen(args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                return SimpleNamespace(pid=1234)

            with patch(
                "GensokyoAI.backends.nb2.watchdog.subprocess.Popen", fake_popen
            ):
                pid = _windows_launch_napcat(Path(tmpdir), Path("QQ.exe"), 3779163297)
            self.assertEqual(pid, 1234)
            args = captured["args"]
            self.assertEqual(args[:2], ["cmd", "/c"])
            command = args[2]
            self.assertIn("chcp 65001", command)
            self.assertIn("3779163297", command)
            self.assertIn("NapCatWinBootMain.exe", command)
            # loadNapCat.js 按 launcher 同款内容落盘
            load_js = (Path(tmpdir) / "loadNapCat.js").read_text(encoding="utf-8")
            self.assertIn("napcat.mjs", load_js)


class WatchdogConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = Nb2Config.from_env({}.get)
        self.assertTrue(config.watchdog_enabled)
        self.assertEqual(config.napcat_dir, Path("ignore/NapCat.Shell"))
        self.assertEqual(config.watchdog_cooldown_seconds, 600.0)
        self.assertEqual(config.watchdog_max_restarts, 5)
        self.assertEqual(config.watchdog_recover_timeout, 900.0)
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
