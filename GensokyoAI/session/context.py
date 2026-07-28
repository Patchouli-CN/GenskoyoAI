"""会话上下文"""

# GensokyoAI\session\context.py

from datetime import datetime
from typing import Any
from uuid import uuid4

from msgspec import Struct, field

from ..utils.helpers import ensure_utc, utc_now


class SessionContext(Struct):
    """会话上下文"""

    session_id: str = field(default_factory=lambda: str(uuid4()))
    character_id: str = "default"
    created_at: datetime = field(default_factory=utc_now)
    last_active: datetime = field(default_factory=utc_now)
    total_turns: int = 0
    revision: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    is_active: bool = True

    def touch(self) -> None:
        """更新最后活跃时间"""
        self.last_active = utc_now()

    def increment_turns(self) -> None:
        """增加对话轮数"""
        self.total_turns += 1

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "session_id": self.session_id,
            "character_id": self.character_id,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
            "total_turns": self.total_turns,
            "revision": self.revision,
            "metadata": self.metadata,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SessionContext:
        """从字典创建"""
        return cls(
            session_id=data["session_id"],
            character_id=data["character_id"],
            created_at=ensure_utc(datetime.fromisoformat(data["created_at"])),
            last_active=ensure_utc(datetime.fromisoformat(data["last_active"])),
            total_turns=data["total_turns"],
            revision=int(data.get("revision", 0)),
            metadata=data.get("metadata", {}),
            is_active=data.get("is_active", True),
        )
