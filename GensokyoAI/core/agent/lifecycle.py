"""生命周期管理器 - 处理启动、关闭、信号等"""

# GensokyoAI/core/agent/lifecycle.py

import asyncio
import signal
import signal as sig
import sys
from collections.abc import Awaitable, Callable

from ...utils.logger import logger
from ...utils.tasks import tracked_task


class LifecycleManager:
    """
    生命周期管理器 - 处理启动、关闭、信号等

    职责：
    - 设置信号处理器（SIGINT, SIGTERM）
    - 管理关闭状态
    - 优雅关闭流程
    - Windows 平台的信号处理兼容
    """

    def __init__(self, on_shutdown: Callable[[], Awaitable[None]] | None = None):
        """
        初始化生命周期管理器

        Args:
            on_shutdown: 关闭时的回调函数（用于保存数据等）
        """
        self._shutting_down = False
        self._shutdown_event = asyncio.Event()
        self._on_shutdown = on_shutdown
        # fire-and-forget 任务强引用集合（防 GC 回收，done 自清）
        self._background_tasks: set[asyncio.Task] = set()

    # ==================== 状态管理 ====================

    @property
    def is_shutting_down(self) -> bool:
        """是否正在关闭"""
        return self._shutting_down

    @property
    def shutdown_event(self) -> asyncio.Event:
        """关闭事件"""
        return self._shutdown_event

    def set_shutting_down(self, value: bool) -> None:
        """设置关闭状态"""
        self._shutting_down = value

    # ==================== 信号处理 ====================

    def setup_signal_handlers(self) -> None:
        """设置信号处理器"""
        try:
            loop = asyncio.get_running_loop()
            for sig_num in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig_num,
                    lambda s=sig_num: tracked_task(self._handle_signal(s), self._background_tasks),
                )
            logger.debug("信号处理器已设置")
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            logger.debug("当前平台不支持 add_signal_handler")
            self._setup_windows_signal_handler()

    def _setup_windows_signal_handler(self) -> None:
        """Windows 平台的信号处理

        🐛 修复: 不再使用 sys.exit(0) 绕过 finally 清理流程。
        改为抛出 KeyboardInterrupt，让 run_interactive 的异常处理接管，
        经过 finally → stop() → shutdown() → _on_shutdown()，确保数据保存。
        第二次 Ctrl+C 恢复默认信号处理（强制退出）。
        """
        _signal_received = [False]  # 使用列表以便在闭包中修改

        def windows_handler(signum, frame):
            if _signal_received[0]:
                # 第二次信号: 恢复默认处理，强制退出
                sig.signal(signum, sig.SIG_DFL)
                raise KeyboardInterrupt

            _signal_received[0] = True
            logger.info("收到中断信号，正在保存数据...")
            raise KeyboardInterrupt

        sig.signal(signal.SIGINT, windows_handler)
        sig.signal(signal.SIGTERM, windows_handler)

    async def _handle_signal(self, signum: int) -> None:
        """异步处理信号"""
        if self._shutting_down:
            return

        self.set_shutting_down(True)
        signal_name = signal.Signals(signum).name

        logger.info(f"收到 {signal_name} 信号，正在优雅关闭...")

        # 执行关闭回调
        if self._on_shutdown:
            try:
                await self._on_shutdown()
            except Exception as e:
                logger.error(f"关闭回调执行失败: {e}")

        logger.info("正在退出...")
        self._shutdown_event.set()
        sys.exit(0)

    # ==================== 关闭流程 ====================

    async def shutdown(self) -> None:
        """主动关闭"""
        if self._shutting_down:
            return

        self.set_shutting_down(True)

        if self._on_shutdown:
            await self._on_shutdown()

        self._shutdown_event.set()
