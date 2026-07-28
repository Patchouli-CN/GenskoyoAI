from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from GensokyoAI.core.config import SessionConfig
from GensokyoAI.runtime.event_store import RuntimeEventStore
from GensokyoAI.runtime.media_store import MediaStore
from GensokyoAI.runtime.service import RuntimeService
from GensokyoAI.session.manager import SessionManager


def test_event_store_persists_sequence_and_replays_filtered_events() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            store = RuntimeEventStore(path)
            first = await store.append({"type": "message.sent", "data": {"value": 1}})
            await store.append({"type": "tool.call.started", "data": {"value": 2}})
            third = await store.append({"type": "message.sent", "data": {"value": 3}})

            reopened = RuntimeEventStore(path)
            replay = await reopened.replay(
                after_sequence=first["sequence"],
                event_types={"message.sent"},
            )

            assert [item["sequence"] for item in replay] == [third["sequence"]]
            assert reopened.latest_sequence == 3
            assert reopened.earliest_sequence == 1

    asyncio.run(run())


def test_media_store_returns_stable_resource_and_resolves_image_part() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = MediaStore(Path(temp_dir))
        item = store.put(b"png-bytes", filename="../avatar.png", content_type="image/png")

        parts = store.resolve_content_parts(
            [{"type": "text", "text": "look"}, {"type": "media", "media_id": item["media_id"]}]
        )

        assert item["filename"] == "avatar.png"
        assert parts[1]["type"] == "image"
        assert parts[1]["image"]["mime_type"] == "image/png"
        assert parts[1]["media_id"] == item["media_id"]
        public = RuntimeService._public_message({"role": "user", "content": parts})
        assert public["content"][1] == {"type": "media", "media_id": item["media_id"]}
        assert store.delete(item["media_id"])


def test_runtime_info_exposes_generated_method_schemas() -> None:
    info = asyncio.run(RuntimeService().info())
    send = next(item for item in info["method_specs"] if item["method"] == "agent.send_message")

    assert send["params_schema"]["type"] == "object"
    assert "message" in send["params_schema"]["required"]
    assert send["params_schema"]["properties"]["expected_revision"]["default"] is None
    assert "result_schema" in send


def test_idempotent_retry_precedes_stale_revision_check() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = SessionManager(SessionConfig(save_path=Path(temp_dir)), "reimu")
            session = manager.create_session()
            manager.replace_messages(
                session.session_id,
                [
                    {"role": "user", "content": "hello", "idempotency_key": "send-1"},
                    {"role": "assistant", "content": "world"},
                ],
            )
            service = RuntimeService()
            cast(Any, service.state).agent = SimpleNamespace(
                session_manager=manager,
                resume_session=manager.set_current_session,
            )
            service.state.started = True

            result = await service.send_message(
                "hello",
                session_id=session.session_id,
                idempotency_key="send-1",
                expected_revision=0,
            )

            assert result["content"] == "world"
            assert result["idempotent_replay"] is True

    asyncio.run(run())
