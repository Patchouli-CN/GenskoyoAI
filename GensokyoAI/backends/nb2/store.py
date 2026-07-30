"""QQ 群/私聊 → Runtime 会话映射的持久化存储。

Runtime 没有「按外部键查会话」的 RPC，适配器需要自己维护
`qq key -> (agent_id, session_id, revision)` 映射。JSON 文件 + tmp 原子替换落盘；
文件损坏时从空表重新开始（最坏情况只是群聊重开一个会话）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ...utils.logger import logger


class SessionStore:
    """同步 JSON 映射表：键为 "group:<群号>" / "user:<QQ号>"。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    def get(self, key: str) -> dict[str, Any] | None:
        """读取映射；不存在返回 None。返回副本，调用方可随意修改。"""
        with self._lock:
            entry = self._entries.get(key)
            return dict(entry) if entry is not None else None

    def put(self, key: str, *, agent_id: str, session_id: str, revision: int) -> None:
        """写入/覆盖一条映射并立即落盘。"""
        with self._lock:
            self._entries[key] = {
                "agent_id": agent_id,
                "session_id": session_id,
                "revision": int(revision),
            }
            self._save_locked()

    def update_revision(self, key: str, revision: int) -> None:
        """仅推进 revision（每轮对话成功后调用）；键不存在时忽略。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry["revision"] = int(revision)
                self._save_locked()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"[nb2] 会话映射文件损坏，已从空表重新开始: {error}")
            return
        if isinstance(raw, dict):
            self._entries = {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, self._path)
