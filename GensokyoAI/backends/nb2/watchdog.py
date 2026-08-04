"""NapCat 守护：被踢下线 / 断连后的自动恢复（安全清理 → main.bat → 等回连）。

触发源（plugin.py 接线）：
- OneBot `bot_offline` 通知事件（NapCat 把「你的账号当前登录已失效」等
  踢下线包装成该事件推送，见 NapCat 事件文档 BotOfflineEvent）；
- 反向 WS 断开且宽限期后仍未回连（NapCat 进程崩溃/被杀的场景）。

恢复模型（2026-08-02 三轮实机事故后的定稿，用户拍板「别那么麻烦」）：
NapCatWinBootMain 是一次性引导器（拉起 QQ 即退）、QQNT 多开进程互相收养
——**进程枚举根本无法可靠识别 bot 的 QQ，一切基于 pid 的存活/捕获探测
都必然误判**（第三轮「秒退」正是探测逻辑自己造的）。因此：
- 启动 = main.bat 实证内容（`launcher-win10-user.bat <QQ号>` **位置参数**
  ——`-q` 形式 NapCat 4.18.13 不识别），QQ 号由 GSK_NB2_BOT_QQ 配置注入
  （未配则由首次连接的 self_id 兜底）；命令行同步显式 set
  NAPCAT_QUICK_PASSWORD_MD5 / NAPCAT_QUICK_PASSWORD（取自已加载的
  dotenv），登录态作废时 NapCat 自动密码回退重登（可能触发腾讯验证码）；
  QQ 号与目录都不同源时也照常启动（不带账号参数）——NapCat 终端会弹出
  扫码登录，人工扫一次即可，不算故障；
- 清理只做安全集合：按镜像名清 NapCatWinBootMain 引导器树 + 旧实例卡在
  `pause` 的 bat 窗口（绝不盲杀 QQ.exe，个人 QQ 绝对安全；新旧登录冲突
  由 QQ 服务端裁决——新登录踢旧登录）；
- 确认 = 只信 WS 回连（默认等 15 分钟，冷启动实测 6+ 分钟），超时告警。

节制（防无限重启激怒风控）：单 flight、冷却期（被吞触发排到期重试）、
每日重启上限；超限或超时则写哨兵文件 + ERROR 日志告警，停手等人。
探测/清理走 asyncio 子进程（aiosubprocess），非 Windows 平台只告警。
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

# 安全清理集合（绝不盲杀 QQ.exe）：
# - NapCatWinBootMain 引导器树（按镜像名，boot 中的实例）；
# - 旧实例残留在 `pause` 上的 main.bat/launcher 控制台窗口
#   （cmd.exe 命令行含 NapCat.Shell / launcher-win10，卡窗误导用户再开实例互踢）。
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

# 回连等待的轮询间隔（秒）
_RECOVER_POLL_SECONDS = 5.0
# 清理后到启动的沉降等待（秒）：等被杀实例的锁/状态释放
_KILL_SETTLE_SECONDS = 3.0


async def _ps(script: str) -> tuple[int, str]:
    """aiosubprocess 跑一段 PowerShell，返回 (returncode, stdout)。"""
    process = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await process.communicate()
    return process.returncode or 0, out.decode("utf-8", "replace")


async def _windows_kill_aux() -> None:
    """安全清理：引导器树 + pause 残留 bat 窗口（不存在则安静通过）。"""
    await _ps(_KILL_AUX_PS)


def _windows_launch_napcat(napcat_dir: Path, bot_qq: int | None) -> int:
    """按 main.bat 实证内容启动 NapCat（快速登录），返回 cmd 进程 PID。

    命令 = `launcher-win10-user.bat <QQ号>`（**位置参数**——`-q` 形式
    NapCat 4.18.13 不识别（实机日志「没有 -q 指令」），main.bat 的位置
    参数才是被验证的形态）。同时把密码回退变量显式 set 进命令行
    （NAPCAT_QUICK_PASSWORD_MD5 / NAPCAT_QUICK_PASSWORD，取自已加载的
    dotenv 环境）：登录态被风控作废时 NapCat 自动走密码回退重登
    （可能触发腾讯验证码，需人工完成一次）。
    bot_qq 为 None 时不带账号参数启动——NapCat 终端弹出扫码登录，
    人工扫码后正常回连（扫码登录后 notify_connected 会记住 QQ 号）。
    """
    # 经 cmd 先把新控制台代码页切到 UTF-8（bat 里的 chcp 65001 同款），
    # 否则守护拉起的 NapCat 控制台默认 GBK，中文日志全是乱码
    extras = ""
    if bot_qq is not None:
        extras += f"set ACCOUNT={bot_qq}&& "
        quick_md5 = os.environ.get("NAPCAT_QUICK_PASSWORD_MD5", "").strip()
        quick_plain = os.environ.get("NAPCAT_QUICK_PASSWORD", "").strip()
        if quick_md5:
            extras += f"set NAPCAT_QUICK_PASSWORD_MD5={quick_md5}&& "
        elif quick_plain:
            extras += f"set NAPCAT_QUICK_PASSWORD={quick_plain}&& "
    account_arg = f" {bot_qq}" if bot_qq is not None else ""
    process = subprocess.Popen(
        ["cmd", "/c", f"chcp 65001 >nul && {extras}call launcher-win10-user.bat{account_arg}"],
        cwd=napcat_dir,
        # 独立控制台窗口：存活不依赖本进程，日志对用户可见（与手动启动一致）
        creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    return process.pid


class NapCatWatchdog:
    """NapCat 掉线守护（单 flight + 冷却重试 + 每日上限 + WS 回连确认）。

    外部动作全部可注入（kill/launch/sleep/now），测试用假实现验证状态机，
    生产默认 Windows 真实现。
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
        bot_qq: int | None = None,
        kill: Callable[[], Awaitable[None]] = _windows_kill_aux,
        launch: Callable[[Path, int | None], int] = _windows_launch_napcat,
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
        self._sleep = sleep
        self._now = now
        self._platform = platform
        self._napcat_dir: Path | None = None
        self._bot_qq = bot_qq  # 配置注入优先；未配则由首次连接的 self_id 兜底
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
        if self._napcat_dir is None:
            self._alert(
                "not_ready",
                "NapCat 目录未知（配置 GSK_NB2_NAPCAT_DIR），无法自动恢复",
            )
            return "not_ready"
        self._last_attempt = now
        self._attempts.append(now)
        logger.warning(
            f"[nb2-watchdog] 开始自动恢复（{reason}），"
            f"24h 内第 {len(self._attempts)}/{self.max_restarts_per_day} 次重启"
        )
        try:
            # 安全清理（引导器树 + pause 窗口；新旧登录冲突由 QQ 服务端裁决，
            # 新登录会踢掉旧登录）→ 沉降 → 硬编码 launcher 内容快速登录
            await self._kill()
            await self._sleep(_KILL_SETTLE_SECONDS)
            pid = await asyncio.to_thread(self._launch, self._napcat_dir, self._bot_qq)
            if self._bot_qq is None:
                logger.info(
                    f"[nb2-watchdog] NapCat 已重启（PID {pid}，QQ 未知——"
                    "请在 NapCat 终端扫码登录），等待回连"
                )
            else:
                logger.info(
                    f"[nb2-watchdog] NapCat 已重启（PID {pid}，QQ {self._bot_qq}），等待回连"
                )
        except Exception as error:
            self._alert("restart_failed", f"清理/重启失败: {error}")
            return "restart_failed"
        # 回连确认：只信 WS（默认等 15 分钟，冷启动实测 6+ 分钟），超时告警
        deadline = self._now() + self.recover_timeout_seconds
        while not self._connected.is_set():
            if self._now() >= deadline:
                self._alert(
                    "recover_timeout",
                    f"重启后 {self.recover_timeout_seconds:.0f}s 未回连——"
                    "快速登录可能已失效（请检查 NapCat 控制台是否需要扫码），"
                    "或有旧 NapCat/QQ 实例未关闭在互踢",
                )
                return "recover_timeout"
            await self._sleep(_RECOVER_POLL_SECONDS)
        logger.info("[nb2-watchdog] 协议端已回连，自动恢复成功")
        self._clear_alert()
        return "restarted"

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
