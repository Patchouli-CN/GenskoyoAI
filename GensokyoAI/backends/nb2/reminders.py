"""到点提醒：存储与调度（nb2 适配器）。

角色经 `set_reminder` 工具接活（"10 分钟后提醒我开会"），提醒落盘持久化
（nb2_data/reminders.json，重启不丢）；插件的 tick 循环每 30 秒扫一次到点
项，让角色用自己的口吻生成提醒文本并 @ 对方投递（见 plugin._fire_reminder）。

时间一律用时区感知的本地 datetime（项目约定：时区感知时间；提醒是「墙上
时钟」语义，本地时区才是人话），落盘存 ISO 8601 字符串（可读、可手改）。

时间解析 `parse_when` 支持：相对（"30秒后/10分钟后/2小时后/1天后"）、
当天/明天/后天时刻（"15:30"、"明天 08:00"）、绝对日期时间
（"2026-08-03 15:30"）——LLM 按工具 docstring 的格式产出，解析是确定性的。
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...utils.logger import logger

# 调度 tick 间隔（秒）：到点精度即 ±30s，对聊天提醒足够
REMINDER_TICK_SECONDS = 30.0
# 到点后投递重试上限（约 20 分钟窗口，覆盖 NapCat 掉线重启的常见时长）；
# 以及落盘时宽容的过期线（重启后逾期超过 24h 的提醒直接作废）
REMINDER_MAX_ATTEMPTS = 40
REMINDER_EXPIRE_AFTER = timedelta(days=1)


def local_now() -> datetime:
    """带本地时区的当前时间（提醒全部用它比较）。"""
    return datetime.now().astimezone()


@dataclass
class Reminder:
    """一条提醒。kind/target_id 决定投递目标（群 @ / 私聊直发）。"""

    id: str
    agent_id: str  # 所属租户（生成提醒文本走该租户的会话，角色记得自己答应过）
    key: str  # SessionStore 键（"group:<群号>" / "user:<QQ号>"）
    kind: str  # "group" | "user"
    target_id: int  # 群号 / 私聊 QQ
    remind_qq: int | None  # 要 @ 的人（None = 不 @，纯文本）
    remind_name: str  # 要提醒的人名（生成文本时给角色看）
    content: str  # 提醒事项
    due: datetime  # 到点时间（时区感知本地时间）
    created_at: datetime
    attempts: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "key": self.key,
            "kind": self.kind,
            "target_id": self.target_id,
            "remind_qq": self.remind_qq,
            "remind_name": self.remind_name,
            "content": self.content,
            "due": self.due.isoformat(),
            "created_at": self.created_at.isoformat(),
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reminder:
        return cls(
            id=str(data["id"]),
            agent_id=str(data["agent_id"]),
            key=str(data["key"]),
            kind=str(data["kind"]),
            target_id=int(data["target_id"]),
            remind_qq=int(data["remind_qq"]) if data.get("remind_qq") is not None else None,
            remind_name=str(data.get("remind_name") or ""),
            content=str(data["content"]),
            due=datetime.fromisoformat(data["due"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            attempts=int(data.get("attempts", 0)),
        )


class ReminderStore:
    """reminders.json：id -> Reminder dict 的持久化映射（原子写，线程安全）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    def add(self, reminder: Reminder) -> None:
        with self._lock:
            self._entries[reminder.id] = reminder.to_dict()
            self._save_locked()

    def due(self, now: datetime) -> list[Reminder]:
        """到点且未超过重试上限的提醒（按到点时间升序）。"""
        with self._lock:
            items = [
                Reminder.from_dict(entry)
                for entry in self._entries.values()
                if datetime.fromisoformat(entry["due"]) <= now
                and entry["attempts"] < REMINDER_MAX_ATTEMPTS
            ]
        return sorted(items, key=lambda item: item.due)

    def mark_done(self, reminder_id: str) -> None:
        with self._lock:
            if reminder_id in self._entries:
                del self._entries[reminder_id]
                self._save_locked()

    def bump_attempts(self, reminder_id: str) -> int:
        """投递失败计数 +1，返回当前次数（达到上限由调用方记日志放弃）。"""
        with self._lock:
            entry = self._entries.get(reminder_id)
            if entry is None:
                return REMINDER_MAX_ATTEMPTS
            entry["attempts"] += 1
            self._save_locked()
            return int(entry["attempts"])

    def pending_count(self, agent_id: str) -> int:
        with self._lock:
            return sum(1 for entry in self._entries.values() if entry["agent_id"] == agent_id)

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"[nb2] 提醒文件损坏，已从空表重新开始: {error}")
            return
        if not isinstance(raw, dict):
            return
        now = local_now()
        for key, entry in raw.items():
            try:
                reminder = Reminder.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                continue
            if now - reminder.due > REMINDER_EXPIRE_AFTER:
                logger.info(f"[nb2] 过期提醒已作废（逾期超 24h）: {reminder.content[:30]}")
                continue
            reminder.attempts = 0  # 新进程给新一轮投递机会
            self._entries[str(key)] = reminder.to_dict()
        if self._entries:
            logger.info(f"[nb2] 已恢复 {len(self._entries)} 条待办提醒")

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, self._path)


# ==================== 时间解析 ====================

_RELATIVE_PATTERN = re.compile(
    r"^\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>秒|分钟|分|小时|天)\s*(?:后|之后)?\s*$"
)
_CLOCK_PATTERN = re.compile(r"^(?P<hour>\d{1,2})[:：](?P<minute>\d{1,2})(?:[:：]\d{1,2})?\s*$")
_DATE_TIME_PATTERN = re.compile(
    r"^\s*(?P<year>\d{4})[-/年](?P<month>\d{1,2})[-/月](?P<day>\d{1,2})[日号]?\s+"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{1,2})"
)
_DAY_OFFSETS = {"今天": 0, "明天": 1, "明日": 1, "后天": 2}
_UNIT_SECONDS = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "天": 86400}


def parse_when(when: str, now: datetime) -> datetime | None:
    """把 LLM 产出的时间描述解析为时区感知 datetime；无法解析返回 None。

    - 相对："30秒后" / "10分钟后" / "2小时后" / "1天后"（支持小数）；
    - 时刻："15:30"（今天，已过则明天）、"明天 08:00"、"后天 7:30"；
    - 绝对："2026-08-03 15:30"（按 now 的时区）。
    """
    text = when.strip()
    if not text:
        return None
    if match := _RELATIVE_PATTERN.match(text):
        seconds = float(match.group("num")) * _UNIT_SECONDS[match.group("unit")]
        return now + timedelta(seconds=seconds)
    if match := _DATE_TIME_PATTERN.match(text):
        try:
            return datetime(
                int(match.group("year")), int(match.group("month")),
                int(match.group("day")), int(match.group("hour")),
                int(match.group("minute")), tzinfo=now.tzinfo,
            )
        except ValueError:
            return None
    day_offset = 0
    for word, offset in _DAY_OFFSETS.items():
        if text.startswith(word):
            day_offset = offset
            text = text[len(word):].strip()
            break
    if match := _CLOCK_PATTERN.match(text):
        try:
            candidate = now.replace(
                hour=int(match.group("hour")), minute=int(match.group("minute")),
                second=0, microsecond=0,
            )
        except ValueError:  # "25:30" / "12:70" 这类非法时刻
            return None
        candidate += timedelta(days=day_offset)
        if candidate <= now:  # 时刻已过 → 顺延一天
            candidate += timedelta(days=1)
        return candidate
    return None
