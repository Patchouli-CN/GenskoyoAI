"""保存协调器 - 管理异步保存的去重和状态"""

# GensokyoAI/core/agent/save_coordinator.py

import asyncio
from typing import TYPE_CHECKING, Any

from ...background import BackgroundManager, TaskPriority
from ...background.types import TaskResult
from ...utils.logger import logger
from ...utils.tasks import tracked_task

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
        self._dirty = False  # 保存在途期间又有新内容：完成后补存最新（05#6）
        # 当前在途提交所属的会话：完成回调只认它——会话切换后旧会话的任务结果
        # 不得清掉新会话的 _save_pending（否则「同会话不并发」保证失效）
        self._save_session_id: str | None = None

        # 后台管理器引用
        self._background_manager: BackgroundManager | None = None
        # fire-and-forget 后台任务强引用集合（防 GC 回收，done 自清）
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._bg_started = False
        self._shutting_down = False

    def set_background_manager(self, manager: BackgroundManager) -> None:
        self._background_manager = manager
        manager.on_complete(self._on_save_task_complete)

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
        self._dirty = False
        self._save_session_id = None

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

    def mark_saved(self, *, success: bool = True) -> None:
        """标记保存结束。success=False 时回滚去重状态（05#13）——否则保存/提交失败后
        同一轮后续保存被 should_save 永久跳过。"""
        self._save_pending = False
        self._save_session_id = None
        if not success:
            self._last_saved_turn = 0
            self._last_saved_content_hash = ""

    async def _on_save_task_complete(self, result: TaskResult) -> None:
        """后台保存任务完成回调：失败回滚去重状态；保存期间又有新内容则补存最新。

        （05#6：同会话不再并发双任务/旧快照回退——在途时置脏，完成后补存最新。）
        只处理当前在途提交所属会话的结果：会话切换后到达的旧会话结果直接忽略
        （旧任务自身已落盘，去重状态属于新会话，不得动）。
        """
        operation = result.result.get("operation") if isinstance(result.result, dict) else None
        if operation not in {"save_messages", "save_session"}:
            return
        result_session = result.result.get("session_id") if isinstance(result.result, dict) else None
        if self._save_session_id is None or result_session != self._save_session_id:
            logger.trace(f"忽略非当前提交的保存结果（{result_session}）{self._log_suffix}")
            return
        if not result.success:
            self.mark_saved(success=False)
            logger.warning(
                f"后台保存失败，去重状态已回滚（可重试）{self._log_suffix}: {result.error}"
            )
            return
        self.mark_saved(success=True)
        if self._dirty:
            self._dirty = False
            session = self._session_manager.get_current_session()
            if session is not None:
                working_memory = self._session_manager.get_working_memory(session.session_id)
                tracked_task(self.save_async(working_memory), self._background_tasks)

    async def start_background_manager(self) -> None:
        """启动后台管理器"""
        if self._background_manager is None:
            logger.warning("后台管理器未注入")
            return

        if not self._bg_started and not self._shutting_down:
            tracked_task(self._background_manager.start(), self._background_tasks)
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

        if self._save_pending:
            # 已有保存任务在途：置脏标记，由完成回调补存最新（05#6，防旧快照回退）
            self._dirty = True
            return False

        current_session = self._session_manager.get_current_session()
        if current_session is None:
            return False

        # 标记正在保存
        self.mark_saving(working_memory)
        self._save_session_id = current_session.session_id

        # 确保后台管理器启动
        await self.start_background_manager()

        if self._background_manager is None:
            self.mark_saved(success=False)
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
            self.mark_saved(success=False)
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
        success = False
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
            self.mark_saved(success=success)
