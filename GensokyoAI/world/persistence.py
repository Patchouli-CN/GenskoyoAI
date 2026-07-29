"""World 会话持久化。

World 存档拥有独立格式与 schema，不修改或复用单角色 session 文件格式。文件操作采用
临时文件原子替换、覆盖前备份与损坏隔离，保证后续 World 主状态机可以安全接入。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import msgspec

from ..core.schema_versions import WORLD_SESSION_FILE_FORMAT, WORLD_SESSION_SCHEMA_VERSION
from ..utils.helpers import utc_now
from ..utils.logger import logger
from ..utils.path_security import sanitize_path_id
from .types import (
    USER_OCCUPANT_ID,
    WorldLoadResult,
    WorldPersistenceDiagnostic,
    WorldSessionRecord,
)

_world_json_encoder = msgspec.json.Encoder()
_world_json_decoder = msgspec.json.Decoder(type=dict[str, Any])


class WorldPersistenceError(ValueError):
    """World 存档格式、版本或身份校验失败。"""


class WorldPersistence:
    """按 world_id 分区的 World 会话持久化服务。"""

    def __init__(self, base_path: Path) -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock_for(self, world_id: str, session_id: str) -> asyncio.Lock:
        key = (sanitize_path_id(world_id), sanitize_path_id(session_id))
        return self._locks.setdefault(key, asyncio.Lock())

    def _world_dir(self, world_id: str, *, create: bool = False) -> Path:
        path = self.base_path / sanitize_path_id(world_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def session_path(self, world_id: str, session_id: str, *, create_parent: bool = False) -> Path:
        """返回经过净化且受保存根目录约束的 World 存档路径。"""
        return self._world_dir(world_id, create=create_parent) / (
            f"{sanitize_path_id(session_id)}.json"
        )

    @staticmethod
    def _backup_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.bak")

    @staticmethod
    def _quarantine_path(path: Path) -> Path:
        timestamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        return path.parent / "quarantine" / f"{path.name}.{timestamp}.{uuid4().hex}.bad"

    def _quarantine_file(self, path: Path) -> Path | None:
        if not path.exists():
            return None
        target = self._quarantine_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        logger.warning(f"损坏 World 存档已隔离: {path} -> {target}")
        return target

    def _atomic_write_json(
        self, path: Path, payload: dict[str, Any], *, backup_existing: bool = True
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            encoded = msgspec.json.format(_world_json_encoder.encode(payload), indent=2)
            with open(temporary, "wb") as file:
                file.write(encoded)
            if backup_existing and path.exists():
                shutil.copy2(path, self._backup_path(path))
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            with open(path, "rb") as file:
                return _world_json_decoder.decode(file.read())
        except (msgspec.DecodeError, OSError) as primary_error:
            backup = self._backup_path(path)
            if backup.exists():
                try:
                    with open(backup, "rb") as file:
                        payload = _world_json_decoder.decode(file.read())
                    self._atomic_write_json(path, payload, backup_existing=False)
                    logger.warning(f"已从备份恢复 World 存档: {path}")
                    return payload
                except msgspec.DecodeError, OSError:
                    pass
            quarantined = self._quarantine_file(path)
            raise WorldPersistenceError(
                f"World 存档损坏且无法从备份恢复: {path}; quarantine={quarantined}"
            ) from primary_error

    @staticmethod
    def _payload(record: WorldSessionRecord) -> dict[str, Any]:
        return {
            "format": WORLD_SESSION_FILE_FORMAT,
            "schema_version": WORLD_SESSION_SCHEMA_VERSION,
            "world_session": msgspec.to_builtins(record),
        }

    @staticmethod
    def _decode_payload(
        payload: dict[str, Any], *, expected_world_id: str, expected_session_id: str
    ) -> WorldSessionRecord:
        if payload.get("format") != WORLD_SESSION_FILE_FORMAT:
            raise WorldPersistenceError("不是受支持的 GensokyoAI World 存档格式")
        version = payload.get("schema_version")
        if version != WORLD_SESSION_SCHEMA_VERSION:
            raise WorldPersistenceError(
                f"不支持的 World session schema version: {version!r}; "
                f"当前仅支持 {WORLD_SESSION_SCHEMA_VERSION}"
            )
        raw_record = payload.get("world_session")
        if not isinstance(raw_record, dict):
            raise WorldPersistenceError("World 存档缺少 world_session 对象")
        try:
            record = msgspec.convert(raw_record, type=WorldSessionRecord, strict=True)
        except (msgspec.ValidationError, TypeError, ValueError) as error:
            raise WorldPersistenceError(f"World 存档字段无效: {error}") from error
        if record.world_id != expected_world_id:
            raise WorldPersistenceError(
                f"World id 不匹配: 期望 {expected_world_id!r}，实际 {record.world_id!r}"
            )
        if record.session_id != expected_session_id:
            raise WorldPersistenceError(
                f"World session id 不匹配: 期望 {expected_session_id!r}，实际 {record.session_id!r}"
            )
        return record

    @staticmethod
    def _roster_diagnostics(
        record: WorldSessionRecord, available_actor_ids: set[str] | None
    ) -> list[WorldPersistenceDiagnostic]:
        if available_actor_ids is None:
            return []
        # roster/actor_sessions 之外，stage 上的幽灵占位与 current_actor 同样
        # 必须被诊断覆盖，否则恢复时会带上不可用 actor 而不自知
        stage_actors = {occupant for occupant in record.stage if occupant != USER_OCCUPANT_ID}
        current = {record.current_actor_id} if record.current_actor_id else set()
        persisted = set(record.roster) | set(record.actor_sessions) | stage_actors | current
        diagnostics: list[WorldPersistenceDiagnostic] = []
        for actor_id in sorted(persisted - available_actor_ids):
            diagnostics.append(
                WorldPersistenceDiagnostic(
                    code="world.persistence.actor_missing",
                    severity="error",
                    actor_id=actor_id,
                    message=f"存档引用的 Actor 当前不可用: {actor_id}",
                )
            )
        for actor_id in sorted(available_actor_ids - persisted):
            diagnostics.append(
                WorldPersistenceDiagnostic(
                    code="world.persistence.actor_added",
                    severity="warning",
                    actor_id=actor_id,
                    message=f"当前配置中的 Actor 不在存档 roster 中: {actor_id}",
                )
            )
        return diagnostics

    def create(
        self,
        world_id: str,
        *,
        session_id: str | None = None,
        protagonist: str = "__user__",
        metadata: dict[str, Any] | None = None,
    ) -> WorldSessionRecord:
        """创建并立即保存一条新 World 会话。"""
        record = WorldSessionRecord(
            world_id=world_id,
            session_id=session_id or str(uuid4()),
            protagonist=protagonist,
            metadata=dict(metadata or {}),
        )
        self._create_exclusive(record)
        return record

    def _create_exclusive(self, record: WorldSessionRecord) -> None:
        """排他创建：已存在即报错（调用方持锁时才能消除 TOCTOU 竞态）。"""
        path = self.session_path(record.world_id, record.session_id, create_parent=True)
        if path.exists():
            raise WorldPersistenceError(f"World session 已存在: {record.session_id}")
        self._atomic_write_json(path, self._payload(record), backup_existing=False)

    async def create_async(
        self,
        world_id: str,
        *,
        session_id: str | None = None,
        protagonist: str = "__user__",
        metadata: dict[str, Any] | None = None,
    ) -> WorldSessionRecord:
        """在 per-key 锁内创建（exists 检查与写入原子化，消除并发 create 竞态）。"""
        record = WorldSessionRecord(
            world_id=world_id,
            session_id=session_id or str(uuid4()),
            protagonist=protagonist,
            metadata=dict(metadata or {}),
        )
        async with self._lock_for(world_id, record.session_id):
            await asyncio.to_thread(self._create_exclusive, record)
        return record

    def save(self, record: WorldSessionRecord) -> None:
        """原子保存 World 会话，并在覆盖前保留上一版备份。"""
        record.touch()
        path = self.session_path(record.world_id, record.session_id, create_parent=True)
        self._atomic_write_json(path, self._payload(record))

    async def save_async(self, record: WorldSessionRecord) -> None:
        async with self._lock_for(record.world_id, record.session_id):
            await asyncio.to_thread(self.save, record)

    def resume(
        self,
        world_id: str,
        session_id: str,
        *,
        available_actor_ids: set[str] | None = None,
    ) -> WorldLoadResult | None:
        """读取并校验 World 会话；不存在时返回 None。"""
        path = self.session_path(world_id, session_id)
        if not path.exists():
            return None
        payload = self._read_json(path)
        record = self._decode_payload(
            payload, expected_world_id=world_id, expected_session_id=session_id
        )
        return WorldLoadResult(
            record=record,
            diagnostics=self._roster_diagnostics(record, available_actor_ids),
        )

    async def resume_async(
        self,
        world_id: str,
        session_id: str,
        *,
        available_actor_ids: set[str] | None = None,
    ) -> WorldLoadResult | None:
        async with self._lock_for(world_id, session_id):
            return await asyncio.to_thread(
                self.resume,
                world_id,
                session_id,
                available_actor_ids=available_actor_ids,
            )

    def list(self, world_id: str) -> list[WorldSessionRecord]:
        """列出指定 World 的有效会话；损坏或不兼容文件记日志后跳过。

        注意自愈副作用：本方法名义上只读，但读取过程中损坏的主文件会被
        移入 quarantine、可读备份会被回写主文件（与 resume 共用 `_read_json`）。
        """
        directory = self._world_dir(world_id)
        if not directory.exists():
            return []
        records: list[WorldSessionRecord] = []
        for path in directory.glob("*.json"):
            try:
                payload = self._read_json(path)
                records.append(
                    self._decode_payload(
                        payload,
                        expected_world_id=world_id,
                        expected_session_id=path.stem,
                    )
                )
            except WorldPersistenceError as error:
                logger.warning(f"跳过无效 World 存档 {path}: {error}")
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def delete(self, world_id: str, session_id: str) -> bool:
        """删除指定 World 会话主文件及其备份。"""
        path = self.session_path(world_id, session_id)
        if not path.exists():
            return False
        path.unlink()
        backup = self._backup_path(path)
        if backup.exists():
            backup.unlink()
        return True

    async def delete_async(self, world_id: str, session_id: str) -> bool:
        async with self._lock_for(world_id, session_id):
            return await asyncio.to_thread(self.delete, world_id, session_id)

    def export(self, world_id: str, session_id: str) -> dict[str, Any]:
        """导出机器可读的独立 World session bundle。"""
        result = self.resume(world_id, session_id)
        if result is None:
            raise WorldPersistenceError(f"World session 不存在: {session_id}")
        return {
            "format": "gensokyoai.world.session.export",
            "schema_version": WORLD_SESSION_SCHEMA_VERSION,
            "world_session_schema_version": WORLD_SESSION_SCHEMA_VERSION,
            "exported_at": utc_now().isoformat(),
            "world_session": msgspec.to_builtins(result.record),
        }
