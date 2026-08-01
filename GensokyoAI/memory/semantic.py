"""语义记忆 - 话题感知模式，支持可选 embedding 混合检索"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..core.config import MemoryConfig
from ..utils.logger import logger
from .topic_store import TopicAwareStore

if TYPE_CHECKING:
    from ..core.agent.model_client import ModelClient
    from .types import Topic


class SemanticMemoryManager:
    """语义记忆管理器"""

    def __init__(
        self,
        config: MemoryConfig,
        character_id: str,
        memory_path: Path,  # 直接传完整路径
        model_client: ModelClient,
    ):
        self.config = config
        self.character_id = character_id
        self._model_client = model_client
        self._enabled = config.semantic_enabled

        # 直接使用传入的路径
        store_path = memory_path / "topics.json"
        self._store = TopicAwareStore(
            store_path,
            max_topics=config.semantic_max_topics,
            topic_config=config.topic_generation,
            pin_importance=config.topic_pin_importance,
        )

        logger.debug(f"语义记忆初始化: {character_id}, 存储路径: {store_path}")

    async def add_async(
        self,
        content: str,
        importance: float = 0.0,
        emotional_valence: float = 0.0,
        topic_name: str | None = None,
    ) -> Topic | None:
        """添加语义记忆"""
        if not self._enabled:
            return None

        topic = await self._store.add_async(
            content=content,
            importance=importance,
            emotional_valence=emotional_valence,
            model_client=self._model_client,
            topic_name=topic_name,
        )

        if topic:
            logger.debug(f"添加语义记忆: {content[:30]}... -> 话题「{topic.name}」")
            return topic

        return None

    def recent_average_valence(self, limit: int = 3) -> float:
        """最近更新话题的平均情感效价（对话欲信号，§7.3）；无话题或禁用返回 0。"""
        if not self._enabled:
            return 0.0
        topics = self._store.recent_topics(limit)
        if not topics:
            return 0.0
        return sum(topic.emotional_valence for topic in topics) / len(topics)

    def get_relevant_context(self, query: str, top_k: int = 3) -> list[str]:
        """获取相关上下文（同步，兼容旧接口）"""
        if not self._enabled:
            return []

        results = self._store.search(
            query_text=query,
            top_k=top_k,
            threshold=self.config.semantic_similarity_threshold,
            diagnostics={"retrieval_mode": "keyword", "embedding_used": False},
        )

        contexts = []
        for item in results:
            if item.get("importance", 0) > 0.3:
                contexts.append(item.get("content", ""))

        return contexts

    async def get_relevant_context_async(self, query: str, top_k: int = 3) -> list[str]:
        """获取相关上下文（异步）"""
        if not self._enabled:
            return []

        results = await self.search_async(query=query, top_k=top_k)
        return [item.get("content", "") for item in results[:top_k]]

    async def search_async(
        self,
        query: str,
        top_k: int | None = None,
        *,
        threshold: float | None = None,
        include_embedding: bool = True,
    ) -> list[dict]:
        """搜索语义记忆，支持 embedding 可用时的混合检索与失败降级。"""
        if not self._enabled:
            return []

        limit = top_k or self.config.semantic_top_k
        effective_threshold = (
            self.config.semantic_similarity_threshold if threshold is None else threshold
        )
        diagnostics: dict = {
            "retrieval_mode": "keyword",
            "embedding_requested": include_embedding,
            "embedding_used": False,
            "embedding_fallback": False,
            "threshold": effective_threshold,
        }
        query_embedding = None
        memory_embeddings: dict[str, list[float]] = {}

        if include_embedding and getattr(self._model_client, "supports_embeddings", False):
            try:
                query_response = await self._model_client.embeddings(query)
                query_embedding = list(query_response.embedding)

                memories = list(self._store._memories.values())
                if memories:
                    memory_contents = [memory.content for memory in memories]
                    memory_ids = [memory.id for memory in memories]
                    batch_embeddings = await self._model_client.embeddings_batch(memory_contents)
                    memory_embeddings = {
                        memory_id: list(embedding)
                        for memory_id, embedding in zip(memory_ids, batch_embeddings, strict=True)
                        if embedding
                    }

                diagnostics.update(
                    {
                        "retrieval_mode": "hybrid",
                        "embedding_used": bool(query_embedding and memory_embeddings),
                        "embedding_count": len(memory_embeddings),
                    }
                )
            except Exception as error:
                diagnostics.update(
                    {
                        "retrieval_mode": "keyword",
                        "embedding_used": False,
                        "embedding_fallback": True,
                        "embedding_error": str(error),
                    }
                )
                logger.debug(f"语义记忆 embedding 检索降级为关键词检索: {error}")
        elif include_embedding:
            diagnostics["embedding_fallback"] = True
            diagnostics["embedding_error"] = "embedding provider is not configured or not supported"

        return self._store.search(
            query_text=query,
            top_k=limit,
            threshold=effective_threshold,
            query_embedding=query_embedding,
            memory_embeddings=memory_embeddings,
            diagnostics=diagnostics,
        )

    def list_memories(
        self,
        *,
        topic_id: str | None = None,
        topic_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """列出语义记忆。"""
        return self._store.list_memories(
            topic_id=topic_id,
            topic_name=topic_name,
            limit=limit,
            offset=offset,
        )

    def get_memory(self, memory_id: str) -> dict | None:
        """获取单条语义记忆。"""
        return self._store.get_memory(memory_id)

    async def update_memory(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        importance: float | None = None,
        tags: list[str] | None = None,
    ) -> dict | None:
        """更新单条语义记忆。"""
        return await self._store.update_memory(
            memory_id,
            content=content,
            importance=importance,
            tags=tags,
        )

    async def delete_memory(self, memory_id: str) -> bool:
        """删除单条语义记忆。"""
        return await self._store.delete_memory(memory_id)

    def get_topic_graph(self) -> dict:
        """获取话题关联图"""
        return self._store.get_topic_graph()

    @property
    def store(self):
        """暴露存储实例（供工具使用）"""
        return self._store

    @property
    def topic_count(self) -> int:
        return self._store.topic_count

    @property
    def memory_count(self) -> int:
        return self._store.memory_count
