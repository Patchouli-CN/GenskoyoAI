from __future__ import annotations

import asyncio
import contextlib
import tempfile
from collections import deque
from pathlib import Path

import pytest

from GensokyoAI.core.agent.types import ModelInfo, ProviderCapability
from GensokyoAI.core.config import ToolConfig
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


async def _as_chat_user(
    service: RuntimeService, user_id: str, method: str, params: dict | None = None
):
    """以无 admin 角色的普通聊天身份调用（审计闸门测试专用）。"""
    token = set_current_principal(
        RuntimePrincipal(user_id=user_id, roles=frozenset({"read", "chat"}), auth_type="test")
    )
    try:
        return await service.handle(method, params)
    finally:
        reset_current_principal(token)


def test_world_init_rejects_custom_config_for_non_admin() -> None:
    """world.init 维持整组禁止：非 admin 禁止全部自定义配置参数（含 tool_overrides）。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            for forbidden_params in (
                {"config_path": "custom.yaml"},
                {"character_path": "char.yaml"},
                {"model_overrides": {"model": "x"}},
                {"embedding_overrides": {"model": "x"}},
                {"tool_overrides": {"enabled": False}},
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


_SAFE_MODEL_OVERRIDES = {
    "name": "other-model",
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 512,
    "think": True,
    "thinking_enabled": True,
    "reasoning_effort": "low",
    "stream": True,
}


def test_agent_init_allows_safe_override_subset_for_chat_user() -> None:
    """chat 身份可用安全子集：模型名/采样/思考/流式 + 工具总开关。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            with contextlib.suppress(Exception):
                # 过了闸门后的装配失败（临时目录无配置/角色文件）属预期，
                # 这里只关心闸门不拦安全子集
                await _as_chat_user(
                    service,
                    "alice",
                    "agent.init",
                    {
                        "agent_id": "a1",
                        "model_overrides": dict(_SAFE_MODEL_OVERRIDES),
                        "tool_overrides": {"enabled": False},
                    },
                )
            # 闸门在租户创建之前：被拒就不会留下租户槽位
            assert ("alice", "a1") in service._tenant_services

    asyncio.run(run())


def test_agent_init_rejects_unsafe_override_keys_for_chat_user() -> None:
    """安全子集外的键（端点/密钥/代理/工具清单）非 admin 一律 authorization.forbidden。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            for forbidden_params in (
                {"model_overrides": {"api_key": "sk-injected"}},
                {"model_overrides": {"provider": "openai"}},
                {"model_overrides": {"base_url": "https://gateway.invalid"}},
                {"model_overrides": {"temperature": 0.7, "api_path": "/custom"}},
                {"model_overrides": "not-a-dict"},
                {"tool_overrides": {"builtin_tools": ["time"]}},
                {"tool_overrides": {"enabled": False, "web_search": {"enabled": True}}},
                {"tool_overrides": "yes"},
                {"config_path": "custom.yaml"},
                {"embedding_overrides": {"name": "embed-x"}},
            ):
                with pytest.raises(RpcError) as error:
                    await _as_chat_user(
                        service,
                        "alice",
                        "agent.init",
                        {"agent_id": "a1", **forbidden_params},
                    )
                assert error.value.code == "authorization.forbidden"
            assert service._tenant_services == {}

    asyncio.run(run())


def test_agent_init_admin_keeps_full_override_rights() -> None:
    """admin 不受子集限制：端点/密钥类覆盖照常过闸门。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            with contextlib.suppress(Exception):
                await _as_user(
                    service,
                    "alice",
                    "agent.init",
                    {
                        "agent_id": "a1",
                        "model_overrides": {"provider": "openai", "api_key": "sk-test"},
                        "tool_overrides": {"enabled": False},
                    },
                )
            assert ("alice", "a1") in service._tenant_services

    asyncio.run(run())


def test_tool_overrides_apply_only_enabled_switch() -> None:
    """tool_overrides 只应用 enabled：白名单外的键被忽略，非布尔值报校验错误。"""
    config = ToolConfig()
    RuntimeService._apply_tool_overrides(config, {"enabled": False})
    assert config.enabled is False
    RuntimeService._apply_tool_overrides(config, {"builtin_tools": ["moon"]})
    assert config.builtin_tools == ["time", "moon", "memory", "system"]
    with pytest.raises(ValueError, match="tool.enabled"):
        RuntimeService._apply_tool_overrides(config, {"enabled": "yes"})


class _FakeModelRegistry:
    async def list_models(self, config, *, refresh=False, overrides=None):
        return [
            ModelInfo(
                id=config.name,
                name=config.name,
                capabilities=[ProviderCapability.CHAT],
                metadata={"source": "fake"},
            )
        ]


