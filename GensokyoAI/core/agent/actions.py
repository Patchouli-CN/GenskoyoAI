"""行动定义 - Agent 可执行的所有行动"""

# GensokyoAI/core/agent/actions.py

from datetime import datetime
from enum import Enum, auto
from typing import Any
from uuid import uuid4

from msgspec import Struct, field

from ...utils.helpers import utc_now


class ActionType(Enum):
    """行动类型"""

    SPEAK = auto()  # 说话
    INITIATIVE_SPEAK = auto()  # 主动说话
    THINK = auto()  # 静默思考
    REMEMBER = auto()  # 记住某事
    RECALL = auto()  # 回忆某事
    WAIT = auto()  # 等待


class ActionPriority(Enum):
    """行动优先级"""

    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


class Action(Struct):
    """行动定义"""

    id: str = field(default_factory=lambda: str(uuid4())[:8])
    type: ActionType = ActionType.WAIT
    priority: ActionPriority = ActionPriority.NORMAL
    content: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    confidence: float = 1.0
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.name,
            "priority": self.priority.name,
            "content": self.content,
            "params": self.params,
            "reason": self.reason,
            "confidence": self.confidence,
        }


class ActionFactory:
    """行动工厂 - 魔理沙：一键生成，比魔法还快DA☆ZE！"""

    @staticmethod
    def speak(reason: str = "") -> Action:
        return Action(type=ActionType.SPEAK, reason=reason)

    @staticmethod
    def initiative_speak(content: str, reason: str = "") -> Action:
        """主动说话行动；content 语义是「待表达意图摘要」，执行时经生成管线产出真正话术。"""
        return Action(
            type=ActionType.INITIATIVE_SPEAK,
            priority=ActionPriority.HIGH,
            content=content,
            reason=reason,
        )

    @staticmethod
    def wait(reason: str = "没什么想说的") -> Action:
        return Action(type=ActionType.WAIT, priority=ActionPriority.LOW, reason=reason)

    @staticmethod
    def remember(content: str, importance: int = 5, topic: str = "") -> Action:
        return Action(
            type=ActionType.REMEMBER,
            content=content,
            params={"importance": importance, "topic": topic},
            reason="需要记住这个信息",
        )

    @staticmethod
    def recall(keyword: str) -> Action:
        return Action(type=ActionType.RECALL, content=keyword, reason=f"回忆 '{keyword}'")
