"""会话持久化"""

# GensokyoAI\session\persistence.py

import asyncio
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import msgspec

from ..core.migrations import (
    make_migration_diagnostic,
    make_session_file_payload,
    migrate_session_file_payload,
    record_migration_diagnostic,
)
from ..core.schema_versions import SESSION_FILE_FORMAT, SESSION_SCHEMA_VERSION
from ..utils.helpers import utc_now
from ..utils.logger import logger
from ..utils.path_security import sanitize_path_id
from .context import SessionContext

# msgspec JSON 编码器/解码器（性能比标准库 json 快 10-100 倍）
_session_json_encoder = msgspec.json.Encoder()
_session_json_decoder = msgspec.json.Decoder(type=dict[str, Any])

# 分片锁配置：减少锁竞争，提高并发性能
_SHARD_COUNT = 16  # 锁分片数量（2 的幂方便位运算）


def _get_shard_index(session_id: str) -> int:
    """根据 session_id 计算锁分片索引。"""
    return hash(session_id) % _SHARD_COUNT


class SessionPersistence:
    """会话持久化 - 基于分片锁的高并发异步 I/O"""

    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.base_path.mkdir(parents=True, exist_ok=True)
        # 分片锁：按 session_id 哈希分片，减少锁竞争
        self._sharded_locks: list[asyncio.Lock] = [asyncio.Lock() for _ in range(_SHARD_COUNT)]
        # 添加 session_id -> character_id 的映射缓存
        self._session_index: dict[str, str] = {}
        self._build_index()

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        """获取指定 session_id 对应的分片锁。"""
        return self._sharded_locks[_get_shard_index(session_id)]

    def _build_index(self) -> None:
        """构建会话索引"""
        self._session_index.clear()
        if not self.base_path.exists():
            return

        for char_dir in self.base_path.iterdir():
            if char_dir.is_dir():
                char_name = char_dir.name
                for session_file in char_dir.glob("*.json"):
                    session_id = session_file.stem
                    self._session_index[session_id] = char_name
        logger.debug(f"构建会话索引完成，共 {len(self._session_index)} 个会话")

    def _add_to_index(self, character_id: str, session_id: str) -> None:
        """添加到索引"""
        self._session_index[session_id] = character_id

    def _remove_from_index(self, session_id: str) -> None:
        """从索引移除"""
        self._session_index.pop(session_id, None)

    def _get_session_path(self, character_id: str, session_id: str) -> Path:
        """获取会话文件路径；对 character_id 与 session_id 做路径净化。"""
        safe_character_id = sanitize_path_id(character_id)
        safe_session_id = sanitize_path_id(session_id)
        char_path = self.base_path / safe_character_id
        char_path.mkdir(parents=True, exist_ok=True)
        return char_path / f"{safe_session_id}.json"

    def _backup_path(self, path: Path) -> Path:
        """获取备份文件路径。"""
        return path.with_name(f"{path.name}.bak")

    def _quarantine_dir(self, path: Path) -> Path:
        """获取损坏文件隔离目录。"""
        return path.parent / "quarantine"

    def _quarantine_path(self, path: Path) -> Path:
        """生成不会覆盖现有文件的隔离文件路径。"""
        timestamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        return self._quarantine_dir(path) / f"{path.name}.{timestamp}.{uuid4().hex}.bad"

    def _read_json_file(self, path: Path) -> dict:
        """读取 JSON 文件；使用 msgspec 高性能解析，失败时尝试从备份恢复。

        注意：为了保持与标准库 json 的兼容性，msgspec 的 DecodeError 会被转换为 JSONDecodeError。
        """
        import json as _json  # 延迟导入，避免循环依赖

        try:
            with open(path, "rb") as f:
                return _session_json_decoder.decode(f.read())
        except msgspec.DecodeError as decode_error:
            # 将 msgspec 异常转换为标准库 json.JSONDecodeError 以保持兼容性
            original_error = _json.JSONDecodeError(
                msg=str(decode_error),
                doc="",
                pos=0,
            )
            logger.warning(f"读取 JSON 失败，尝试备份恢复 {path}: {original_error}")
            backup_path = self._backup_path(path)
            if backup_path.exists():
                try:
                    with open(backup_path, "rb") as f:
                        data = _session_json_decoder.decode(f.read())
                    self._atomic_write_json(path, data, backup_existing=False)
                    logger.warning(f"已从备份恢复 JSON 文件: {path}")
                    return data
                except msgspec.DecodeError as backup_decode_error:
                    backup_error = _json.JSONDecodeError(
                        msg=str(backup_decode_error),
                        doc="",
                        pos=0,
                    )
                    logger.warning(f"备份恢复失败 {backup_path}: {backup_error}")

            quarantine_path = self._quarantine_file(path)
            self._record_failed_migration(
                path,
                backup_path=backup_path if backup_path.exists() else None,
                message="Session file could not be read or recovered before migration.",
                error=original_error,
                diagnostics=[
                    {
                        "code": "migration.session.read_failed",
                        "path": str(path),
                        "severity": "error",
                        "message": str(original_error),
                        "suggestion": "请检查会话 JSON 是否损坏；如存在可用备份，可手动恢复。",
                    },
                    {
                        "code": "migration.session.quarantined",
                        "path": str(quarantine_path) if quarantine_path else str(path),
                        "severity": "warning",
                        "message": "Corrupt session file was quarantined.",
                        "suggestion": "可在 quarantine 目录中查看损坏文件。",
                    },
                ],
            )
            raise original_error from None

    def _read_session_file(self, path: Path) -> dict:
        """读取并迁移会话 JSON 文件。"""
        data = self._read_json_file(path)
        from_schema_version = data.get("schema_version")
        try:
            migrated, changed = migrate_session_file_payload(data)
            if changed:
                backup_path = self._backup_path(path)
                self._atomic_write_json(path, migrated, backup_existing=True)
                record_migration_diagnostic(
                    make_migration_diagnostic(
                        source="session",
                        status="migrated",
                        from_schema_version=(
                            from_schema_version if isinstance(from_schema_version, int) else None
                        ),
                        to_schema_version=SESSION_SCHEMA_VERSION,
                        format=SESSION_FILE_FORMAT,
                        path=path,
                        backup_path=backup_path,
                        message="Session file migrated to current schema version.",
                    )
                )
                logger.info(f"会话文件已迁移到当前 schema: {path}")
            return migrated
        except Exception as error:
            self._record_failed_migration(
                path,
                backup_path=self._backup_path(path) if path.exists() else None,
                from_schema_version=(
                    from_schema_version if isinstance(from_schema_version, int) else None
                ),
                message="Session file migration failed.",
                error=error,
                diagnostics=[
                    {
                        "code": "migration.session.failed",
                        "path": str(path),
                        "severity": "error",
                        "message": str(error),
                        "suggestion": "请保留原始会话文件和 .bak 备份，并检查 schema 或文件权限。",
                    }
                ],
            )
            raise

    def _record_failed_migration(
        self,
        path: Path,
        *,
        backup_path: Path | None = None,
        from_schema_version: int | None = None,
        message: str,
        error: Exception,
        diagnostics: list[dict] | None = None,
    ) -> None:
        """Record a failed session migration diagnostic without changing control flow."""

        record_migration_diagnostic(
            make_migration_diagnostic(
                source="session",
                status="failed",
                from_schema_version=from_schema_version,
                to_schema_version=SESSION_SCHEMA_VERSION,
                format=SESSION_FILE_FORMAT,
                path=path,
                backup_path=backup_path,
                message=message,
                diagnostics=diagnostics
                or [
                    {
                        "code": "migration.session.failed",
                        "path": str(path),
                        "severity": "error",
                        "message": str(error),
                    }
                ],
            )
        )

    async def _read_session_file_async(self, path: Path) -> dict:
        """异步读取并迁移会话 JSON 文件。"""
        return await asyncio.to_thread(self._read_session_file, path)

    async def _read_json_file_async(self, path: Path) -> dict:
        """异步读取 JSON 文件；复用同步容错逻辑避免两套恢复语义。"""
        return await asyncio.to_thread(self._read_json_file, path)

    def _atomic_write_json(self, path: Path, data: dict, *, backup_existing: bool = True) -> None:
        """以临时文件 + 原子替换写入 JSON，使用 msgspec 高性能序列化，并在覆盖前保留 .bak。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            # 使用 msgspec 高性能序列化（比标准库 json 快 10-100 倍）
            json_bytes = msgspec.json.format(_session_json_encoder.encode(data), indent=2)
            with open(tmp_path, "wb") as f:
                f.write(json_bytes)
            if backup_existing and path.exists():
                shutil.copy2(path, self._backup_path(path))
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    async def _atomic_write_json_async(
        self,
        path: Path,
        data: dict,
        *,
        backup_existing: bool = True,
    ) -> None:
        """异步写入 JSON；文件替换逻辑放到线程中执行以保持一致容错语义。"""
        await asyncio.to_thread(
            self._atomic_write_json, path, data, backup_existing=backup_existing
        )

    def _quarantine_file(self, path: Path) -> Path | None:
        """将损坏文件移动到 quarantine 目录。"""
        if not path.exists():
            return None
        quarantine_path = self._quarantine_path(path)
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        path.replace(quarantine_path)
        logger.warning(f"损坏 JSON 文件已隔离: {path} -> {quarantine_path}")
        return quarantine_path

    def save_session(self, session: SessionContext) -> None:
        """保存会话（同步，用于兼容）"""
        path = self._get_session_path(session.character_id, session.session_id)
        self._add_to_index(session.character_id, session.session_id)

        existing_messages = []
        if path.exists():
            data = self._read_session_file(path)
            existing_messages = data.get("messages", [])

        data = make_session_file_payload(session.to_dict(), existing_messages)
        self._atomic_write_json(path, data)
        logger.debug(f"会话已保存: {path}")

    async def save_session_async(self, session: SessionContext) -> None:
        """保存会话（异步 - 使用分片锁提高并发）"""
        async with self._get_lock(session.session_id):
            path = self._get_session_path(session.character_id, session.session_id)
            self._add_to_index(session.character_id, session.session_id)

            existing_messages = []
            if path.exists():
                data = await self._read_session_file_async(path)
                existing_messages = data.get("messages", [])

            data = make_session_file_payload(session.to_dict(), existing_messages)
            await self._atomic_write_json_async(path, data)
        logger.debug(f"会话已异步保存: {path}")

    def replace_messages(self, session_id: str, messages: list[dict]) -> None:
        """全量替换消息（同步），复用原子写入与备份逻辑。"""
        self.save_messages(session_id, messages)

    async def replace_messages_async(self, session_id: str, messages: list[dict]) -> None:
        """全量替换消息（异步），复用原子写入与备份逻辑。"""
        await self.async_save_message(session_id, messages)

    def save_messages(self, session_id: str, messages: list[dict]) -> None:
        """保存消息（同步）- 优化版"""
        safe_session_id = sanitize_path_id(session_id)
        char_id = self._session_index.get(session_id)
        if char_id:
            session_file = self._get_session_path(char_id, safe_session_id)
            if session_file.exists():
                data = self._read_session_file(session_file)
                data["messages"] = messages
                if "session" in data:
                    data["session"]["total_turns"] = len(messages) // 2
                self._atomic_write_json(session_file, data)
                logger.debug(f"消息已保存: {session_id}, {len(messages)} 条")
                return

    async def async_save_message(self, session_id: str, messages: list[dict]) -> None:
        """保存消息（异步）- 使用分片锁提高并发"""
        async with self._get_lock(session_id):
            safe_session_id = sanitize_path_id(session_id)
            # 使用索引快速定位
            char_id = self._session_index.get(session_id)
            if char_id:
                session_file = self._get_session_path(char_id, safe_session_id)
                if session_file.exists():
                    data = await self._read_session_file_async(session_file)
                    data["messages"] = messages
                    if "session" in data:
                        data["session"]["total_turns"] = len(messages) // 2
                    await self._atomic_write_json_async(session_file, data)
                    logger.debug(f"消息已异步保存: {session_id}, {len(messages)} 条")
                    return

            # 降级：遍历查找（同时更新索引）
            for char_dir in self.base_path.iterdir():
                if char_dir.is_dir():
                    session_file = self._get_session_path(char_dir.name, safe_session_id)
                    if session_file.exists():
                        self._add_to_index(char_dir.name, session_id)
                        data = await self._read_session_file_async(session_file)
                        data["messages"] = messages
                        if "session" in data:
                            data["session"]["total_turns"] = len(messages) // 2
                        await self._atomic_write_json_async(session_file, data)
                        logger.debug(f"消息已异步保存: {session_id}, {len(messages)} 条")
                        return

            logger.warning(f"未找到会话文件: {session_id}")

    def load_messages(self, session_id: str) -> list[dict]:
        """加载消息（同步）"""
        safe_session_id = sanitize_path_id(session_id)
        # 使用索引快速定位
        char_id = self._session_index.get(session_id)
        if char_id:
            session_file = self._get_session_path(char_id, safe_session_id)
            if session_file.exists():
                data = self._read_session_file(session_file)
                messages = data.get("messages", [])
                logger.debug(f"加载消息: {session_id}, {len(messages)} 条")
                return messages

        # 降级：遍历查找
        for char_dir in self.base_path.iterdir():
            if char_dir.is_dir():
                session_file = self._get_session_path(char_dir.name, safe_session_id)
                if session_file.exists():
                    self._add_to_index(char_dir.name, session_id)
                    data = self._read_session_file(session_file)
                    messages = data.get("messages", [])
                    logger.debug(f"加载消息: {session_id}, {len(messages)} 条")
                    return messages
        return []

    async def load_messages_async(self, session_id: str) -> list[dict]:
        """加载消息（异步 - 使用 ayafileio）"""
        safe_session_id = sanitize_path_id(session_id)
        # 使用索引快速定位
        char_id = self._session_index.get(session_id)
        if char_id:
            session_file = self._get_session_path(char_id, safe_session_id)
            if session_file.exists():
                data = await self._read_session_file_async(session_file)
                messages = data.get("messages", [])
                logger.debug(f"异步加载消息: {session_id}, {len(messages)} 条")
                return messages

        # 降级：遍历查找
        for char_dir in self.base_path.iterdir():
            if char_dir.is_dir():
                session_file = self._get_session_path(char_dir.name, safe_session_id)
                if session_file.exists():
                    self._add_to_index(char_dir.name, session_id)
                    data = await self._read_session_file_async(session_file)
                    messages = data.get("messages", [])
                    logger.debug(f"异步加载消息: {session_id}, {len(messages)} 条")
                    return messages
        return []

    def load_session(self, character_id: str, session_id: str) -> SessionContext | None:
        """加载会话（同步）"""
        path = self._get_session_path(character_id, session_id)
        if not path.exists():
            return None

        data = self._read_session_file(path)
        return SessionContext.from_dict(data["session"])

    def list_sessions(self, character_id: str) -> list[SessionContext]:
        """列出所有会话（同步）"""
        sessions = []
        char_path = self.base_path / sanitize_path_id(character_id)
        if char_path.exists():
            for file in char_path.glob("*.json"):
                try:
                    data = self._read_session_file(file)
                    sessions.append(SessionContext.from_dict(data["session"]))
                except Exception as e:
                    logger.warning(f"加载会话失败 {file}: {e}")
        return sessions

    def delete_session(self, session_id: str) -> bool:
        """删除会话（同步）- 优化版"""
        safe_session_id = sanitize_path_id(session_id)
        # 使用索引快速定位；删除成功后再移除索引，避免提前移除导致快速路径失效。
        char_id = self._session_index.get(session_id)
        if char_id:
            session_file = self._get_session_path(char_id, safe_session_id)
            if session_file.exists():
                session_file.unlink()
                self._remove_from_index(session_id)
                logger.debug(f"会话已删除: {session_id}")
                return True

        # 降级：遍历查找
        for char_dir in self.base_path.iterdir():
            if char_dir.is_dir():
                session_file = self._get_session_path(char_dir.name, safe_session_id)
                if session_file.exists():
                    session_file.unlink()
                    self._remove_from_index(session_id)
                    logger.debug(f"会话已删除: {session_id}")
                    return True
        return False

    async def delete_session_async(self, session_id: str) -> bool:
        """删除会话（异步）- 使用分片锁提高并发"""
        async with self._get_lock(session_id):
            safe_session_id = sanitize_path_id(session_id)
            # 使用索引快速定位；删除成功后再移除索引，避免提前移除导致快速路径失效。
            char_id = self._session_index.get(session_id)
            if char_id:
                session_file = self._get_session_path(char_id, safe_session_id)
                if session_file.exists():
                    await asyncio.to_thread(session_file.unlink)
                    self._remove_from_index(session_id)
                    logger.debug(f"会话已异步删除: {session_id}")
                    return True

            # 降级：遍历查找
            for char_dir in self.base_path.iterdir():
                if char_dir.is_dir():
                    session_file = self._get_session_path(char_dir.name, safe_session_id)
                    if session_file.exists():
                        await asyncio.to_thread(session_file.unlink)
                        self._remove_from_index(session_id)
                        logger.debug(f"会话已异步删除: {session_id}")
                        return True
        return False

    def rebuild_index(self) -> None:
        """重建索引（用于手动刷新）"""
        self._build_index()
        logger.info("会话索引已重建")
