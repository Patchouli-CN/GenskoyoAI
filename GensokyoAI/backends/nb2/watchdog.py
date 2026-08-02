"""NapCat 守护：被踢下线 / 断连后的自动恢复（杀进程树 → 快速登录 → 确认回连）。

触发源（plugin.py 接线）：
- OneBot `bot_offline` 通知事件（NapCat 把「你的账号当前登录已失效」等
  踢下线包装成该事件推送，见 NapCat 事件文档 BotOfflineEvent）；
- 反向 WS 断开且宽限期后仍未回连（NapCat 进程崩溃/被杀的场景）。

恢复动作：杀 NapCat 进程树（只杀 NapCatWinBootMain 及其子孙，不碰用户
可能开着的个人 QQ），再按 NapCat.Shell 的 launcher 方式带 QQ 号快速登录
（本地缓存凭证静默重登，无需扫码——用户 2026-08-02 实证）。

节制（防无限重启激怒风控）：单 flight、冷却期、每日重启上限；超过上限或
回连超时则写哨兵文件 + ERROR 日志告警，停手等人处理。非 Windows 平台只
告警不动手（恢复路径是 Windows 专用的注册表查询 + exe 启动）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ...utils.logger import logger

if sys.platform == "win32":
    import winreg

# 杀 NapCat 进程树：NapCatWinBootMain（注入启动器）+ 全部子孙（QQ.exe 及其渲染进程）。
# 按父子关系定向收口，绝不像 KillQQ.bat 那样 taskkill /im QQ.exe 误伤个人 QQ。
_KILL_TREE_PS = (
    "$roots = @(Get-CimInstance Win32_Process -Filter \"Name='NapCatWinBootMain.exe'\""
    " | Select-Object -ExpandProperty ProcessId);"
    " $all = Get-CimInstance Win32_Process;"
    " $targets = New-Object System.Collections.Generic.HashSet[int];"
    " $queue = New-Object System.Collections.Generic.Queue[int];"
    " foreach ($r in $roots) { [void]$targets.Add($r); $queue.Enqueue($r) }"
    " while ($queue.Count) { $p = $queue.Dequeue();"
    " foreach ($c in ($all | Where-Object ParentProcessId -eq $p)) {"
    " if ($targets.Add($c.ProcessId)) { $queue.Enqueue($c.ProcessId) } } }"
    " foreach ($t in $targets) { Stop-Process -Id $t -Force -ErrorAction SilentlyContinue }"
)

_QQ_UNINSTALL_KEY = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"


def _resolve_qq_path() -> Path:
    """从注册表定位 QQNT 安装路径（与 launcher-win10-user.bat 同一招）。"""
    if sys.platform != "win32":
        raise OSError("注册表定位 QQ 路径仅支持 Windows")
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _QQ_UNINSTALL_KEY) as key:
        uninstall, _ = winreg.QueryValueEx(key, "UninstallString")
    qq_path = Path(uninstall).parent / "QQ.exe"
    if not qq_path.exists():
        raise FileNotFoundError(f"注册表定位的 QQ.exe 不存在: {qq_path}")
    return qq_path


def _windows_kill_napcat_tree() -> None:
    """杀掉 NapCat 注入启动器及其全部子孙进程（不存在则安静通过）。"""
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _KILL_TREE_PS],
        capture_output=True,
        timeout=30,
        check=False,
    )


def _windows_launch_napcat(napcat_dir: Path, qq_path: Path, bot_qq: int) -> int:
    """按 launcher 脚本同等环境启动 NapCat（快速登录），返回启动器 PID。"""
    env = {
        **os.environ,
        "NAPCAT_PATCH_PACKAGE": str(napcat_dir / "qqnt.json"),
        "NAPCAT_LOAD_PATH": str(napcat_dir / "loadNapCat.js"),
        "NAPCAT_INJECT_PATH": str(napcat_dir / "NapCatWinBootHook.dll"),
        "NAPCAT_LAUNCHER_PATH": str(napcat_dir / "NapCatWinBootMain.exe"),
        "NAPCAT_MAIN_PATH": str(napcat_dir / "napcat.mjs"),
    }
    # 与 launcher 每次重写 loadNapCat.js 保持一致（内容相同则不写，避免无谓 IO）
    load_js = napcat_dir / "loadNapCat.js"
    content = (
        f'(async () => {{await import("file:///{env["NAPCAT_MAIN_PATH"].replace(chr(92), "/")}")}})()'
    )
    if not load_js.exists() or load_js.read_text(encoding="utf-8").strip() != content:
        load_js.write_text(content, encoding="utf-8")
    process = subprocess.Popen(
        [
            env["NAPCAT_LAUNCHER_PATH"],
            str(qq_path),
            env["NAPCAT_INJECT_PATH"],
            str(bot_qq),
        ],
        cwd=napcat_dir,
        env=env,
        # 独立控制台窗口：存活不依赖本进程，日志对用户可见（与手动启动一致）
        creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    return process.pid


class NapCatWatchdog:
    """NapCat 掉线守护（单 flight + 冷却 + 每日上限 + 回连确认）。

    外部动作全部可注入（kill/launch/resolve_qq_path/sleep/now），测试用
    假实现验证状态机，生产默认 Windows 真实现。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        cooldown_seconds: float = 600.0,
        max_restarts_per_day: int = 5,
        recover_timeout_seconds: float = 300.0,
        disconnect_grace_seconds: float = 60.0,
        alert_path: Path | None = None,
        kill: Callable[[], None] = _windows_kill_napcat_tree,
        launch: Callable[[Path, Path, int], int] = _windows_launch_napcat,
        resolve_qq_path: Callable[[], Path] = _resolve_qq_path,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        now: Callable[[], float] = time.time,
        platform: str = sys.platform,
    ) -> None:
        self.enabled = enabled
        self.cooldown_seconds = cooldown_seconds
        self.max_restarts_per_day = max_restarts_per_day
        self.recover_timeout_seconds = recover_timeout_seconds
        self.disconnect_grace_seconds = disconnect_grace_seconds
        self.alert_path = alert_path
        self._kill = kill
        self._launch = launch
        self._resolve_qq_path = resolve_qq_path
        self._sleep = sleep
        self._now = now
        self._platform = platform
        self._napcat_dir: Path | None = None
        self._bot_qq: int | None = None
        self._attempts: list[float] = []  # 近 24h 重启时间戳
        self._last_attempt = 0.0
        self._recover_task: asyncio.Task[Any] | None = None
        self._grace_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._closed = False

    def configure(self, *, napcat_dir: Path) -> None:
        self._napcat_dir = napcat_dir

    def close(self) -> None:
        """进程退出时停用守护（防止适配器正常关停时反而把 NapCat 拉起来）。"""
        self._closed = True
        for task in (self._recover_task, self._grace_task):
            if task and not task.done():
                task.cancel()

    # ==================== 事件入口（plugin 接线） ====================

    def notify_connected(self, bot_qq: int) -> None:
        """协议端回连（driver.on_bot_connect）：记 QQ 号、解除离线、清哨兵。"""
        was_offline = not self._connected.is_set()
        self._bot_qq = bot_qq
        self._connected.set()
        if self._grace_task and not self._grace_task.done():
            self._grace_task.cancel()
        if was_offline:
            logger.info(f"[nb2-watchdog] 协议端已回连（QQ {bot_qq}）")
        self._clear_alert()

    def notify_disconnected(self) -> None:
        """WS 断开：宽限期后仍未回连才按掉线处理（NapCat 自己也会重连）。"""
        self._connected.clear()
        if self._closed or self._restarting:
            return  # 正常关停 / 我们亲手杀的重启中：不视为异常掉线
        logger.warning(
            f"[nb2-watchdog] 协议端 WS 断开，{self.disconnect_grace_seconds:.0f}s "
            "内未回连将自动恢复"
        )
        if self._grace_task and not self._grace_task.done():
            return

        async def _grace_then_recover() -> None:
            await self._sleep(self.disconnect_grace_seconds)
            if not self._connected.is_set() and not self._closed:
                await self.trigger("ws_disconnect")

        self._grace_task = asyncio.create_task(_grace_then_recover())

    def notify_bot_offline(self, tag: str, message: str) -> None:
        """NapCat bot_offline 事件：登录态失效被踢，直接进恢复流程。"""
        logger.error(f"[nb2-watchdog] 账号被踢下线 [{tag}] {message}")
        self._connected.clear()
        if self._closed:
            return
        self._spawn_recovery(f"bot_offline: {tag} {message}".strip())

    # ==================== 恢复状态机 ====================

    @property
    def _restarting(self) -> bool:
        return self._recover_task is not None and not self._recover_task.done()

    def _spawn_recovery(self, reason: str) -> None:
        if self._restarting:
            logger.info(f"[nb2-watchdog] 恢复流程进行中，忽略重复触发（{reason}）")
            return
        self._recover_task = asyncio.create_task(self.trigger(reason))

    async def trigger(self, reason: str) -> str:
        """执行一次恢复（单 flight）；返回结果码供日志/测试断言。"""
        # 无论从哪条路径进来（事件/宽限/直接调用），跑起来就算 restarting，
        # 保证「我们亲手杀出的 WS 断开」与重复触发都能被识别
        self._recover_task = asyncio.current_task()
        if self._closed or not self.enabled:
            return "disabled"
        now = self._now()
        if now - self._last_attempt < self.cooldown_seconds:
            logger.warning(
                f"[nb2-watchdog] 冷却中（上次重启 {now - self._last_attempt:.0f}s 前），"
                f"忽略本次触发（{reason}）"
            )
            return "cooldown"
        self._attempts = [ts for ts in self._attempts if now - ts <= 86400.0]
        if len(self._attempts) >= self.max_restarts_per_day:
            self._alert(
                "daily_cap",
                f"24h 内已重启 {len(self._attempts)} 次达上限，停止自动恢复（{reason}）",
            )
            return "daily_cap"
        if self._platform != "win32":
            self._alert("not_windows", f"当前平台 {self._platform} 不支持自动恢复（{reason}）")
            return "not_windows"
        if self._napcat_dir is None or self._bot_qq is None:
            self._alert("not_ready", "NapCat 目录或 bot QQ 号未知，无法自动恢复")
            return "not_ready"
        self._last_attempt = now
        self._attempts.append(now)
        logger.warning(
            f"[nb2-watchdog] 开始自动恢复（{reason}），"
            f"24h 内第 {len(self._attempts)}/{self.max_restarts_per_day} 次重启"
        )
        try:
            await asyncio.to_thread(self._kill)
            pid = await asyncio.to_thread(
                self._launch, self._napcat_dir, self._resolve_qq_path(), self._bot_qq
            )
            logger.info(f"[nb2-watchdog] NapCat 已重启（启动器 PID {pid}），等待回连")
        except Exception as error:
            self._alert("restart_failed", f"杀树/重启失败: {error}")
            return "restart_failed"
        try:
            await asyncio.wait_for(self._connected.wait(), self.recover_timeout_seconds)
        except TimeoutError:
            self._alert(
                "recover_timeout",
                f"重启后 {self.recover_timeout_seconds:.0f}s 未回连，"
                "快速登录可能已失效——请检查 NapCat 是否需要重新扫码",
            )
            return "recover_timeout"
        logger.info("[nb2-watchdog] 协议端已回连，自动恢复成功")
        self._clear_alert()
        return "restarted"

    # ==================== 告警（哨兵文件 + ERROR 日志） ====================

    def _alert(self, kind: str, detail: str) -> None:
        logger.error(f"[nb2-watchdog] 需要人工介入 [{kind}] {detail}")
        if self.alert_path is None:
            return
        try:
            self.alert_path.parent.mkdir(parents=True, exist_ok=True)
            self.alert_path.write_text(
                json.dumps(
                    {
                        "kind": kind,
                        "detail": detail,
                        "attempts_24h": len(self._attempts),
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning(f"[nb2-watchdog] 哨兵文件写入失败: {error}")

    def _clear_alert(self) -> None:
        if self.alert_path is not None and self.alert_path.exists():
            with contextlib.suppress(OSError):
                self.alert_path.unlink()
