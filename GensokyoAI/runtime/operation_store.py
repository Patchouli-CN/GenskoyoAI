"""Persistent idempotent operation records for remote Runtime requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from GensokyoAI.utils.helpers import utc_now


class RuntimeOperationStore:
    """Atomically persist bounded message-generation operation state.

    写路径（begin/succeed/fail/cancel）为 async：账本随运行时长增长，
    全量序列化+落盘放到 ``asyncio.to_thread``，不阻塞共享事件循环。
    读路径（get）为纯内存查询，保持同步。
    """

    MAX_RECORDS = 10_000

    def __init__(self, path: Path, *, max_records: int = MAX_RECORDS) -> None:
        self.path = path
        self.max_records = max(100, int(max_records))
        self._records = self._load()
        if self._recover_interrupted():
            self._save()

    @staticmethod
    def request_fingerprint(payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def get(self, session_id: str, idempotency_key: str) -> dict[str, Any] | None:
        record = self._records.get(self._record_key(session_id, idempotency_key))
        return dict(record) if record is not None else None

    async def begin(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        generation_id: str,
    ) -> dict[str, Any]:
        storage_key = self._record_key(session_id, idempotency_key)
        existing = self._records.get(storage_key)
        if existing is not None:
            return dict(existing)
        timestamp = utc_now().isoformat()
        record = {
            "operation_id": str(uuid4()),
            "session_id": session_id,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "generation_id": generation_id,
            "status": "pending",
            "created_at": timestamp,
            "updated_at": timestamp,
            "result": None,
            "error": None,
        }
        self._records[storage_key] = record
        self._trim()
        await self._save_async()
        return dict(record)

    async def succeed(
        self,
        session_id: str,
        idempotency_key: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._finish(
            session_id,
            idempotency_key,
            status="succeeded",
            result=result,
            error=None,
        )

    async def fail(
        self,
        session_id: str,
        idempotency_key: str,
        error: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._finish(
            session_id,
            idempotency_key,
            status="failed",
            result=None,
            error=error,
        )

    async def cancel(
        self,
        session_id: str,
        idempotency_key: str,
        error: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._finish(
            session_id,
            idempotency_key,
            status="cancelled",
            result=None,
            error=error,
        )

    async def _finish(
        self,
        session_id: str,
        idempotency_key: str,
        *,
        status: str,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        storage_key = self._record_key(session_id, idempotency_key)
        record = self._records.get(storage_key)
        if record is None:
            raise KeyError("Runtime operation record does not exist")
        record.update(
            {
                "status": status,
                "updated_at": utc_now().isoformat(),
                "result": result,
                "error": error,
            }
        )
        await self._save_async()
        return dict(record)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Runtime operation store is unreadable or corrupt: {self.path}"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError(f"Runtime operation store is unreadable or corrupt: {self.path}")
        records = payload.get("records", payload)
        if not isinstance(records, dict):
            raise RuntimeError(f"Runtime operation store is unreadable or corrupt: {self.path}")
        if not all(isinstance(value, dict) for value in records.values()):
            raise RuntimeError(f"Runtime operation store is unreadable or corrupt: {self.path}")
        return {str(key): dict(value) for key, value in records.items()}

    def _recover_interrupted(self) -> bool:
        changed = False
        timestamp = utc_now().isoformat()
        for record in self._records.values():
            if record.get("status") != "pending":
                continue
            record.update(
                {
                    "status": "failed",
                    "updated_at": timestamp,
                    "error": {
                        "code": "message.operation_outcome_unknown",
                        "error_code": "message.operation_outcome_unknown",
                        "message": "上一次生成在 Runtime 重启前没有确认最终结果。",
                        "technical_message": "Message operation was interrupted before its outcome was committed",
                        "user_message": "上一次生成在 Runtime 重启前没有确认最终结果。",
                        "recoverable": True,
                        "action_hint": "请先重新读取会话；确认没有结果后，再使用新的 idempotency_key 发送。",
                        "details": {"outcome_unknown": True},
                    },
                }
            )
            changed = True
        return changed

    def _trim(self) -> None:
        if len(self._records) <= self.max_records:
            return
        terminal = sorted(
            (
                (key, record)
                for key, record in self._records.items()
                if record.get("status") != "pending"
            ),
            key=lambda item: str(item[1].get("updated_at", "")),
        )
        for key, _ in terminal:
            if len(self._records) <= self.max_records:
                break
            self._records.pop(key, None)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"version": 1, "records": self._records},
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    async def _save_async(self) -> None:
        """消息热路径用：全量序列化+落盘丢线程，让出事件循环。"""
        await asyncio.to_thread(self._save)

    @staticmethod
    def _record_key(session_id: str, idempotency_key: str) -> str:
        value = f"{session_id}\0{idempotency_key}".encode()
        return hashlib.sha256(value).hexdigest()
