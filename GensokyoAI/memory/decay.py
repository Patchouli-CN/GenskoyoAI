"""话题热度淘汰 - 读取时惰性衰减，隐藏而非删除（参考 Lumi_Nox memory/decay.py）

时间戳持久保存，热度在读取时现算，无需后台衰减任务；
被隐藏的话题不会被删除，后续对话刷新其时间戳后自然复活。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..utils.helpers import utc_now

if TYPE_CHECKING:
    from .types import Topic

DEFAULT_HALF_LIFE_HOURS = 72.0
DEFAULT_PRUNE_THRESHOLD = 0.1
DEFAULT_PIN_IMPORTANCE = 8.0


def topic_heat(
    topic: Topic,
    now: datetime | None = None,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> float:
    """话题当前热度（0..1）：以最后活跃时间（更新/访问取较新者）按半衰期指数衰减。"""
    if half_life_hours <= 0:
        return 1.0
    now = now or utc_now()
    last_active = max(topic.last_updated, topic.last_accessed)
    age_hours = (now - last_active).total_seconds() / 3600.0
    if age_hours <= 0:
        return 1.0
    return 0.5 ** (age_hours / half_life_hours)


def is_pinned(topic: Topic, pin_importance: float = DEFAULT_PIN_IMPORTANCE) -> bool:
    """重要性达到 pin 阈值的话题免疫衰减——反复被强化的记忆已是角色的一部分。"""
    return topic.importance >= pin_importance


def filter_active_topics(
    topics: list[Topic],
    now: datetime | None = None,
    *,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    prune_threshold: float = DEFAULT_PRUNE_THRESHOLD,
    pin_importance: float = DEFAULT_PIN_IMPORTANCE,
) -> list[Topic]:
    """保留 pinned 或热度达标的话题（维持输入顺序）；冷话题隐藏而非删除。"""
    now = now or utc_now()
    return [
        topic
        for topic in topics
        if is_pinned(topic, pin_importance)
        or topic_heat(topic, now, half_life_hours) >= prune_threshold
    ]