def test_model_list_works_without_agent_for_chat_user() -> None:
    """建会话前查模型目录：网络侧 model.list 不再要求 agent_id 或已装配 Agent。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            (root / "config" / "local.yaml").write_text(
                "model:\n  provider: ollama\n  name: test-model\n", encoding="utf-8"
            )
            service = RuntimeService(root)
            service._model_registry = _FakeModelRegistry()  # type: ignore[assignment]
            result = await _as_chat_user(service, "alice", "model.list")
            assert result["provider"] == "ollama"
            assert result["model"] == "test-model"
            assert [item["id"] for item in result["models"]] == ["test-model"]

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


def test_delete_tenant_busy_is_rejected() -> None:
    """删除在途租户被拒绝（03#4）：在途/持锁时 agent.delete 报 agent.busy。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            busy = _register_idle_tenant(service, "alice", "busy")
            busy._tenant_in_flight = 1  # 模拟等锁者/持锁者
            try:
                with pytest.raises(RpcError) as error:
                    await _as_user(service, "alice", "agent.delete", {"agent_id": "busy"})
                assert error.value.code == "agent.busy"
                assert ("alice", "busy") in service._tenant_services  # 未被删
            finally:
                busy._tenant_in_flight = 0

    asyncio.run(run())


def test_concurrent_init_same_agent_creates_single_service() -> None:
    """TOCTOU 修复（03#3）：同一新 agent_id 并发 get-or-create 只建一个 service。"""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(Path(temp_dir))
            results = await asyncio.gather(
                service._get_or_create_tenant_service("alice", "same"),
                service._get_or_create_tenant_service("alice", "same"),
            )
            # 两次拿到同一个 service，且只注册了一个（不泄漏）
            assert results[0][0] is results[1][0]
            assert len(service._tenant_services) == 1

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


def test_host_system_status_counts_tenants_and_operations() -> None:
    """get_system_status：按 agent_id 前缀统计开户数，附带在途操作数与延迟。"""
    from GensokyoAI.runtime.host import RuntimeHost

    with tempfile.TemporaryDirectory() as temp_dir:
        service = RuntimeService(Path(temp_dir))
        _register_idle_tenant(service, "nb2", "qq-group-111")
        _register_idle_tenant(service, "nb2", "qq-group-222")
        _register_idle_tenant(service, "nb2", "qq-user-333")
        service._active_network_operations = 3

        status = RuntimeHost(service=service).get_system_status()
        assert status["tenants"] == {"groups": 2, "users": 1, "other": 0}
        assert status["active_operations"] == 3
        assert status["latency"] == {"count": 0}  # 租户均未装配 Agent → 空延迟

        # 内心戏延迟聚合自各租户 Agent 的模型客户端（nb2-meta 元租户已删）
        from types import SimpleNamespace

        client_a = SimpleNamespace(
            _latency_samples=deque([("think_engine", 1000.0), ("chat", 9000.0)]),
            _cost_samples=deque(),  # 假客户端与真 ModelClient 同形状（§8.48 直接属性访问）
        )
        client_b = SimpleNamespace(
            _latency_samples=deque([("think_engine", 2000.0), ("think_engine", 3000.0)]),
            _cost_samples=deque(),
        )
        for agent_id, client in (("qq-group-111", client_a), ("qq-group-222", client_b)):
            service._tenant_services[("nb2", agent_id)].state.agent = SimpleNamespace(
                runtime_context=SimpleNamespace(model_client=client),
                semantic_memory=None,  # 假 Agent 与真 Agent 同形状（§8.48 直接属性访问）
            )
        status = RuntimeHost(service=service).get_system_status()
        assert status["latency"]["count"] == 3
        assert status["latency"]["median_ms"] == 2000.0
        assert status["latency"]["max_ms"] == 3000.0
        # runtime 闸只取 root 入口闸（模板默认 4，不随租户扩容）；
        # model 闸为每租户一套：instances = root + 3 租户
        runtime_gate = next(g for g in status["gates"] if g["name"] == "runtime")
        assert runtime_gate["max_concurrent"] == 4
        assert runtime_gate["instances"] == 1
        model_gate = next(g for g in status["gates"] if g["name"] == "model")
        assert model_gate["instances"] == 4
        assert status["load_level"] == {"level": "healthy", "reason": "运行正常"}


def test_host_load_level_transitions() -> None:
    """负载水位：满载/排队 → critical，利用率 ≥60% → warning，排空 → unavailable。"""
    from GensokyoAI.runtime.host import RuntimeHost

    with tempfile.TemporaryDirectory() as temp_dir:
        service = RuntimeService(Path(temp_dir))
        host = RuntimeHost(service=service)

        # warning：最高利用率 2/4 = 50% → healthy；3/4 = 75% → warning
        gates = [{"name": "model", "max_concurrent": 4, "active": 3, "waiting": 0}]
        assert host._compute_load_level(gates, {"count": 0})["level"] == "warning"

        # critical：满载 / 有排队
        gates = [{"name": "model", "max_concurrent": 4, "active": 4, "waiting": 0}]
        assert host._compute_load_level(gates, {"count": 0})["level"] == "critical"
        gates = [{"name": "model", "max_concurrent": 4, "active": 2, "waiting": 1}]
        result = host._compute_load_level(gates, {"count": 0})
        assert result["level"] == "critical"
        assert "排队" in result["reason"]

        # warning：思考延迟超 15s
        result = host._compute_load_level([], {"count": 3, "median_ms": 20000})
        assert result["level"] == "warning"
        assert "延迟" in result["reason"]

        # unavailable：排空
        service._draining = True
        assert host._compute_load_level([], {"count": 0})["level"] == "unavailable"
