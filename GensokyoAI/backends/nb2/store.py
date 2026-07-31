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


def _read_json_dict(path: Path) -> dict:
    """读取 JSON dict 文件；不存在返回空，损坏抛给调用方决定（各自记日志降级）。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _atomic_write_json(path: Path, data: dict) -> None:
    """tmp 文件 + 原子替换落盘（防中途断电留半个文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


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
            raw = _read_json_dict(self._path)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"[nb2] 会话映射文件损坏，已从空表重新开始: {error}")
            return
        self._entries = {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _save_locked(self) -> None:
        _atomic_write_json(self._path, self._entries)


class MemberStore:
    """群友印象 fake db（known_members.json）。

    key = ``{qq_name}_{qq_id}``（同名靠 qq_id 后缀区分），value = 角色视角的
    第一人称印象文本。查询按 qq_id 后缀匹配，改名不丢印象（put 时会清掉同
    qq_id 的旧 key）。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, str] = {}
        try:
            raw = _read_json_dict(self._path)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"[nb2] 群友印象文件损坏，已从空表重新开始: {error}")
            return
        self._entries = {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}

    def get(self, qq_id: int) -> str | None:
        """按 qq_id 查印象；精确 key 未知时按 ``_{qq_id}`` 后缀匹配。"""
        suffix = f"_{qq_id}"
        with self._lock:
            for key, value in self._entries.items():
                if key.endswith(suffix):
                    return value
        return None

    def put(self, qq_name: str, qq_id: int, impression: str) -> None:
        """写入/更新印象；同名覆盖、改名清旧 key。"""
        suffix = f"_{qq_id}"
        with self._lock:
            for key in [k for k in self._entries if k.endswith(suffix)]:
                del self._entries[key]
            self._entries[f"{qq_name}_{qq_id}"] = impression
            self._save_locked()

    def update_by_name(self, qq_name: str, impression: str) -> bool:
        """按昵称部分（key 的 ``{name}_`` 前缀）更新印象，供 AI 工具调用；找不到返回 False。"""
        with self._lock:
            for key in self._entries:
                if key.rsplit("_", 1)[0] == qq_name:
                    self._entries[key] = impression
                    self._save_locked()
                    return True
        return False

    def _save_locked(self) -> None:
        _atomic_write_json(self._path, self._entries)
