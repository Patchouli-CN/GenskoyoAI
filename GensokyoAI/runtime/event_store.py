"""Bounded persistent Runtime event log for replayable transports."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from pathlib import Path
from typing import Any
from uuid import uuid4

from GensokyoAI.utils.helpers import utc_now
from GensokyoAI.utils.logger import logger


class RuntimeEventStore:
    """Append-only event store scoped to one tenant Agent."""

    def __init__(self, path: Path, *, max_events: int = 10_000) -> None:
        self.path = path
        self.max_events = max(100, max_events)
        self._events: deque[dict[str, Any]] = deque(maxlen=self.max_events)
        self._sequence = 0
        self._overflow_appends = 0
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as file:
                for line in file:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and isinstance(event.get("sequence"), int):
                        self._events.append(event)
                        self._sequence = max(self._sequence, event["sequence"])
        except OSError:
            # 坏文件改名留证：否则每次启动读同一坏文件、清空重来，append 还往坏文件
            # 里写混合数据（旧 CODE_INF 03#9）
            logger.warning(f"[event_store] 事件日志读取失败，坏文件改名留证: {self.path}")
            with contextlib.suppress(OSError):
                self.path.rename(self.path.with_suffix(self.path.suffix + ".corrupted"))
            self._events.clear()
            self._sequence = 0

    async def append(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._sequence += 1
            event = {
                **payload,
                "event_id": str(uuid4()),
                "sequence": self._sequence,
                "recorded_at": utc_now().isoformat(),
            }
            was_full = len(self._events) == self.max_events
            self._events.append(event)
            self._overflow_appends = self._overflow_appends + 1 if was_full else 0
            rewrite = was_full and self._overflow_appends >= 1000
            await asyncio.to_thread(self._persist, event, rewrite)
            if rewrite:
                self._overflow_appends = 0
            return dict(event)

    async def replay(
        self,
        *,
        after_sequence: int = 0,
        event_types: set[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if after_sequence < 0:
            raise ValueError("Runtime after_sequence must be greater than or equal to 0")
        # limit=0 语义为「不回放」（切片 [:0] 天然返回空）
        if not 0 <= limit <= 1000:
            raise ValueError("Runtime replay limit must be between 0 and 1000")
        async with self._lock:
            return [
                dict(event)
                for event in self._events
                if event["sequence"] > after_sequence
                and (event_types is None or event.get("type") in event_types)
            ][:limit]

    @property
    def earliest_sequence(self) -> int | None:
        return self._events[0]["sequence"] if self._events else None

    @property
    def latest_sequence(self) -> int:
        return self._sequence

    def _persist(self, event: dict[str, Any], rewrite: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if rewrite:
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(temporary, "w", encoding="utf-8", newline="\n") as file:
                for stored in self._events:
                    file.write(json.dumps(stored, ensure_ascii=False, default=str) + "\n")
            temporary.replace(self.path)
            return
        with open(self.path, "a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
