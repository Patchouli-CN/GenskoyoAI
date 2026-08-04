"""Tenant-scoped media metadata and blob storage."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from GensokyoAI.utils.helpers import utc_now


class BlobStore(Protocol):
    """Replaceable blob boundary for filesystem or object-storage backends."""

    def put(self, blob_id: str, data: bytes) -> None: ...

    def get(self, blob_id: str) -> bytes: ...

    def delete(self, blob_id: str) -> bool: ...


@dataclass(slots=True)
class FileBlobStore:
    root: Path

    def put(self, blob_id: str, data: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f"{blob_id}.tmp"
        temporary.write_bytes(data)
        temporary.replace(self.root / blob_id)

    def get(self, blob_id: str) -> bytes:
        return (self.root / blob_id).read_bytes()

    def delete(self, blob_id: str) -> bool:
        path = self.root / blob_id
        if not path.exists():
            return False
        path.unlink()
        return True


class MediaStore:
    """Stable media resources scoped to one user Agent."""

    ALLOWED_CONTENT_TYPES = frozenset(
        {
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "audio/mpeg",
            "audio/wav",
            "audio/ogg",
            "application/pdf",
            "text/plain",
        }
    )
    MAX_ITEMS = 1000
    MAX_TOTAL_BYTES = 1024 * 1024 * 1024

    def __init__(self, root: Path, blob_store: BlobStore | None = None) -> None:
        self.root = root
        self.blob_store = blob_store or FileBlobStore(root / "blobs")
        self.metadata_path = root / "index.json"
        self._items = self._load()

    def put(self, data: bytes, *, filename: str, content_type: str) -> dict[str, Any]:
        if not data:
            raise ValueError("Runtime media upload must not be empty")
        if content_type not in self.ALLOWED_CONTENT_TYPES:
            raise ValueError(f"Runtime media content type is not allowed: {content_type}")
        if len(self._items) >= self.MAX_ITEMS:
            raise ValueError("Runtime media item quota exceeded")
        used_bytes = sum(int(item.get("size", 0)) for item in self._items.values())
        if used_bytes + len(data) > self.MAX_TOTAL_BYTES:
            raise ValueError("Runtime media byte quota exceeded")
        media_id = str(uuid4())
        digest = hashlib.sha256(data).hexdigest()
        self.blob_store.put(media_id, data)
        safe_filename = "".join(
            character
            for character in Path(filename).name
            if character.isprintable() and character not in {'"', "\\", "/"}
        ).strip()
        item = {
            "media_id": media_id,
            "filename": safe_filename or "upload",
            "content_type": content_type,
            "size": len(data),
            "sha256": digest,
            "created_at": utc_now().isoformat(),
        }
        self._items[media_id] = item
        self._save()
        return dict(item)

    def get(self, media_id: str) -> tuple[dict[str, Any], bytes]:
        item = self._items.get(media_id)
        if item is None:
            raise ValueError(f"Runtime media does not exist: {media_id}")
        return dict(item), self.blob_store.get(media_id)

    def list(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._items.values()]

    def delete(self, media_id: str) -> bool:
        if media_id not in self._items:
            return False
        self.blob_store.delete(media_id)
        del self._items[media_id]
        self._save()
        return True

    def resolve_content_parts(self, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for part in parts:
            if part.get("type") != "media":
                resolved.append(dict(part))
                continue
            media_id = part.get("media_id")
            if not isinstance(media_id, str):
                raise ValueError("Runtime media content part requires media_id")
            item, data = self.get(media_id)
            content_type = item["content_type"]
            if not content_type.startswith("image/"):
                raise ValueError(
                    f"Runtime model input does not yet support uploaded type: {content_type}"
                )
            resolved.append(
                {
                    "type": "image",
                    "image": {
                        "data": base64.b64encode(data).decode(),
                        "mime_type": content_type,
                        **({"detail": part["detail"]} if part.get("detail") else {}),
                    },
                    "media_id": media_id,
                }
            )
        return resolved

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.metadata_path.exists():
            return {}
        try:
            data = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.metadata_path)
