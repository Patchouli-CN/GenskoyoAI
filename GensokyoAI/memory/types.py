# GensokyoAI/memory/types.py

"""记忆数据类"""

from datetime import datetime
from enum import Enum, auto
from uuid import uuid4

from msgspec import Struct, field

from ..utils.helpers import utc_now


class TopicMemoryType(Enum):
    FACT = auto()
    PREFERENCE = auto()
    EVENT = auto()
    CORRECTION = auto()


class WorkingMemory(Struct):
    """工作记忆 - 当前会话的完整对话"""

    messages: list[dict] = field(default_factory=list)
    max_turns: int = 20

    def get_context(self) -> list[dict]:
        """获取上下文"""
        return self.messages.copy()

    def clear(self) -> None:
        """清空"""
        self.messages.clear()


class Topic(Struct):
    """话题 - 对话的语义聚类单元"""

    name: str  # 无默认值，放最前
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    summary: str = ""
    created_at: datetime = field(default_factory=utc_now)
    last_updated: datetime = field(default_factory=utc_now)
    last_accessed: datetime = field(default_factory=utc_now)  # 最后访问时间
    access_count: int = 0  # 访问次数
    message_count: int = 0
    importance: float = 0.0
    emotional_valence: float = 0.0  # 情感效价
    related_topics: dict[str, float] = field(default_factory=dict)
    message_ids: list[str] = field(default_factory=list)
    last_thought_at: datetime | None = None  # 上次被静默思考的时间
    thought_count: int = 0  # 累计被静默思考次数


class TopicMemory(Struct):
    """话题记忆 - 用于话题检索"""

    content: str  # 无默认值，放最前
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    topic_id: str = ""
    importance: float = 0.0
    emotional_impact: float = 0.0  # 情感冲击力
    tags: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=utc_now)
    memory_type: TopicMemoryType = TopicMemoryType.FACT
    supersedes: str | None = None
