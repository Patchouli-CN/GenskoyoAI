from __future__ import annotations

import asyncio
import contextlib
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


async def _as_chat_user(service: RuntimeService, user_id: str, method: str, params: dict | None = None):
    """以无 admin 角色的普通聊天身份调用（审计闸门测试专用）。"""
    token = set_current_principal(
        RuntimePrincipal(user_id=user_id, roles=frozenset({"read", "chat"}), auth_type="test")
    )
    try:
        return await service.handle(method, params)
    finally:
        reset_current_principal(token)


def test_world_init_rejects_custom_config_for_non_admin() -> None:
    """审计修复：world.init 与 agent.init 同一道闸门——非 admin 禁止全部自定义配置参数。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            for forbidden_params in (
                {"config_path": "custom.yaml"},
                {"character_path": "char.yaml"},
                {"model_overrides": {"model": "x"}},
                {"embedding_overrides": {"model": "x"}},
            ):
                with pytest.raises(RpcError) as error:
                    await _as_chat_user(
                        service,
                        "alice",
                        "world.init",
                        {"agent_id": "w1", **forbidden_params},
                    )
                assert error.value.code == "authorization.forbidden"

    asyncio.run(run())


def test_agent_init_rejects_custom_params_for_non_admin() -> None:
    """审计修复回归：agent.init 闸门改用共享常量后行为不变。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            with pytest.raises(RpcError) as error:
                await _as_chat_user(
                    service,
                    "alice",
                    "agent.init",
                    {"agent_id": "a1", "model_overrides": {"model": "x"}},
                )
            assert error.value.code == "authorization.forbidden"

    asyncio.run(run())


def test_world_init_agent_id_matches_agent_init_contract() -> None:
    """审计修复：world.init 与 agent.init 一致——agent_id 可选，省略时由服务生成。"""

    from GensokyoAI.runtime.rpc import network_rpc_requirements

    assert "agent_id" not in network_rpc_requirements("world.init")
    assert "agent_id" not in network_rpc_requirements("agent.init")
    # 其余资源方法仍必须传 agent_id
    assert "agent_id" in network_rpc_requirements("agent.send_message")
    assert "agent_id" in network_rpc_requirements("world.send_message")


def _register_idle_tenant(service: RuntimeService, user_id: str, agent_id: str) -> RuntimeService:
    child = RuntimeService(
        service.state.root_dir,
        tenant_key=(user_id, agent_id),
        storage_root=service._tenant_storage_root(user_id, agent_id),
    )
    service._tenant_services[(user_id, agent_id)] = child
    return child


def test_tenant_limit_evicts_least_active_idle_tenant() -> None:
    """达到租户上限：休眠最久未活跃租户（而不是硬拒绝），再发言可原样唤醒。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            service._tenant_agent_limit = lambda: 2  # type: ignore[method-assign]
            _register_idle_tenant(service, "alice", "old")
            _register_idle_tenant(service, "alice", "fresh")
            service._tenant_last_active[("alice", "old")] = 1.0
            service._tenant_last_active[("alice", "fresh")] = 9.0

            # agent.init 新租户：先驱逐 old，随后的装配因角色文件不存在而失败——
            # 这里只断言驱逐已经发生
            with pytest.raises(FileNotFoundError):
                await _as_user(
                    service, "alice", "agent.init", {"agent_id": "new", "character": "ghost"}
                )

            assert ("alice", "old") not in service._tenant_services
            assert ("alice", "old") not in service._tenant_last_active
            assert ("alice", "fresh") in service._tenant_services

    asyncio.run(run())


def test_tenant_limit_raises_only_when_all_busy() -> None:
    """所有租户都在处理请求时才报 agent.limit_exceeded（真正的背压）。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            service._tenant_agent_limit = lambda: 1  # type: ignore[method-assign]
            busy = _register_idle_tenant(service, "alice", "busy")
            await busy._tenant_operation_lock.acquire()
            try:
                with pytest.raises(RpcError) as error:
                    await _as_user(service, "alice", "agent.init", {"agent_id": "new"})
                assert error.value.code == "agent.limit_exceeded"
                assert error.value.details == {"maximum": 1}
                # 忙碌租户不被驱逐
                assert ("alice", "busy") in service._tenant_services
            finally:
                busy._tenant_operation_lock.release()

    asyncio.run(run())


def test_tenant_dispatch_marks_last_active() -> None:
    """每次租户 RPC 派发都刷新活跃时间，活跃租户不会被当作休眠候选。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            _register_idle_tenant(service, "alice", "agent-1")
            # 未装配 Agent 的调用结果不影响活跃度标记
            with contextlib.suppress(Exception):
                await _as_user(service, "alice", "session.list", {"agent_id": "agent-1"})
            assert service._tenant_last_active[("alice", "agent-1")] > 0.0

    asyncio.run(run())


def test_tenant_agent_limit_reads_config_default() -> None:
    """上限来自 resource_control.tenant_max_agents_per_user（模板默认 32）。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        service = RuntimeService(Path(temp_dir))
        assert service._tenant_agent_limit() == 32
