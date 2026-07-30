"""情景记忆 - 纯异步版"""

# GensokyoAI\memory\episodic.py

import asyncio

from ..core.agent.model_client import ModelClient
from ..core.agent.prompts import build_episodic_summary_prompt
from ..core.config import MemoryConfig
from ..core.exceptions import MemorySystemError
from ..utils.helpers import utc_now
from ..utils.logger import logger
from .types import EpisodicMemory, MemoryRecord


class EpisodicMemoryManager:
    """情景记忆管理器 - 纯异步"""

    def __init__(
        self,
        config: MemoryConfig,
        character_id: str,
        persistence=None,
        model_client: ModelClient | None = None,
    ):
        self.config = config
        self.character_id = character_id
        self._persistence = persistence
        self._episodes: list[EpisodicMemory] = []
        self._current_episode_messages: list[MemoryRecord] = []
        self._compress_lock = asyncio.Lock()

        self._model_client = model_client

    async def initialize(self) -> None:
        """异步初始化，加载持久化数据"""
        if self._persistence:
            self._episodes = await self._persistence.load_episodes_async(self.character_id)
        logger.info(f"加载了 {len(self._episodes)} 条情景记忆")

    async def add_message(self, record: MemoryRecord) -> None:
        """异步添加消息，触发压缩检查"""
        self._current_episode_messages.append(record)

        if len(self._current_episode_messages) >= self.config.episodic_threshold:
            # 不等待压缩完成，后台执行
            asyncio.create_task(self.compress())

    async def compress(self) -> EpisodicMemory | None:
        """压缩当前消息为情景记忆"""
        async with self._compress_lock:
            if len(self._current_episode_messages) < self.config.episodic_threshold:
                return None

            logger.info(f"开始异步压缩 {len(self._current_episode_messages)} 条消息...")

            keep_recent = self.config.episodic_keep_recent
            to_compress = (
                self._current_episode_messages[:-keep_recent]
                if keep_recent > 0
                else self._current_episode_messages
            )
            recent = self._current_episode_messages[-keep_recent:] if keep_recent > 0 else []

            summary = await self._generate_summary(to_compress)

            episode = EpisodicMemory(
                summary=summary,
                start_time=to_compress[0].timestamp if to_compress else utc_now(),
                end_time=to_compress[-1].timestamp if to_compress else utc_now(),
                message_count=len(to_compress),
                key_events=self._extract_key_events(to_compress),
            )

            self._episodes.append(episode)
            self._current_episode_messages = recent

            if self._persistence:
                asyncio.create_task(
                    self._persistence.save_episode_async(self.character_id, episode)
                )

            logger.info(f"异步压缩完成，生成摘要长度: {len(summary)}")
            return episode

    async def _generate_summary(self, messages: list[MemoryRecord]) -> str:
        """生成对话摘要"""
        conversation = []
        for m in messages:
            role_name = "用户" if m.role == "user" else "助手"
            conversation.append(f"{role_name}: {m.content}")

        text = "\n".join(conversation)

        prompt = build_episodic_summary_prompt(text)

        try:
            if not self._model_client:
                raise MemorySystemError("没有模型客户端！")

            response = await self._model_client.chat(
                messages=[{"role": "user", "content": prompt}],
                model=self.config.episodic_summary_model,
                options={"temperature": 0.3},
            )
            content = response.message.content
            return content.strip() if isinstance(content, str) else ""
        except Exception as e:
            logger.error(f"生成摘要失败: {e}")
            return f"[压缩摘要] 共 {len(messages)} 条消息"

    def _extract_key_events(self, messages: list[MemoryRecord]) -> list[str]:
        """提取关键事件"""
        events = []
        for m in messages:
            if m.importance > 0.7 or len(m.content) > 100:
                events.append(m.content[:100])
        return events[-10:]

    def get_relevant_context(self, query: str = "", max_summaries: int = 3) -> list[str]:
        """获取相关历史摘要（同步方法，供 MessageBuilder 调用）"""
        if not self._episodes:
            return []

        recent = self._episodes[-max_summaries:]
        return [e.summary for e in recent]

    def get_current_context(self) -> list[str]:
        """获取当前未压缩的消息内容"""
        return [m.content for m in self._current_episode_messages]

    @property
    def episode_count(self) -> int:
        """已压缩的情景记忆数量"""
        return len(self._episodes)

    @property
    def pending_message_count(self) -> int:
        """待压缩的消息数量"""
        return len(self._current_episode_messages)
