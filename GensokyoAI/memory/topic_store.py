"""
话题感知的记忆存储 - 比阿求的记忆力还好DA☆ZE！

阿求：我编纂了《幻想乡缘起》，什么都记得！
本模块：我不光记得，还能联想，还会遗忘，还会情感标记！
帕秋莉：姆Q~ 这存储结构比我的图书馆还整齐...

Design Philosophy:
- 紫大人说：边界要模糊，所以话题之间要有重叠
- 幽幽子说：记忆太多会撑死的，所以要遗忘
- 魔理沙说：重要的东西要"借"久一点DA☆ZE~
"""

# GensokyoAI/memory/topic_store.py

import asyncio
import json
import math
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import ayafileio
import msgspec

from ..core.agent.model_client import ModelClient
from ..core.config import TopicGenerationConfig
from ..core.migrations import (
    make_memory_store_payload,
    make_migration_diagnostic,
    migrate_memory_store_payload,
    record_migration_diagnostic,
)
from ..core.schema_versions import MEMORY_SCHEMA_VERSION, MEMORY_STORE_FORMAT
from ..utils.helpers import ensure_utc, utc_now
from ..utils.logger import logger
from .types import Topic, TopicMemory, TopicMemoryType


def _json_encoder(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


_TOKEN_PATTERN = re.compile(r"[\s,，。、；;！!？?·]+")


def _tokenize(text: str) -> set[str]:
    return {word for word in _TOKEN_PATTERN.split(text.lower()) if 2 <= len(word) <= 20}


class TopicAwareStore:
    """
    话题感知存储 - 幻想乡最强记忆体

    能力：
    - 自主命名话题 (比琪露诺聪明多了)
    - 情感标记记忆 (比古明地觉还会读心)
    - 遗忘曲线 (比幽幽子吃得还慢)
    - 话题关联 (比紫的隙间还能连)
    """

    def __init__(
        self, path: Path, max_topics: int = 50, topic_config: TopicGenerationConfig | None = None
    ):
        self.path = Path(path)
        self.max_topics = max_topics
        self.topic_config = topic_config or TopicGenerationConfig()

        self._topics: dict[str, Topic] = {}
        self._memories: dict[str, TopicMemory] = {}

        self._topic_name_index: dict[str, str] = {}
        self._keyword_index: dict[str, set[str]] = defaultdict(set)

        self._lock = asyncio.Lock()
        self._load_sync()

    # ==================== 持久化 ====================

    @property
    def _bak_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    @staticmethod
    def _read_json_file(path: Path) -> dict:
        with open(path, "rb") as f:
            return msgspec.json.decode(f.read())

    def _quarantine_corrupt(self, error: Exception) -> None:
        """把损坏的主文件隔离到 .corrupt-<时间戳>，避免后续保存覆盖犯罪现场。"""
        quarantine = self.path.with_name(
            f"{self.path.name}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        try:
            os.replace(self.path, quarantine)
            logger.warning(f"话题存储文件已隔离: {quarantine}（原因: {error}）")
        except OSError as quarantine_error:
            logger.warning(f"损坏文件隔离失败: {quarantine_error}")

    def _read_payload_with_backup(self) -> dict:
        """读取主文件；损坏时尝试 .bak 恢复并回写主文件，均失败则隔离后抛出。"""
        try:
            return self._read_json_file(self.path)
        except Exception as main_error:
            bak_path = self._bak_path
            if bak_path.exists():
                try:
                    data = self._read_json_file(bak_path)
                except Exception:
                    data = None
                if data is not None:
                    self._quarantine_corrupt(main_error)
                    self._write_sync(data)
                    logger.warning(f"话题存储主文件损坏，已从备份恢复: {self.path}")
                    return data
            self._quarantine_corrupt(main_error)
            raise

    def _load_sync(self) -> None:
        """同步加载"""
        if not self.path.exists():
            return

        migration_failed_recorded = False
        try:
            data = self._read_payload_with_backup()
            from_schema_version = data.get("schema_version") if isinstance(data, dict) else None
            try:
                data, changed = migrate_memory_store_payload(data)
                if changed:
                    self._write_sync(data)
                    record_migration_diagnostic(
                        make_migration_diagnostic(
                            source="memory.topic_store",
                            status="migrated",
                            from_schema_version=(
                                from_schema_version
                                if isinstance(from_schema_version, int)
                                else None
                            ),
                            to_schema_version=MEMORY_SCHEMA_VERSION,
                            format=MEMORY_STORE_FORMAT,
                            path=self.path,
                            backup_path=None,
                            message="Memory topic store migrated to current schema version.",
                        )
                    )
                    logger.info(f"话题存储已迁移到当前 schema: {self.path}")
            except Exception as migration_error:
                migration_failed_recorded = True
                record_migration_diagnostic(
                    make_migration_diagnostic(
                        source="memory.topic_store",
                        status="failed",
                        from_schema_version=(
                            from_schema_version if isinstance(from_schema_version, int) else None
                        ),
                        to_schema_version=MEMORY_SCHEMA_VERSION,
                        format=MEMORY_STORE_FORMAT,
                        path=self.path,
                        backup_path=None,
                        message="Memory topic store migration failed.",
                        diagnostics=[
                            {
                                "code": "migration.memory.topic_store.failed",
                                "path": str(self.path),
                                "severity": "error",
                                "message": str(migration_error),
                                "suggestion": "请检查 topics.json 结构、文件权限或从备份恢复。",
                            }
                        ],
                    )
                )
                raise

            for t_data in data.get("topics", []):
                if "created_at" in t_data and isinstance(t_data["created_at"], str):
                    t_data["created_at"] = ensure_utc(datetime.fromisoformat(t_data["created_at"]))
                if "last_updated" in t_data and isinstance(t_data["last_updated"], str):
                    t_data["last_updated"] = ensure_utc(
                        datetime.fromisoformat(t_data["last_updated"])
                    )
                if "last_accessed" in t_data and isinstance(t_data["last_accessed"], str):
                    t_data["last_accessed"] = ensure_utc(
                        datetime.fromisoformat(t_data["last_accessed"])
                    )
                if "last_thought_at" in t_data and isinstance(t_data["last_thought_at"], str):
                    t_data["last_thought_at"] = ensure_utc(
                        datetime.fromisoformat(t_data["last_thought_at"])
                    )

                topic = Topic(name=t_data.pop("name", "未命名"), **t_data)
                self._topics[topic.id] = topic
                self._topic_name_index[topic.name.lower()] = topic.id
                self._index_topic(topic)

            for m_data in data.get("memories", []):
                if "timestamp" in m_data and isinstance(m_data["timestamp"], str):
                    m_data["timestamp"] = ensure_utc(datetime.fromisoformat(m_data["timestamp"]))
                memory = TopicMemory(content=m_data.pop("content", ""), **m_data)
                self._memories[memory.id] = memory

            logger.debug(f"加载话题存储: {len(self._topics)} 个话题, {len(self._memories)} 条记忆")

        except Exception as e:
            if not migration_failed_recorded:
                record_migration_diagnostic(
                    make_migration_diagnostic(
                        source="memory.topic_store",
                        status="failed",
                        from_schema_version=None,
                        to_schema_version=MEMORY_SCHEMA_VERSION,
                        format=MEMORY_STORE_FORMAT,
                        path=self.path,
                        backup_path=None,
                        message="Memory topic store could not be loaded before migration.",
                        diagnostics=[
                            {
                                "code": "migration.memory.topic_store.load_failed",
                                "path": str(self.path),
                                "severity": "error",
                                "message": str(e),
                                "suggestion": "请检查 topics.json 是否为有效 JSON，或从备份恢复。",
                            }
                        ],
                    )
                )
            logger.warning(f"加载话题存储失败: {e}")

    def _replace_with_backup(self, tmp_path: Path) -> None:
        """原子替换主文件；替换前把旧主文件复制为 .bak（崩溃可回滚）。"""
        if self.path.exists():
            try:
                shutil.copy2(self.path, self._bak_path)
            except OSError as error:
                logger.warning(f"话题存储备份失败（不影响保存）: {error}")
        os.replace(tmp_path, self.path)

    def _write_sync(self, data: dict) -> None:
        """同步原子写入话题存储（tmp + replace），用于启动时迁移与备份恢复。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        json_bytes = msgspec.json.format(
            msgspec.json.encode(data, enc_hook=_json_encoder), indent=2
        )
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        with open(tmp_path, "wb") as f:
            f.write(json_bytes)
        self._replace_with_backup(tmp_path)

    async def _save_async(self) -> None:
        """异步保存（tmp + 原子替换 + .bak，避免崩溃窗口留下半截文件）"""
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)

            data = make_memory_store_payload(
                list(self._topics.values()),
                list(self._memories.values()),
            )

            json_bytes = msgspec.json.format(
                msgspec.json.encode(data, enc_hook=_json_encoder), indent=2
            )

            tmp_path = self.path.with_name(self.path.name + ".tmp")
            async with ayafileio.open(tmp_path, "wb") as f:
                await f.write(json_bytes)
            self._replace_with_backup(tmp_path)

    def _index_topic(self, topic: Topic) -> None:
        """构建关键词索引"""
        text = f"{topic.name} {topic.summary}".lower()
        words = _TOKEN_PATTERN.split(text)

        for word in words:
            if 2 <= len(word) <= 20:
                self._keyword_index[word].add(topic.id)

    def _rebuild_indexes(self) -> None:
        self._topic_name_index.clear()
        self._keyword_index.clear()
        for topic in self._topics.values():
            self._topic_name_index[topic.name.lower()] = topic.id
            self._index_topic(topic)

    # ==================== 遗忘曲线计算 ====================

    def _calculate_recall_weight(self, topic: Topic) -> float:
        """计算话题的回忆权重"""
        # 基础重要性
        base = topic.importance / max(topic.message_count, 1)

        # 古明地觉：你能感受到吗？
        emotional_factor = 1.0 + abs(topic.emotional_valence) * 2.0

        # 提取频率因子
        access_factor = 1.0 + min(topic.access_count / 10.0, 1.0)

        # 时间衰减
        days_since_access = (utc_now() - topic.last_accessed).days
        half_life_days = 30 * emotional_factor * access_factor
        decay = 0.5 ** (days_since_access / half_life_days)

        return base * decay

    def _refresh_topic(self, topic: Topic, boost: float = 0.03) -> None:
        """刷新话题：更新时间戳，微量增加重要性"""
        topic.last_accessed = utc_now()
        topic.access_count = getattr(topic, "access_count", 0) + 1
        topic.importance = min(topic.importance + boost, 10.0)

        logger.debug(f"话题 '{topic.name}' 被刷新，重要性: {topic.importance:.2f}")

    def _snapshot_memories(self) -> list[TopicMemory]:
        """获取当前记忆的浅拷贝快照，避免迭代期间被异步写操作修改。"""
        return list(self._memories.values())

    def _snapshot_topics(self) -> list[Topic]:
        """获取当前话题的浅拷贝快照，避免迭代期间被异步写操作修改。"""
        return list(self._topics.values())

    # ==================== 话题候选 ====================

    def _get_candidates(self, query: str, max_candidates: int = 5) -> list[Topic]:
        """获取候选话题"""
        topics = self._snapshot_topics()
        if not topics:
            return []

        query_lower = query.lower()
        words = _TOKEN_PATTERN.split(query_lower)

        hits: dict[str, int] = defaultdict(int)
        # 对关键词索引做快照，避免迭代时索引被修改
        keyword_index = dict(self._keyword_index)
        for word in words:
            if 2 <= len(word) <= 20 and word in keyword_index:
                for tid in keyword_index[word]:
                    hits[tid] += 1

        candidates = []
        topic_map = {t.id: t for t in topics}
        for tid, _ in sorted(hits.items(), key=lambda x: x[1], reverse=True):
            if tid in topic_map:
                candidates.append(topic_map[tid])
                if len(candidates) >= max_candidates:
                    break

        if len(candidates) < max_candidates:
            recent = sorted(topics, key=lambda t: t.last_updated, reverse=True)
            candidate_set = set(candidates)
            for t in recent:
                if t not in candidate_set:
                    candidates.append(t)
                    candidate_set.add(t)
                    if len(candidates) >= max_candidates:
                        break

        return candidates[:max_candidates]

    # ==================== LLM 评分 ====================

    async def _score_topics(
        self,
        content: str,
        candidates: list[Topic],
        model_client: ModelClient,
    ) -> dict[str, float]:
        """让 LLM 为候选话题打分"""
        if not candidates:
            return {}

        topics_desc = "\n".join(
            f"{i + 1}. 【{t.name}】{t.summary[:80]}" for i, t in enumerate(candidates)
        )

        prompt = f"""判断以下对话内容与各话题的相关性，给每个话题打 0-10 分。

内容：
{content}

话题列表：
{topics_desc}

只返回 JSON，格式：{{"1": 9, "2": 3}}"""

        try:
            response = await model_client.chat(
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.1, "num_predict": 100},
            )

            result_text = response.message.content.strip()  # type: ignore
            json_match = re.search(r"\{[^}]+\}", result_text)

            if json_match:
                scores = json.loads(json_match.group())
                return {
                    candidates[i].id: float(scores.get(str(i + 1), 0))
                    for i in range(len(candidates))
                }

        except Exception as e:
            logger.debug(f"模型打分失败，使用降级方案: {e}")

        return self._fallback_score(content, candidates)

    def _fallback_score(self, content: str, candidates: list[Topic]) -> dict[str, float]:
        """降级方案：基于关键词匹配打分"""
        content_lower = content.lower()
        scores = {}

        for t in candidates:
            score = 0.0
            topic_text = f"{t.name} {t.summary}".lower()

            if t.name.lower() in content_lower:
                score += 5.0

            topic_words = set(_TOKEN_PATTERN.split(topic_text))
            content_words = set(_TOKEN_PATTERN.split(content_lower))
            common = topic_words & content_words
            score += len(common) * 0.5

            scores[t.id] = min(score, 10.0)

        return scores

    # ==================== 添加记忆 ====================

    async def add_async(
        self,
        content: str,
        importance: float = 0.0,
        emotional_valence: float = 0.0,
        model_client: ModelClient | None = None,
        topic_name: str | None = None,
    ) -> Topic | None:
        """
        添加语义记忆！

        - 魔理沙：借来的记忆也要好好保存DA☆ZE！
        - 咲夜：比我的时停还快（异步嘛）
        """
        if not content:
            return None

        memory = TopicMemory(
            content=content,
            importance=importance,
            emotional_impact=abs(emotional_valence),  # 泪目了！
        )
        self._memories[memory.id] = memory

        if topic_name:
            topic_name_lower = topic_name.lower()

            if topic_name_lower in self._topic_name_index:
                topic_id = self._topic_name_index[topic_name_lower]
                topic = self._topics[topic_id]
                self._update_topic(topic, memory, importance, 10.0, emotional_valence)
                self._refresh_topic(topic, boost=0.05)
                await self._save_async()
                logger.debug(f"更新现有话题(由AI指定): {topic.name}")
                return topic

            topic = Topic(
                name=topic_name,
                summary=content[: self.topic_config.summary_max_length],
                importance=importance,
                emotional_valence=emotional_valence,
            )
            topic.message_ids.append(memory.id)
            topic.message_count = 1

            memory.topic_id = topic.id
            memory.tags = [topic_name]

            self._topics[topic.id] = topic
            self._topic_name_index[topic_name_lower] = topic.id
            self._index_topic(topic)

            await self._save_async()
            logger.info(
                f"创建新话题(由AI命名): 「{topic_name}」 (重要性: {importance:.2f}, 情感: {emotional_valence:.2f})"
            )
            return topic

        # 哎呀，话题名就是很难想嘛！
        candidates = self._get_candidates(content)

        if model_client and candidates:
            scores = await self._score_topics(content, candidates, model_client)

            if scores:
                best_id, best_score = max(scores.items(), key=lambda x: x[1])

                # _score_topics 是 LLM 调用（秒级窗口），期间话题可能被并发删除，
                # 直接索引会 KeyError——话题已消失时按无匹配处理，走降级命名。
                best_topic = self._topics.get(best_id)
                if best_score >= 7.0 and best_topic is not None:
                    topic = best_topic
                    self._update_topic(topic, memory, importance, best_score, emotional_valence)
                    self._refresh_topic(topic, boost=0.03)  # 刷新
                    self._update_edges(topic.id, scores)
                    await self._save_async()
                    logger.debug(f"更新话题: {topic.name} (相关性: {best_score:.1f})")
                    return topic

        # 最终降级：生成默认话题名（探测未占用名，避免删过话题后撞名覆盖索引）
        fallback_name = f"话题{len(self._topics) + 1}"
        while fallback_name.lower() in self._topic_name_index:
            fallback_name += "_"
        logger.info(f"使用降级话题名: {fallback_name}")

        topic = Topic(
            name=fallback_name,
            summary=content[: self.topic_config.summary_max_length],
            importance=importance,
            emotional_valence=emotional_valence,
        )
        topic.message_ids.append(memory.id)
        topic.message_count = 1

        memory.topic_id = topic.id
        memory.tags = [fallback_name]

        self._topics[topic.id] = topic
        self._topic_name_index[fallback_name.lower()] = topic.id
        self._index_topic(topic)

        await self._save_async()
        return topic

    def _update_topic(
        self,
        topic: Topic,
        memory: TopicMemory,
        importance: float,
        score: float,
        emotional_valence: float = 0.0,
    ) -> None:
        """更新已有话题"""
        topic.last_updated = utc_now()
        topic.message_count += 1
        topic.importance += importance * (score / 10.0)

        # 情感效价加权平均
        old_weight = topic.message_count - 1
        new_weight = 1
        topic.emotional_valence = (
            topic.emotional_valence * old_weight + emotional_valence * new_weight
        ) / topic.message_count

        topic.message_ids.append(memory.id)

        memory.topic_id = topic.id
        memory.tags = [topic.name]

    def _update_edges(self, topic_id: str, scores: dict[str, float]) -> None:
        """更新话题关联边"""
        topic = self._topics.get(topic_id)
        if not topic:
            return

        for other_id, score in scores.items():
            if other_id == topic_id or score < 4.0:
                continue

            old = topic.related_topics.get(other_id, score)
            topic.related_topics[other_id] = old * 0.7 + score * 0.3

            if other_id in self._topics:
                other = self._topics[other_id]
                old = other.related_topics.get(topic_id, score)
                other.related_topics[topic_id] = old * 0.7 + score * 0.3

    # ==================== 检索 ====================

    def _keyword_memory_score(
        self, query: str, memory: TopicMemory, topic: Topic | None
    ) -> tuple[float, list[str]]:
        query_tokens = _tokenize(query)
        memory_tokens = _tokenize(memory.content)
        topic_tokens = _tokenize(f"{topic.name} {topic.summary}" if topic else "")
        matched_memory = query_tokens & memory_tokens
        matched_topic = query_tokens & topic_tokens
        phrase_match = bool(query and query.lower() in memory.content.lower())

        score = 0.0
        if query_tokens:
            score += len(matched_memory) / len(query_tokens) * 0.55
            score += len(matched_topic) / len(query_tokens) * 0.25
        if phrase_match:
            score += 0.2
        score += min(memory.importance, 1.0) * 0.1
        if topic:
            score += min(self._calculate_recall_weight(topic), 1.0) * 0.1

        matched_by = []
        if matched_memory or phrase_match:
            matched_by.append("memory_keyword")
        if matched_topic:
            matched_by.append("topic_keyword")
        if not matched_by:
            matched_by.append("recent")
        return min(score, 1.0), matched_by

    @staticmethod
    def _cosine_similarity(left: list[float] | None, right: list[float] | None) -> float | None:
        if not left or not right or len(left) != len(right):
            return None
        dot = sum(a * b for a, b in zip(left, right, strict=False))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0 or right_norm == 0:
            return None
        return dot / (left_norm * right_norm)

    def _memory_payload(
        self,
        memory: TopicMemory,
        *,
        topic: Topic | None = None,
        score: float | None = None,
        keyword_score: float | None = None,
        embedding_score: float | None = None,
        matched_by: list[str] | None = None,
        diagnostics: dict | None = None,
    ) -> dict:
        topic = topic or self._topics.get(memory.topic_id)
        payload = {
            "id": memory.id,
            "content": memory.content,
            "importance": memory.importance,
            "emotional_impact": memory.emotional_impact,
            "tags": list(memory.tags),
            "timestamp": memory.timestamp.isoformat(),
            "memory_type": memory.memory_type.name.lower(),
            "supersedes": memory.supersedes,
            "topic_id": memory.topic_id,
            "topic_name": topic.name if topic else None,
            "topic": self._topic_payload(topic) if topic else None,
            "embedding": None,
        }
        if score is not None:
            payload["score"] = score
        if keyword_score is not None:
            payload["keyword_score"] = keyword_score
        if embedding_score is not None:
            payload["embedding_score"] = embedding_score
        if matched_by is not None:
            payload["matched_by"] = matched_by
        if diagnostics is not None:
            payload["diagnostics"] = diagnostics
        return payload

    def _topic_payload(self, topic: Topic | None) -> dict | None:
        if topic is None:
            return None
        return {
            "id": topic.id,
            "name": topic.name,
            "summary": topic.summary,
            "created_at": topic.created_at.isoformat(),
            "last_updated": topic.last_updated.isoformat(),
            "last_accessed": topic.last_accessed.isoformat(),
            "access_count": topic.access_count,
            "message_count": topic.message_count,
            "importance": topic.importance,
            "emotional_valence": topic.emotional_valence,
            "recall_weight": self._calculate_recall_weight(topic),
            "related_topics": dict(topic.related_topics),
            "message_ids": list(topic.message_ids),
            "last_thought_at": topic.last_thought_at.isoformat() if topic.last_thought_at else None,
            "thought_count": topic.thought_count,
        }

    def search(
        self,
        top_k: int = 5,
        query_text: str | None = None,
        *,
        threshold: float = 0.0,
        query_embedding: list[float] | None = None,
        memory_embeddings: dict[str, list[float]] | None = None,
        embedding_weight: float = 0.55,
        refresh: bool = True,
        diagnostics: dict | None = None,
    ) -> list[dict]:
        """搜索记忆，按关键词/话题权重与可选 embedding 相似度排序。"""
        if not query_text or not self._memories:
            return []

        diagnostics = diagnostics or {}
        resolved_memory_embeddings = memory_embeddings or {}
        use_embedding = bool(query_embedding and resolved_memory_embeddings)
        keyword_weight = 1.0 - embedding_weight if use_embedding else 1.0
        candidates: list[dict] = []

        memories = self._snapshot_memories()
        topic_map = {topic.id: topic for topic in self._snapshot_topics()}

        for memory in memories:
            topic = topic_map.get(memory.topic_id)
            keyword_score, matched_by = self._keyword_memory_score(query_text, memory, topic)
            embedding_score = None
            if use_embedding:
                embedding_score = self._cosine_similarity(
                    query_embedding,
                    resolved_memory_embeddings.get(memory.id),
                )
                if embedding_score is not None:
                    matched_by = [*matched_by, "embedding"]

            combined = keyword_score * keyword_weight
            if embedding_score is not None:
                combined += max(0.0, embedding_score) * embedding_weight
            if combined < threshold:
                continue

            candidates.append(
                self._memory_payload(
                    memory,
                    topic=topic,
                    score=combined,
                    keyword_score=keyword_score,
                    embedding_score=embedding_score,
                    matched_by=matched_by,
                    diagnostics=diagnostics,
                )
            )

        candidates.sort(
            key=lambda item: (
                item.get("score", 0.0),
                item.get("importance", 0.0),
                item.get("timestamp", ""),
            ),
            reverse=True,
        )

        for item in candidates[:top_k]:
            item_topic_id = item.get("topic_id")
            topic = topic_map.get(item_topic_id) if isinstance(item_topic_id, str) else None
            if topic and refresh:
                self._refresh_topic(topic, boost=0.01)

        return candidates[:top_k]

    def get_all(self) -> list[dict]:
        """获取所有记忆"""
        return [self._memory_payload(m) for m in self._snapshot_memories()]

    def list_memories(
        self,
        *,
        topic_id: str | None = None,
        topic_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        topic = self.find_topic_by_name(topic_name) if topic_name else None
        selected_topic_id = topic_id or (topic.id if topic else None)
        memories = self._snapshot_memories()
        if selected_topic_id:
            memories = [memory for memory in memories if memory.topic_id == selected_topic_id]
        memories.sort(key=lambda memory: memory.timestamp, reverse=True)
        total = len(memories)
        page = memories[max(offset, 0) : max(offset, 0) + max(limit, 1)]
        return {
            "items": [self._memory_payload(memory) for memory in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def list_topics(self) -> list[dict]:
        return [
            payload for topic in self._snapshot_topics() if (payload := self._topic_payload(topic))
        ]

    def get_memory(self, memory_id: str) -> dict | None:
        memory = self._memories.get(memory_id)
        return self._memory_payload(memory) if memory else None

    async def update_memory(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        importance: float | None = None,
        tags: list[str] | None = None,
    ) -> dict | None:
        memory = self._memories.get(memory_id)
        if memory is None:
            return None
        if content is not None:
            memory.content = content
        if importance is not None:
            memory.importance = max(0.0, min(1.0, float(importance)))
        if tags is not None:
            memory.tags = list(tags)
        await self._save_async()
        return self._memory_payload(memory)

    async def delete_memory(self, memory_id: str) -> bool:
        memory = self._memories.pop(memory_id, None)
        if memory is None:
            return False
        topic = self._topics.get(memory.topic_id)
        if topic and memory_id in topic.message_ids:
            topic.message_ids = [item for item in topic.message_ids if item != memory_id]
            topic.message_count = max(0, topic.message_count - 1)
            topic.last_updated = utc_now()
            if topic.message_count == 0:
                self._topics.pop(topic.id, None)
        self._rebuild_indexes()
        await self._save_async()
        return True

    def get_all_topics(self) -> list[Topic]:
        """获取所有话题（只读快照）"""
        return self._snapshot_topics()

    def find_topic_by_name(self, name: str | None) -> Topic | None:
        """根据话题名查找话题。"""
        if not name:
            return None
        topic_id = self._topic_name_index.get(name.lower())
        if not topic_id:
            return None
        return self._topics.get(topic_id)

    async def update_topic_memory(
        self,
        topic_name: str,
        content: str,
        *,
        importance: float = 0.7,
        score: float = 10.0,
        memory_type: TopicMemoryType = TopicMemoryType.CORRECTION,
    ) -> Topic | None:
        """为指定话题追加一条更新记忆，并返回更新后的话题。"""
        if not topic_name or not content:
            return None

        topic = self.find_topic_by_name(topic_name)
        if topic is None:
            return None

        memory = TopicMemory(
            content=content,
            importance=importance,
            memory_type=memory_type,
            supersedes=topic.message_ids[-1] if topic.message_ids else None,
        )
        self._memories[memory.id] = memory
        self._update_topic(topic, memory, importance, score)
        await self._save_async()
        return topic

    def get_topic_by_id(self, topic_id: str) -> Topic | None:
        """根据 ID 获取话题"""
        return self._topics.get(topic_id)

    def mark_topic_thought(self, topic_id: str) -> bool:
        """标记话题刚刚被思考过，更新思考时间戳和计数。

        返回是否成功找到并更新话题。
        """
        topic = self._topics.get(topic_id)
        if topic is None:
            return False
        topic.last_thought_at = utc_now()
        topic.thought_count = getattr(topic, "thought_count", 0) + 1
        return True

    def get_topic_graph(self) -> dict:
        """获取话题关联图"""
        topics = self._snapshot_topics()
        topic_map = {topic.id: topic for topic in topics}

        nodes = [self._topic_payload(t) for t in topics]

        edges = []
        for t in topics:
            for rid, score in t.related_topics.items():
                if score >= 5.0 and rid in topic_map:
                    edges.append(
                        {
                            "source": t.id,
                            "target": rid,
                            "weight": score,
                        }
                    )

        return {"nodes": nodes, "edges": edges}

    def clear_cache(self) -> None:
        """清空关键词索引缓存"""
        self._keyword_index.clear()

    # ==================== 属性 ====================

    @property
    def topic_count(self) -> int:
        return len(self._topics)

    @property
    def memory_count(self) -> int:
        return len(self._memories)
