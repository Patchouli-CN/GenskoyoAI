from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from GensokyoAI.core.config import SessionConfig
from GensokyoAI.runtime.event_store import RuntimeEventStore
from GensokyoAI.runtime.media_store import MediaStore
from GensokyoAI.runtime.operation_store import RuntimeOperationStore
from GensokyoAI.runtime.rpc import RpcError
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


def test_event_store_replay_limit_zero_disables_replay() -> None:
    """replay_limit=0 语义为「不回放」（nb2 适配器订阅事件时用来避免历史刷屏）。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RuntimeEventStore(Path(temp_dir) / "events.jsonl")
            await store.append({"type": "message.sent", "data": {"value": 1}})

            assert await store.replay(limit=0) == []
            assert len(await store.replay(limit=1)) == 1

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


def test_network_runtime_info_exposes_remote_resource_requirements() -> None:
    async def run() -> None:
        service = RuntimeService()
        from GensokyoAI.runtime.auth import (
            RuntimePrincipal,
            reset_current_principal,
            set_current_principal,
        )

        token = set_current_principal(
            RuntimePrincipal(
                user_id="alice",
                roles=frozenset({"read", "chat"}),
                auth_type="test",
            )
        )
        try:
            info = await service.info()
        finally:
            reset_current_principal(token)

        send = next(item for item in info["method_specs"] if item["method"] == "agent.send_message")
        status = next(item for item in info["method_specs"] if item["method"] == "message.status")
        assert set(send["params_schema"]["required"]) >= {
            "agent_id",
            "session_id",
            "expected_revision",
            "idempotency_key",
            "message",
        }
        assert send["contract_scope"] == "network"
        assert send["result_schema_complete"] is True
        assert set(status["params_schema"]["required"]) == {
            "agent_id",
            "session_id",
            "idempotency_key",
        }

    asyncio.run(run())


def test_operation_store_recovers_pending_request_without_reexecuting() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "operations.json"
            store = RuntimeOperationStore(path)
            fingerprint = store.request_fingerprint({"message": "hello"})
            created = await store.begin(
                session_id="session-1",
                idempotency_key="send-1",
                request_fingerprint=fingerprint,
                generation_id="generation-1",
            )

            recovered = RuntimeOperationStore(path).get("session-1", "send-1")

            assert created["status"] == "pending"
            assert recovered is not None
            assert recovered["status"] == "failed"
            assert recovered["error"]["code"] == "message.operation_outcome_unknown"
            assert recovered["error"]["details"]["outcome_unknown"] is True

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        "{not-json",
        "[]",
        '{"version": 1, "records": []}',
        '{"version": 1, "records": {"operation": "invalid"}}',
    ],
)
def test_operation_store_fails_closed_when_ledger_is_corrupt(payload: str) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "operations.json"
        path.write_text(payload, encoding="utf-8")

        with pytest.raises(RuntimeError, match="operation store is unreadable or corrupt"):
            RuntimeOperationStore(path)


def test_runtime_message_operation_is_persisted_and_replayed() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            storage_root = root / "tenant"
            manager = SessionManager(SessionConfig(save_path=storage_root / "sessions"), "reimu")
            session = manager.create_session()
            service = RuntimeService(
                root,
                tenant_key=("alice", "agent-1"),
                storage_root=storage_root,
            )

            class FakeAgent:
                session_manager = manager

                @staticmethod
                def resume_session(session_id: str) -> bool:
                    return manager.set_current_session(session_id)

                @staticmethod
                async def send(message: str, system_contexts: list[str] | None = None):
                    operation = cast(Any, service)._operation_store.get(
                        session.session_id, "send-1"
                    )
                    assert operation["status"] == "pending"
                    memory = manager.get_working_memory(session.session_id)
                    memory.add_message("user", message)
                    memory.add_message("assistant", "world", reasoning_content="reason")
                    manager.save_working_memory(session.session_id)
                    return SimpleNamespace(content="world", reasoning_content="reason")

            cast(Any, service.state).agent = FakeAgent()
            service.state.started = True

            first = await service.send_message(
                "hello",
                session_id=session.session_id,
                idempotency_key="send-1",
                expected_revision=session.revision,
            )
            status = await service.message_status(session.session_id, "send-1")
            replay = await service.send_message(
                "hello",
                session_id=session.session_id,
                idempotency_key="send-1",
                expected_revision=0,
            )

            assert first["generation_id"]
            assert status["status"] == "succeeded"
            assert status["result"]["content"] == "world"
            assert replay["generation_id"] == first["generation_id"]
            assert replay["idempotent_replay"] is True
            with pytest.raises(RpcError) as conflict:
                await service.send_message(
                    "different",
                    session_id=session.session_id,
                    idempotency_key="send-1",
                    expected_revision=session.revision,
                )
            assert getattr(conflict.value, "code", None) == "message.idempotency_conflict"

    asyncio.run(run())


def test_runtime_message_operation_records_provider_failure() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            storage_root = root / "tenant"
            manager = SessionManager(SessionConfig(save_path=storage_root / "sessions"), "reimu")
            session = manager.create_session()
            service = RuntimeService(
                root,
                tenant_key=("alice", "agent-1"),
                storage_root=storage_root,
            )

            class FailingAgent:
                session_manager = manager

                @staticmethod
                def resume_session(session_id: str) -> bool:
                    return manager.set_current_session(session_id)

                @staticmethod
                async def send(message: str, system_contexts: list[str] | None = None):
                    operation = cast(Any, service)._operation_store.get(
                        session.session_id, "send-fail"
                    )
                    assert operation["status"] == "pending"
                    raise ValueError("provider failed")

            cast(Any, service.state).agent = FailingAgent()
            service.state.started = True

            with pytest.raises(ValueError, match="provider failed"):
                await service.send_message(
                    "hello",
                    session_id=session.session_id,
                    idempotency_key="send-fail",
                    expected_revision=session.revision,
                )
            status = await service.message_status(session.session_id, "send-fail")

            assert status["status"] == "failed"
            assert status["error"]["technical_message"] == "provider failed"

    asyncio.run(run())


def test_runtime_stream_cancellation_records_terminal_operation() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            storage_root = root / "tenant"
            manager = SessionManager(SessionConfig(save_path=storage_root / "sessions"), "reimu")
            session = manager.create_session()
            service = RuntimeService(
                root,
                tenant_key=("alice", "agent-1"),
                storage_root=storage_root,
            )
            stream_waiting = asyncio.Event()

            class StreamingAgent:
                session_manager = manager

                @staticmethod
                def resume_session(session_id: str) -> bool:
                    return manager.set_current_session(session_id)

                @staticmethod
                def send_stream(message: str, system_contexts: list[str] | None = None):
                    async def chunks():
                        yield SimpleNamespace(type="text", content="partial")
                        stream_waiting.set()
                        await asyncio.Event().wait()

                    return chunks()

            cast(Any, service.state).agent = StreamingAgent()
            service.state.started = True
            events: list[dict[str, Any]] = []

            async def consume() -> None:
                async for event in service.iter_message_stream(
                    "hello",
                    session_id=session.session_id,
                    idempotency_key="send-cancel",
                    expected_revision=session.revision,
                    generation_id="generation-cancel",
                ):
                    events.append(event)

            task = asyncio.create_task(consume())
            await asyncio.wait_for(stream_waiting.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            status = await service.message_status(session.session_id, "send-cancel")

            assert events[-1]["type"] == "cancelled"
            assert status["status"] == "cancelled"
            assert status["error"]["code"] == "message.operation_cancelled"

    asyncio.run(run())


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
