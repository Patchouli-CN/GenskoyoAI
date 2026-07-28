from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from GensokyoAI.runtime.auth import RuntimePrincipal, reset_current_principal, set_current_principal
from GensokyoAI.runtime.rpc import RpcError
from GensokyoAI.runtime.service import RuntimeService


async def _as_user(service: RuntimeService, user_id: str, method: str, params: dict | None = None):
    token = set_current_principal(
        RuntimePrincipal(
            user_id=user_id, roles=frozenset({"read", "chat", "admin"}), auth_type="test"
        )
    )
    try:
        return await service.handle(method, params)
    finally:
        reset_current_principal(token)


def test_tenant_agent_catalog_and_lookup_are_user_scoped() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            child = RuntimeService(
                Path(temp_dir),
                tenant_key=("alice", "agent-1"),
                storage_root=service._tenant_storage_root("alice", "agent-1"),
            )
            service._tenant_services[("alice", "agent-1")] = child

            assert [
                item["agent_id"] for item in await _as_user(service, "alice", "agent.list")
            ] == ["agent-1"]
            assert await _as_user(service, "bob", "agent.list") == []
            with pytest.raises(RpcError) as error:
                await _as_user(service, "bob", "session.list", {"agent_id": "agent-1"})
            assert error.value.code == "agent.not_found"

    asyncio.run(run())


def test_network_conversation_writes_require_explicit_concurrency_fields() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            child = RuntimeService(
                Path(temp_dir),
                tenant_key=("alice", "agent-1"),
                storage_root=service._tenant_storage_root("alice", "agent-1"),
            )
            service._tenant_services[("alice", "agent-1")] = child

            with pytest.raises(RpcError) as missing_session:
                await _as_user(
                    service,
                    "alice",
                    "agent.send_message",
                    {"agent_id": "agent-1", "message": "hello"},
                )
            assert missing_session.value.code == "session.explicit_id_required"

            with pytest.raises(RpcError) as missing_revision:
                await _as_user(
                    service,
                    "alice",
                    "agent.send_message",
                    {"agent_id": "agent-1", "session_id": "s1", "message": "hello"},
                )
            assert missing_revision.value.code == "session.expected_revision_required"

    asyncio.run(run())


def test_runtime_drain_rejects_new_work_and_waits_for_active_operation() -> None:
    async def run() -> None:
        service = RuntimeService()
        started = asyncio.Event()
        release = asyncio.Event()

        async def active_operation() -> None:
            async with service._network_operation_scope("agent.send_message"):
                started.set()
                await release.wait()

        task = asyncio.create_task(active_operation())
        await started.wait()
        service.begin_drain()
        readiness = await service.readiness()
        with pytest.raises(RpcError) as draining:
            async with service._network_operation_scope("session.list"):
                pass
        release.set()
        assert await service.wait_for_drain(timeout=1)
        await task

        assert readiness["ready"] is False
        assert readiness["active_operations"] == 1
        assert draining.value.code == "runtime.draining"

    asyncio.run(run())
