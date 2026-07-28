"""Persistence migration helpers."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from msgspec import Struct, field

from ..utils.helpers import utc_now
from .schema_versions import (
    GENSOKYOAI_CREATED_BY,
    MEMORY_SCHEMA_VERSION,
    MEMORY_STORE_FORMAT,
    SESSION_FILE_FORMAT,
    SESSION_SCHEMA_VERSION,
)

MAX_RECENT_MIGRATION_DIAGNOSTICS = 100


class MigrationDiagnostic(Struct):
    """Structured diagnostic emitted when persisted data is migrated."""

    source: str
    status: str
    from_schema_version: int | None
    to_schema_version: int
    format: str
    path: str | None = None
    backup_path: str | None = None
    message: str = ""
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    migrated_at: str = field(default_factory=lambda: utc_now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "from_schema_version": self.from_schema_version,
            "to_schema_version": self.to_schema_version,
            "format": self.format,
            "path": self.path,
            "backup_path": self.backup_path,
            "message": self.message,
            "diagnostics": list(self.diagnostics),
            "migrated_at": self.migrated_at,
        }


_RECENT_MIGRATION_DIAGNOSTICS: deque[MigrationDiagnostic] = deque(
    maxlen=MAX_RECENT_MIGRATION_DIAGNOSTICS
)


def _path_to_str(path: str | Path | None) -> str | None:
    return str(path) if path is not None else None


def record_migration_diagnostic(diagnostic: MigrationDiagnostic) -> None:
    """Record one process-local migration diagnostic for Runtime observability."""

    _RECENT_MIGRATION_DIAGNOSTICS.append(diagnostic)


def recent_migration_diagnostics() -> list[dict[str, Any]]:
    """Return recent migration diagnostics as JSON-compatible dictionaries."""

    return [item.to_dict() for item in _RECENT_MIGRATION_DIAGNOSTICS]


def clear_migration_diagnostics() -> None:
    """Clear process-local migration diagnostics. Primarily used by tests."""

    _RECENT_MIGRATION_DIAGNOSTICS.clear()


def migration_diagnostics_summary() -> dict[str, Any]:
    """Return a compact Runtime payload for recent migration diagnostics."""

    recent = recent_migration_diagnostics()
    counts = {"migrated": 0, "skipped": 0, "failed": 0}
    for item in recent:
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    return {"recent": recent, "counts": counts}


def make_migration_diagnostic(
    *,
    source: str,
    status: str,
    from_schema_version: int | None,
    to_schema_version: int | None = None,
    format: str,
    path: str | Path | None = None,
    backup_path: str | Path | None = None,
    message: str = "",
    diagnostics: list[dict[str, Any]] | None = None,
) -> MigrationDiagnostic:
    """Build a structured migration diagnostic with normalized path fields."""

    return MigrationDiagnostic(
        source=source,
        status=status,
        from_schema_version=from_schema_version,
        to_schema_version=to_schema_version or from_schema_version or 0,
        format=format,
        path=_path_to_str(path),
        backup_path=_path_to_str(backup_path),
        message=message,
        diagnostics=list(diagnostics or []),
    )


def _now_iso() -> str:
    return utc_now().isoformat()


def _migration_entry(*, from_version: int | None, to_version: int, reason: str) -> dict[str, Any]:
    return {
        "from_version": from_version,
        "to_version": to_version,
        "migrated_at": _now_iso(),
        "reason": reason,
    }


def migrate_session_file_payload(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a current-version session file payload and whether it changed."""

    migrated = dict(data)
    changed = False
    current_version = migrated.get("schema_version")

    if current_version == SESSION_SCHEMA_VERSION:
        migration_history = migrated.get("migration_history")
        if not isinstance(migration_history, list):
            migrated["migration_history"] = []
            changed = True
        if migrated.get("format") != SESSION_FILE_FORMAT:
            migrated["format"] = SESSION_FILE_FORMAT
            changed = True
        if migrated.get("created_by") != GENSOKYOAI_CREATED_BY:
            migrated["created_by"] = GENSOKYOAI_CREATED_BY
            changed = True
        return migrated, changed

    if "session" not in migrated:
        return migrated, False

    migrated.setdefault("format", SESSION_FILE_FORMAT)
    migrated["schema_version"] = SESSION_SCHEMA_VERSION
    migrated.setdefault("created_by", GENSOKYOAI_CREATED_BY)
    history = migrated.get("migration_history")
    if not isinstance(history, list):
        history = []
    history.append(
        _migration_entry(
            from_version=current_version if isinstance(current_version, int) else None,
            to_version=SESSION_SCHEMA_VERSION,
            reason="legacy_session_file_without_schema_version",
        )
    )
    migrated["migration_history"] = history
    migrated.setdefault("messages", [])
    session = migrated.get("session")
    if isinstance(session, dict):
        session.setdefault("revision", 0)
    return migrated, True


def make_session_file_payload(
    session: dict[str, Any], messages: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the current persisted session file payload."""

    return {
        "format": SESSION_FILE_FORMAT,
        "schema_version": SESSION_SCHEMA_VERSION,
        "created_by": GENSOKYOAI_CREATED_BY,
        "migration_history": [],
        "session": session,
        "messages": messages,
    }


def migrate_memory_store_payload(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a current-version topic memory store payload and whether it changed."""

    migrated = dict(data)
    changed = False
    current_version = migrated.get("schema_version")

    if current_version == MEMORY_SCHEMA_VERSION:
        migration_history = migrated.get("migration_history")
        if not isinstance(migration_history, list):
            migrated["migration_history"] = []
            changed = True
        if migrated.get("format") != MEMORY_STORE_FORMAT:
            migrated["format"] = MEMORY_STORE_FORMAT
            changed = True
        if migrated.get("created_by") != GENSOKYOAI_CREATED_BY:
            migrated["created_by"] = GENSOKYOAI_CREATED_BY
            changed = True
        return migrated, changed

    migrated.setdefault("format", MEMORY_STORE_FORMAT)
    migrated.setdefault("created_by", GENSOKYOAI_CREATED_BY)
    history = migrated.get("migration_history")
    if not isinstance(history, list):
        history = []

    if current_version == 1:
        # v1 -> v2: Topic 新增 last_thought_at 和 thought_count 字段，旧数据使用默认值即可
        migrated["schema_version"] = MEMORY_SCHEMA_VERSION
        history.append(
            _migration_entry(
                from_version=1,
                to_version=MEMORY_SCHEMA_VERSION,
                reason="add_topic_thought_tracking_fields",
            )
        )
    else:
        # 无 schema version 的旧数据直接升级到当前版本
        migrated["schema_version"] = MEMORY_SCHEMA_VERSION
        history.append(
            _migration_entry(
                from_version=current_version if isinstance(current_version, int) else None,
                to_version=MEMORY_SCHEMA_VERSION,
                reason="legacy_topic_store_without_schema_version",
            )
        )

    migrated["migration_history"] = history
    migrated.setdefault("topics", [])
    migrated.setdefault("memories", [])
    return migrated, True


def make_memory_store_payload(topics: list[Any], memories: list[Any]) -> dict[str, Any]:
    """Build the current persisted topic memory store payload."""

    return {
        "format": MEMORY_STORE_FORMAT,
        "schema_version": MEMORY_SCHEMA_VERSION,
        "created_by": GENSOKYOAI_CREATED_BY,
        "migration_history": [],
        "topics": topics,
        "memories": memories,
    }
