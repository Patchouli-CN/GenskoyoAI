"""会话管理器"""

# GensokyoAI\session\manager.py

from copy import deepcopy
from typing import Any
from uuid import uuid4

from ..core.config import SessionConfig
from ..memory.working import WorkingMemoryManager
from ..utils.logger import logger
from .context import SessionContext
from .persistence import SessionPersistence


class SessionManager:
    """会话管理器"""

    def __init__(self, config: SessionConfig, character_id: str, working_max_turns: int = 20):
        self.config = config
        self.character_id = character_id
        self._working_max_turns = working_max_turns
        self._persistence = SessionPersistence(config.save_path)
        self._sessions: dict[str, SessionContext] = {}
        self._current_session_id: str | None = None
        self._working_memories: dict[str, WorkingMemoryManager] = {}

        self._load_sessions()

    def _load_sessions(self) -> None:
        """加载历史会话"""
        sessions = self._persistence.list_sessions(self.character_id)
        for sess in sessions:
            self._sessions[sess.session_id] = sess
            # 加载工作记忆
            messages = self._persistence.load_messages(sess.session_id)
            if messages:
                messages, changed = self._merge_message_model([], messages)
                if changed:
                    self._persistence.save_messages(sess.session_id, messages)
                wm = WorkingMemoryManager(max_turns=self._working_max_turns)
                wm.replace_messages(messages)
                self._working_memories[sess.session_id] = wm
        logger.info(f"加载了 {len(self._sessions)} 个历史会话")

    def create_session(self) -> SessionContext:
        """创建新会话"""
        session = SessionContext(character_id=self.character_id)
        self._sessions[session.session_id] = session
        self._working_memories[session.session_id] = WorkingMemoryManager(
            max_turns=self._working_max_turns
        )
        self._current_session_id = session.session_id
        self._persistence.save_session(session)
        # 保存空消息列表
        self._persistence.save_messages(session.session_id, [])
        logger.info(f"创建会话: {session.session_id}")
        return session

    async def create_session_async(self) -> SessionContext:
        """创建新会话（异步）"""
        session = SessionContext(character_id=self.character_id)
        self._sessions[session.session_id] = session
        self._working_memories[session.session_id] = WorkingMemoryManager(
            max_turns=self._working_max_turns
        )
        self._current_session_id = session.session_id

        # 异步保存
        await self._persistence.save_session_async(session)
        await self._persistence.async_save_message(session.session_id, [])

        logger.info(f"异步创建会话: {session.session_id}")
        return session

    def get_session(self, session_id: str) -> SessionContext | None:
        """获取会话"""
        return self._sessions.get(session_id)

    def get_current_session(self) -> SessionContext | None:
        """获取当前会话"""
        if self._current_session_id:
            return self._sessions.get(self._current_session_id)
        return None

    def set_current_session(self, session_id: str) -> bool:
        """设置当前会话"""
        if session_id in self._sessions:
            self._current_session_id = session_id
            return True
        return False

    def list_sessions(self) -> list[SessionContext]:
        """列出所有会话"""
        return list(self._sessions.values())

    @property
    def persistence(self) -> SessionPersistence:
        """获取会话持久化服务，供需要直接落盘协作的基础设施使用。"""
        return self._persistence

    def get_working_memory(self, session_id: str | None = None) -> WorkingMemoryManager:
        """获取工作记忆"""
        sid = session_id or self._current_session_id
        if not sid:
            raise ValueError("No active session")

        if sid not in self._working_memories:
            # 尝试从持久化加载
            messages = self._persistence.load_messages(sid)
            wm = WorkingMemoryManager(max_turns=self._working_max_turns)
            wm.replace_messages(messages)
            self._working_memories[sid] = wm

        return self._working_memories[sid]

    def save_working_memory(self, session_id: str | None = None) -> None:
        """保存工作记忆到持久化"""
        sid = session_id or self._current_session_id
        if not sid:
            return

        wm = self._working_memories.get(sid)
        if wm:
            messages = wm.get_context()
            previous = self._persistence.load_messages(sid)
            messages, changed = self._merge_message_model(previous, messages)
            if changed:
                wm.replace_messages(messages)
            self._persistence.save_messages(sid, messages)
            logger.debug(f"保存工作记忆: {sid}, {len(messages)} 条消息")

            # 同时更新会话的 total_turns
            session = self._sessions.get(sid)
            if session:
                session.total_turns = len(messages) // 2
                if changed:
                    session.revision += 1
                # 立即保存会话信息
                self._persistence.save_session(session)

    def replace_messages(self, session_id: str, messages: list[dict]) -> bool:
        """全量替换指定会话消息，并同步持久化与工作记忆缓存。

        工作记忆实例必须原地更新而非换新：Agent 侧的 MessageBuilder/
        ResponseHandler/ActionPlanner 在构造期捕获了该实例，换新会让它们
        持有孤儿引用（编辑内容被后续保存静默回滚）。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False

        previous = self._persistence.load_messages(session_id)
        normalized_messages, changed = self._merge_message_model(previous, messages)
        wm = self._working_memories.get(session_id)
        if wm is None:
            wm = WorkingMemoryManager(max_turns=self._working_max_turns)
            self._working_memories[session_id] = wm
        wm.replace_messages(normalized_messages)

        session.total_turns = len(normalized_messages) // 2
        if changed:
            session.revision += 1
            session.touch()
        self._persistence.replace_messages(session_id, normalized_messages)
        self._persistence.save_session(session)
        logger.debug(f"替换会话消息: {session_id}, {len(normalized_messages)} 条")
        return True

    async def save_working_memory_async(self, session_id: str | None = None) -> bool:
        """异步保存工作记忆到持久化，用于关机最终保存等需要等待落盘的场景"""
        sid = session_id or self._current_session_id
        if not sid:
            return False

        wm = self._working_memories.get(sid)
        if not wm:
            return False

        messages = wm.get_context()
        previous = await self._persistence.load_messages_async(sid)
        messages, changed = self._merge_message_model(previous, messages)
        if changed:
            wm.replace_messages(messages)
        await self._persistence.async_save_message(sid, messages)
        logger.debug(f"异步保存工作记忆: {sid}, {len(messages)} 条消息")

        session = self._sessions.get(sid)
        if session:
            session.total_turns = len(messages) // 2
            if changed:
                session.revision += 1
            await self._persistence.save_session_async(session)

        return True

    async def replace_messages_async(self, session_id: str, messages: list[dict]) -> bool:
        """`replace_messages` 的异步变体：消息热路径（幂等 finalize 等）不阻塞事件循环。"""
        session = self._sessions.get(session_id)
        if session is None:
            return False

        previous = await self._persistence.load_messages_async(session_id)
        normalized_messages, changed = self._merge_message_model(previous, messages)
        wm = self._working_memories.get(session_id)
        if wm is None:
            wm = WorkingMemoryManager(max_turns=self._working_max_turns)
            self._working_memories[session_id] = wm
        wm.replace_messages(normalized_messages)

        session.total_turns = len(normalized_messages) // 2
        if changed:
            session.revision += 1
            session.touch()
        await self._persistence.replace_messages_async(session_id, normalized_messages)
        await self._persistence.save_session_async(session)
        logger.debug(f"异步替换会话消息: {session_id}, {len(normalized_messages)} 条")
        return True

    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        if session_id in self._sessions:
            del self._sessions[session_id]
            if session_id in self._working_memories:
                del self._working_memories[session_id]
            self._persistence.delete_session(session_id)
            if self._current_session_id == session_id:
                self._current_session_id = None
            return True
        return False

    def save_current(self) -> None:
        """保存当前会话"""
        if self._current_session_id:
            # 保存工作记忆（会自动更新 total_turns 和保存会话）
            self.save_working_memory()
        logger.debug(f"会话已保存: {self._current_session_id}")

    async def save_current_async(self) -> bool:
        """`save_current` 的异步变体：async RPC 路径不阻塞事件循环。"""
        if not self._current_session_id:
            return False
        saved = await self.save_working_memory_async()
        logger.debug(f"会话已异步保存: {self._current_session_id}")
        return saved

    def assert_revision(self, session_id: str, expected_revision: int | None) -> SessionContext:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Session does not exist: {session_id}")
        if expected_revision is not None and session.revision != expected_revision:
            from GensokyoAI.runtime.rpc import RpcError

            raise RpcError(
                f"Session revision conflict: expected {expected_revision}, current {session.revision}",
                code="session.revision_conflict",
                user_message="会话已被其他请求修改。",
                recoverable=True,
                action_hint="请重新读取 session.messages 后重试。",
                details={
                    "session_id": session_id,
                    "expected_revision": expected_revision,
                    "current_revision": session.revision,
                },
            )
        return session

    @staticmethod
    def _merge_message_model(
        previous: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        previous_by_id = {
            message.get("message_id"): message
            for message in previous
            if isinstance(message.get("message_id"), str)
        }
        normalized: list[dict[str, Any]] = []
        for raw in messages:
            message = deepcopy(dict(raw))
            message_id = message.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                message_id = str(uuid4())
                message["message_id"] = message_id
            old = previous_by_id.get(message_id)
            old_revision = int(old.get("revision", 1)) if old else 0
            comparable = {key: value for key, value in message.items() if key != "revision"}
            old_comparable = (
                {key: value for key, value in old.items() if key != "revision"} if old else None
            )
            message["revision"] = old_revision if comparable == old_comparable else old_revision + 1
            normalized.append(message)
        return normalized, normalized != previous
