"""到点提醒：存储与调度（nb2 适配器）。

登记通道只有一条：AttentionThings 注意力管线（判定全权交给 LLM，
ThinkEngine 范式——本模块内**不允许出现任何形式判断代码**，时间一律
以判定输出的 ISO 8601 绝对时间入库）。到点由 tick 循环让角色用自己的
口吻生成提醒文本并 @ 对方投递（见 plugin._fire_reminder）。

时间一律用时区感知的本地 datetime（项目约定：时区感知时间；提醒是「墙上
时钟」语义，本地时区才是人话），落盘存 ISO 8601 字符串（可读、可手改）。
"""

from __future__ import annotations

import json
import os
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


def _as_aware(dt: datetime) -> datetime:
    """无时区的 datetime 视为本地时间补时区（手改 JSON 存了 naive 串也不崩 aware/naive 相减）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=local_now().tzinfo)
    return dt


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
            due=_as_aware(datetime.fromisoformat(data["due"])),
            created_at=_as_aware(datetime.fromisoformat(data["created_at"])),
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
        """到点且未超过重试上限的提醒（按到点时间升序）。

        顺带清理重试耗尽的僵尸提醒：它们不会再被返回，若留着会永久
        计入 pending_count 堵满租户配额（手改文件/旧版本残留可达）。
        """
        with self._lock:
            exhausted = [
                key
                for key, entry in self._entries.items()
                if entry["attempts"] >= REMINDER_MAX_ATTEMPTS
            ]
            for key in exhausted:
                logger.info(
                    f"[nb2] 重试耗尽的提醒已清理: {self._entries[key].get('content', '')[:30]}"
                )
                del self._entries[key]
            if exhausted:
                self._save_locked()
            items: list[Reminder] = []
            for entry in self._entries.values():
                try:
                    due = _as_aware(datetime.fromisoformat(entry["due"]))
                except (KeyError, TypeError, ValueError):
                    # 手改 JSON 混入非法 due：跳过不崩，坏条目不占位也不删（下次还来，
                    # 但绝不拖垮整个 due()/tick）
                    logger.warning(f"[nb2] 坏提醒条目已跳过（due 非法）: {entry.get('content', '')[:30]}")
                    continue
                if due <= now:
                    items.append(Reminder.from_dict(entry))
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

    def pending(self, agent_id: str) -> list[Reminder]:
        """某租户全部待办提醒（按到点时间升序）。"""
        with self._lock:
            items = [
                Reminder.from_dict(entry)
                for entry in self._entries.values()
                if entry["agent_id"] == agent_id
            ]
        return sorted(items, key=lambda item: item.due)

    def cancel_latest(self, agent_id: str) -> Reminder | None:
        """取消某租户最近创建的一条待办提醒（用户定稿的取消机制）。"""
        with self._lock:
            candidates = [
                entry
                for entry in self._entries.values()
                if entry["agent_id"] == agent_id
            ]
            if not candidates:
                return None
            latest = max(candidates, key=lambda entry: entry["created_at"])
            reminder = Reminder.from_dict(latest)
            del self._entries[reminder.id]
            self._save_locked()
        logger.info(f"[nb2] {agent_id} 提醒已取消: {reminder.content[:30]}")
        return reminder

    def cancel_all(self, agent_id: str) -> list[Reminder]:
        """取消某租户全部待办提醒，返回被取消的列表。"""
        with self._lock:
            ids = [
                key
                for key, entry in self._entries.items()
                if entry["agent_id"] == agent_id
            ]
            items = [Reminder.from_dict(self._entries.pop(key)) for key in ids]
            if items:
                self._save_locked()
        if items:
            logger.info(f"[nb2] {agent_id} 全部 {len(items)} 条提醒已取消")
        return items

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

