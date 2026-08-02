"""保存协调器 - 管理异步保存的去重和状态"""

# GensokyoAI/core/agent/save_coordinator.py

import asyncio
from typing import TYPE_CHECKING

from ...background import BackgroundManager, TaskPriority
from ...utils.logger import logger

if TYPE_CHECKING:
    from ...core.config import SessionConfig
    from ...memory.working import WorkingMemoryManager
    from ...session.manager import SessionManager


class SaveCoordinator:
    """
    保存协调器 - 管理异步保存的去重和状态

    灵梦：保存这种事，能省则省，但不能不存~
    """

    def __init__(
        self,
        session_manager: SessionManager,
        session_config: SessionConfig,
        label: str | None = None,
    ):
        self._session_manager = session_manager
        self._session_config = session_config
        # 日志租户后缀（Runtime 多租户下区分各租户的同款保存日志）
        self._log_suffix = f" (租户: {label})" if label else ""

        # 状态
        self._last_saved_content_hash: str = ""  # 用内容哈希去重
        self._save_pending = False
        self._last_saved_turn = 0

        # 后台管理器引用
        self._background_manager: BackgroundManager | None = None
        self._bg_started = False
        self._shutting_down = False

    def set_background_manager(self, manager: BackgroundManager) -> None:
        self._background_manager = manager

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = value

    @property
    def save_pending(self) -> bool:
        return self._save_pending

    def reset(self) -> None:
        """重置状态（新会话时调用）"""
        self._save_pending = False
        self._last_saved_turn = 0
        self._last_saved_content_hash = ""

    def _get_content_hash(self, working_memory: WorkingMemoryManager) -> str:
        """计算工作记忆内容的简单哈希"""
        messages = working_memory.get_context()
        # 用消息数量和最后一条内容作为简单哈希
        if not messages:
            return ""
        last_msg = messages[-1]
        return f"{len(messages)}:{last_msg.get('role', '')}:{last_msg.get('content', '')[:50]}"

    def should_save(self, working_memory: WorkingMemoryManager, force: bool = False) -> bool:
        """
        判断是否应该保存

        魔理沙：重要的东西才值得保存DA☆ZE！
        """
        if self._shutting_down:
            logger.debug("正在关闭，跳过普通后台保存")
            return False

        if force:
            return True

        if not self._session_config.auto_save:
            return False

        current_turn = len(working_memory) // 2

        # 轮数没变，不保存
        if current_turn <= self._last_saved_turn:
            return False

        # 内容没变，不保存（更强的去重）
        current_hash = self._get_content_hash(working_memory)
        if current_hash == self._last_saved_content_hash:
            logger.debug("内容未变化，跳过保存")
            return False

        return True

    def mark_saving(self, working_memory: WorkingMemoryManager) -> None:
        """标记正在保存"""
        self._save_pending = True
        self._last_saved_turn = len(working_memory) // 2
        self._last_saved_content_hash = self._get_content_hash(working_memory)

    def mark_saved(self) -> None:
        """标记保存完成"""
        self._save_pending = False

    async def start_background_manager(self) -> None:
        """启动后台管理器"""
        if self._background_manager is None:
            logger.warning("后台管理器未注入")
            return

        if not self._bg_started and not self._shutting_down:
            asyncio.create_task(self._background_manager.start())
            self._bg_started = True
            logger.debug("后台管理器已启动")

    async def save_async(
        self,
        working_memory: WorkingMemoryManager,
        force: bool = False,
    ) -> bool:
        """异步保存"""
        if self._shutting_down:
            logger.debug("正在关闭，拒绝提交后台保存任务；请使用 save_immediately 执行最终保存")
            return False

        if not self._session_config.auto_save and not force:
            return False

        if not self.should_save(working_memory, force=force):
            return False

        current_session = self._session_manager.get_current_session()
        if current_session is None:
            return False

        # 标记正在保存
        self.mark_saving(working_memory)

        # 确保后台管理器启动
        await self.start_background_manager()

        if self._background_manager is None:
            self.mark_saved()
            return False

        messages = working_memory.get_context()

        # 提交持久化任务
        submitted = self._background_manager.submit_persistence_task(
            operation="save_messages",
            data={
                "session_id": current_session.session_id,
                "messages": messages,
            },
            priority=TaskPriority.LOW,
            timeout=10.0,
        )

        if not submitted:
            self.mark_saved()
            logger.warning("保存任务提交失败")
            return False

        logger.debug(f"已提交保存任务 (轮数: {len(messages) // 2}, 消息数: {len(messages)})")
        return True

    async def save_immediately(
        self,
        working_memory: WorkingMemoryManager,
    ) -> bool:
        """立即保存当前工作记忆，不经过后台队列。

        用于关机最终保存。调用返回即表示写入完成或失败，不会再提交后台任务。
        """
        current_session = self._session_manager.get_current_session()
        if current_session is None:
            return False

        self.mark_saving(working_memory)
        try:
            success = await self._session_manager.save_working_memory_async(
                current_session.session_id
            )
            if success:
                logger.info(
                    f"最终保存已完成{self._log_suffix} "
                    f"(轮数: {len(working_memory) // 2}, 消息数: {len(working_memory)})"
                )
            return success
        finally:
            self.mark_saved()
