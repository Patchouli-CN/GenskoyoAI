"""NapCat 守护：被踢下线 / 断连后的自动恢复（精确杀 → 快速登录 → 确认回连）。

触发源（plugin.py 接线）：
- OneBot `bot_offline` 通知事件（NapCat 把「你的账号当前登录已失效」等
  踢下线包装成该事件推送，见 NapCat 事件文档 BotOfflineEvent）；
- 反向 WS 断开且宽限期后仍未回连（NapCat 进程崩溃/被杀的场景）。

进程模型（2026-08-02 两轮实机事故后的定稿）：NapCatWinBootMain 是一次性
引导器（拉起 QQ 即退），QQNT 多开时进程还会互相收养——**靠进程枚举无法
可靠区分哪个 QQ.exe 是 bot 的**。因此守护只精确管理自己拉起的实例：
启动后在孵化期捕获 QQ 子进程 pid 并持久化（napcat_bot.json），杀的时候
`taskkill /pid <qq_pid> /t` 精确收口（外加按镜像名清引导器树与 pause
残留的 bat 窗口，绝不盲杀 QQ.exe 误伤个人 QQ）。无追踪记录的外来实例
（如用户手动 main.bat 起的）不做盲杀盲启——此前正是这样撞车闪退。

节制（防无限重启激怒风控）：单 flight、冷却期（被吞触发排到期重试）、
每日重启上限；超限或回连超时则写哨兵文件 + ERROR 日志告警，停手等人。
所有进程探测走 asyncio 子进程（aiosubprocess），非 Windows 平台只告警。
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

# 杀 bot 实例的安全集合（绝不盲杀 QQ.exe）：
# - NapCatWinBootMain 引导器树（按镜像名，boot 中的实例）；
# - 旧实例残留在 `pause` 上的 main.bat/launcher 控制台窗口
#   （cmd.exe 命令行含 NapCat.Shell / launcher-win10，卡窗误导用户再开实例互踢）；
# 追踪到的 bot QQ 本体由调用方先 taskkill /pid /t（见 _windows_kill_bot）。
_KILL_AUX_PS = (
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
    " $bats = @($all | Where-Object { $_.Name -eq 'cmd.exe' -and"
    " $_.CommandLine -match 'NapCat\\.Shell|launcher-win10' });"
    " foreach ($b in $bats) { Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue }"
)

_FIND_CHILD_QQ_PS = (
    "$all = Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\";"
    " $child = @($all | Where-Object ParentProcessId -eq {pid})"
    " | Select-Object -First 1; if ($child) {{ $child.ProcessId }}"
)

_QQ_UNINSTALL_KEY = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"

# 回连确认的轮询间隔（秒）：WS 回连为主、进程存活为辅
_RECOVER_POLL_SECONDS = 5.0
# 启动孵化期（秒）：期内捕获 launcher 的 QQ 子进程（后续精确管理的把手）；
# 期内 launcher 死了且没见到 QQ = 秒退，自动重试一次
_SPAWN_GRACE_SECONDS = 30.0
# 精确杀后的死透等待（秒）：QQNT 单实例锁未释放时立刻重启会撞车
_KILL_WAIT_DEAD_SECONDS = 15.0


async def _ps(script: str) -> tuple[int, str]:
    """aiosubprocess 跑一段 PowerShell，返回 (returncode, stdout)。"""
    process = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await process.communicate()
    return process.returncode or 0, out.decode("utf-8", "replace")


async def _windows_pid_alive(pid: int) -> bool:
    """指定 PID 是否还活着。"""
    rc, _ = await _ps(
        f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}"
    )
    return rc == 0


async def _windows_find_child_qq(launcher_pid: int) -> int | None:
    """找 launcher 的 QQ.exe 子进程（守护拉起实例的 bot QQ 把手）。"""
    _, out = await _ps(_FIND_CHILD_QQ_PS.format(pid=launcher_pid))
    text = out.strip()
    return int(text) if text.isdigit() else None


async def _windows_kill_bot(qq_pid: int | None) -> None:
    """精确杀 bot 实例：追踪到的 QQ 本体（/t 带子树）+ 引导器树 + pause 残留窗口，
    并等追踪目标死透（QQNT 单实例锁未释放时立刻重启会撞车闪退）。"""
    if qq_pid is not None:
        await _ps(f"taskkill /pid {qq_pid} /t /f")
    await _ps(_KILL_AUX_PS)
    if qq_pid is not None:
        deadline = time.monotonic() + _KILL_WAIT_DEAD_SECONDS
        while time.monotonic() < deadline and await _windows_pid_alive(qq_pid):
            await asyncio.sleep(0.5)


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


def _windows_launch_napcat(napcat_dir: Path, qq_path: Path, bot_qq: int) -> int:
    """按 launcher 脚本同等环境启动 NapCat（快速登录），返回引导器 PID。"""
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
    # 经 cmd 先把新控制台代码页切到 UTF-8（launcher bat 里的 chcp 65001 同款），
    # 否则守护拉起的 NapCat 控制台默认 GBK，中文日志全是乱码
    command = (
        f'chcp 65001 >nul && "{env["NAPCAT_LAUNCHER_PATH"]}" '
        f'"{qq_path}" "{env["NAPCAT_INJECT_PATH"]}" {bot_qq}'
    )
    process = subprocess.Popen(
        ["cmd", "/c", command],
        cwd=napcat_dir,
        env=env,
        # 独立控制台窗口：存活不依赖本进程，日志对用户可见（与手动启动一致）
        creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    return process.pid


class NapCatWatchdog:
    """NapCat 掉线守护（单 flight + 冷却重试 + 每日上限 + 精确进程管理）。

    外部动作全部可注入（kill/launch/pid_alive/find_child_qq/resolve_qq_path/
    sleep/now），测试用假实现验证状态机，生产默认 Windows 真实现。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        cooldown_seconds: float = 600.0,
        max_restarts_per_day: int = 5,
        recover_timeout_seconds: float = 900.0,
        disconnect_grace_seconds: float = 60.0,
        alert_path: Path | None = None,
        state_path: Path | None = None,
        kill: Callable[[int | None], Awaitable[None]] = _windows_kill_bot,
        launch: Callable[[Path, Path, int], int] = _windows_launch_napcat,
        pid_alive: Callable[[int], Awaitable[bool]] = _windows_pid_alive,
        find_child_qq: Callable[[int], Awaitable[int | None]] = _windows_find_child_qq,
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
        self.state_path = state_path
        self._kill = kill
        self._launch = launch
        self._pid_alive = pid_alive
        self._find_child_qq = find_child_qq
        self._resolve_qq_path = resolve_qq_path
        self._sleep = sleep
        self._now = now
        self._platform = platform
        self._napcat_dir: Path | None = None
        self._bot_qq: int | None = None
        self._attempts: list[float] = []  # 近 24h 重启时间戳
        self._last_attempt = 0.0
        self._trigger_active = False  # 恢复流程进行中（单 flight 旗标，不依赖任务身份）
        self._recover_task: asyncio.Task[Any] | None = None
        self._grace_task: asyncio.Task[None] | None = None
        self._cooldown_retry_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._closed = False

    def configure(self, *, napcat_dir: Path) -> None:
        self._napcat_dir = napcat_dir

    def close(self) -> None:
        """进程退出时停用守护（防止适配器正常关停时反而把 NapCat 拉起来）。"""
        self._closed = True
        for task in (self._recover_task, self._grace_task, self._cooldown_retry_task):
            if task and not task.done():
                task.cancel()

    # ==================== 事件入口（plugin 接线） ====================

    def notify_connected(self, bot_qq: int) -> None:
        """协议端回连（driver.on_bot_connect）：记 QQ 号、解除离线、清哨兵。"""
        was_offline = not self._connected.is_set()
        self._bot_qq = bot_qq
        self._connected.set()
        for task in (self._grace_task, self._cooldown_retry_task):
            if task and not task.done():
                task.cancel()
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
        return self._trigger_active

    def _spawn_recovery(self, reason: str) -> None:
        if self._restarting:
            logger.info(f"[nb2-watchdog] 恢复流程进行中，忽略重复触发（{reason}）")
            return
        # 立即占旗（而不是等任务起跑）：连续两个事件在同一拍到达时，
        # 第二个必须看到「进行中」
        self._trigger_active = True
        self._recover_task = asyncio.create_task(self._run_trigger(reason))

    async def trigger(self, reason: str) -> str:
        """执行一次恢复（单 flight）；返回结果码供日志/测试断言。"""
        if self._trigger_active:
            logger.info(f"[nb2-watchdog] 恢复流程进行中，忽略并发触发（{reason}）")
            return "inflight"
        self._trigger_active = True
        return await self._run_trigger(reason)

    async def _run_trigger(self, reason: str) -> str:
        try:
            return await self._trigger_inner(reason)
        finally:
            self._trigger_active = False

    async def _trigger_inner(self, reason: str) -> str:
        if self._closed or not self.enabled:
            return "disabled"
        now = self._now()
        if now - self._last_attempt < self.cooldown_seconds:
            remaining = self.cooldown_seconds - (now - self._last_attempt)
            logger.warning(
                f"[nb2-watchdog] 冷却中（上次重启 {now - self._last_attempt:.0f}s 前），"
                f"{remaining:.0f}s 后自动重试（{reason}）"
            )
            self._schedule_cooldown_retry(remaining, reason)
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
            # 精确杀：追踪到的 bot QQ 本体（无追踪记录则只清引导器树与
            # pause 窗口——绝不盲杀 QQ.exe；外来实例冲突由孵化期检出并告警）
            tracked = self._load_tracked()
            await self._kill(tracked.get("qq_pid") if tracked else None)
            pid = await asyncio.to_thread(
                self._launch, self._napcat_dir, self._resolve_qq_path(), self._bot_qq
            )
            logger.info(f"[nb2-watchdog] NapCat 已重启（引导器 PID {pid}），等待回连")
        except Exception as error:
            self._alert("restart_failed", f"杀树/重启失败: {error}")
            return "restart_failed"
        # 孵化期：捕获 QQ 子进程 pid（持久化，后续精确管理的把手）；
        # launcher 死了且没见到 QQ = 秒退（外来实例冲突是已观察到的成因），
        # 自动重试一次，仍秒退才告警
        relaunched = False
        while True:
            qq_pid = await self._capture_qq_pid(pid)
            if qq_pid is not None:
                self._save_tracked(qq_pid=qq_pid, launcher_pid=pid)
                break
            if await self._pid_alive(pid):
                break  # launcher 还活着但 QQ 未现身（慢起）：以 WS 回连为准
            if relaunched:
                self._alert(
                    "process_died",
                    f"NapCat 启动后秒退（引导器 PID {pid}），重试仍闪退——"
                    "可能有未关闭的旧 NapCat/QQ 实例冲突，请手动检查并关闭后"
                    "（或直接手动运行 main.bat），回连后本告警自动清除",
                )
                return "process_died"
            relaunched = True
            logger.warning("[nb2-watchdog] 启动后秒退，杀树后自动重试一次")
            try:
                tracked = self._load_tracked()
                await self._kill(tracked.get("qq_pid") if tracked else None)
                pid = await asyncio.to_thread(
                    self._launch, self._napcat_dir, self._resolve_qq_path(), self._bot_qq
                )
            except Exception as error:
                self._alert("restart_failed", f"秒退重试失败: {error}")
                return "restart_failed"
        # 回连确认：WS 回连为主（默认等 15 分钟，冷启动实测 6+ 分钟）；
        # 已捕获的 QQ 中途死亡则提前判死
        deadline = self._now() + self.recover_timeout_seconds
        watched_pid = qq_pid if qq_pid is not None else pid
        while not self._connected.is_set():
            if self._now() >= deadline:
                self._alert(
                    "recover_timeout",
                    f"重启后 {self.recover_timeout_seconds:.0f}s 未回连，"
                    "快速登录可能已失效——请检查 NapCat 是否需要重新扫码",
                )
                return "recover_timeout"
            if not await self._pid_alive(watched_pid):
                self._alert(
                    "process_died",
                    f"NapCat 进程已退出（PID {watched_pid}）——"
                    "请查看 NapCat 控制台/日志确认原因",
                )
                return "process_died"
            await self._sleep(_RECOVER_POLL_SECONDS)
        logger.info("[nb2-watchdog] 协议端已回连，自动恢复成功")
        self._clear_alert()
        return "restarted"

    async def _capture_qq_pid(self, launcher_pid: int) -> int | None:
        """孵化期内轮询捕获 launcher 的 QQ 子进程 pid；launcher 先死则放弃。"""
        deadline = self._now() + _SPAWN_GRACE_SECONDS
        while self._now() < deadline:
            qq_pid = await self._find_child_qq(launcher_pid)
            if qq_pid is not None:
                return qq_pid
            if not await self._pid_alive(launcher_pid):
                return None
            if self._connected.is_set():
                return None  # 已回连但没捕获到（收养场景）：以 WS 为准
            await self._sleep(2.0)
        return None

    def _schedule_cooldown_retry(self, delay: float, reason: str) -> None:
        """冷却期拒绝的触发排一个到期重试（仍离线才重试，回连/关停自动取消）。

        从 retry 任务自身里再次排期是被允许的（retry 醒来后仍处冷却期时
        续排下一轮）——否则重试链会在 retry 任务「未完成」的自我占用下断掉。
        """
        current = asyncio.current_task()
        if (
            self._cooldown_retry_task is not None
            and not self._cooldown_retry_task.done()
            and self._cooldown_retry_task is not current
        ):
            return  # 别的任务已排期，不重复

        async def _retry() -> None:
            await self._sleep(delay)
            if not self._connected.is_set() and not self._closed:
                self._spawn_recovery(f"{reason}（冷却期满重试）")

        self._cooldown_retry_task = asyncio.create_task(_retry())

    # ==================== 追踪状态（napcat_bot.json） ====================

    def _load_tracked(self) -> dict[str, Any] | None:
        """读追踪记录（守护拉起的 bot QQ pid）；没有/损坏返回 None。"""
        if self.state_path is None or not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and data.get("qq_pid") else None

    def _save_tracked(self, *, qq_pid: int, launcher_pid: int) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {
                        "qq_pid": qq_pid,
                        "launcher_pid": launcher_pid,
                        "bot_qq": self._bot_qq,
                        "launched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning(f"[nb2-watchdog] 追踪状态写入失败: {error}")

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
