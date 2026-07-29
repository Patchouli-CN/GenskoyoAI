"""World Runtime RPC（阶段 9）定向测试。

覆盖：
- service 层 world.* RPC：init/state/roster/transcript/move/send_message[_stream]/
  session.*/shutdown 与 Agent/World 模式互斥
- 幂等账本：world.send_message 同键重放、流式帧序列
- auth required_role 的 world 分支（fallthrough 不得是 admin）
- 网络资源模型：world.* 前缀、idempotency_key 要求、租户隔离路由
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import StreamChunk, UnifiedMessage, UnifiedResponse
from GensokyoAI.runtime.auth import RuntimePrincipal, required_role, set_current_principal
from GensokyoAI.runtime.rpc import RpcError, network_rpc_requirements, rpc_methods
from GensokyoAI.runtime.service import RuntimeService

_MARISA_YAML = """\
name: "雾雨魔理沙"
system_prompt: "你是雾雨魔理沙，好奇心旺盛的魔法使。"
metadata:
  description: "好奇心旺盛的魔法使"
"""

_PATCHOULI_YAML = """\
name: "帕秋莉·诺蕾姬"
system_prompt: "你是帕秋莉·诺蕾姬，不动的大图书馆。"
metadata:
  description: "不动的大图书馆"
"""

_CONFIG_TEMPLATE = """\
model:
  provider: "world_rpc_test"
  name: "test-model"
  stream: true
session:
  save_path: "{tmp}/sessions"
memory:
  semantic_enabled: false
  auto_memory_enabled: false
think_engine:
  enabled: false
initiative_timer:
  enabled: false
scene:
  enabled: false
world:
  enabled: true
  id: "testworld"
  protagonist: "__user__"
  actors:
    - id: "marisa"
      character_file: "{tmp}/marisa.yaml"
    - id: "patchouli"
      character_file: "{tmp}/patchouli.yaml"
  persistence:
    enabled: true
    save_path: "{tmp}/worlds"
  project_perspective_memories: false
"""


def _decision(action: str, next_actor: str | None = None) -> str:
    return json.dumps(
        {"action": action, "next_character": next_actor, "reason": "剧情需要", "confidence": 0.9},
        ensure_ascii=False,
    )


class _WorldRpcProvider(BaseProvider):
    """chat 只被 Director 使用；chat_stream 出 Actor 正文。"""

    director_script: list[str] = []
    reply_script: list[list[StreamChunk]] = []

    @classmethod
    def reset(cls, director=(), replies=()) -> None:
        cls.director_script = list(director)
        cls.reply_script = list(replies)

    async def chat(self, model, messages, tools=None, options=None, **kwargs):
        content = (
            type(self).director_script.pop(0)
            if type(self).director_script
            else _decision("wait_user")
        )
        return UnifiedResponse(
            model=model, message=UnifiedMessage(role="assistant", content=content)
        )

    async def chat_stream(self, model, messages, tools=None, options=None, **kwargs):
        chunks = (
            type(self).reply_script.pop(0)
            if type(self).reply_script
            else [StreamChunk(content="（过场）")]
        )
        for chunk in chunks:
            yield chunk


def _make_root(tmp: str) -> None:
    root = Path(tmp)
    (root / "marisa.yaml").write_text(_MARISA_YAML, encoding="utf-8")
    (root / "patchouli.yaml").write_text(_PATCHOULI_YAML, encoding="utf-8")
    # yaml 里写绝对路径；Windows 反斜杠需转义为正斜杠
    (root / "config.yaml").write_text(
        _CONFIG_TEMPLATE.replace("{tmp}", tmp.replace("\\", "/")),
        encoding="utf-8",
    )


def _make_service(tmp: str, *, with_storage: bool = True) -> RuntimeService:
    root = Path(tmp)
    return RuntimeService(root, storage_root=root / "runtime" if with_storage else None)


class WorldRuntimeRpcTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if ProviderFactory.get_provider_definition("world_rpc_test") is None:
            ProviderFactory.register("world_rpc_test", _WorldRpcProvider)

    async def test_world_init_state_roster_transcript_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp)
            service = _make_service(tmp)

            payload = await service.world_init(config_path="config.yaml")
            assert payload["world_id"] == "testworld"
            assert payload["session_id"]
            assert payload["started"] is True
            assert payload["waiting_for_user"] is True
            assert payload["roster"] == {"marisa": "雾雨魔理沙", "patchouli": "帕秋莉·诺蕾姬"}
            assert payload["stage"]["__user__"] == "world_default"

            state = await service.world_state()
            assert state["world_id"] == "testworld"
            assert state["resume_diagnostics"] == []

            roster = await service.world_roster()
            assert {entry["actor_id"] for entry in roster} == {"marisa", "patchouli"}
            assert all(entry["is_current"] is False for entry in roster)

            # 演一段：Director 切魔理沙 → 回复 → wait_user
            _WorldRpcProvider.reset(
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                replies=[[StreamChunk(content="书我借走了DA☆ZE")]],
            )
            await service.world_send_message("把书放下！", idempotency_key="m1")

            transcript = await service.world_transcript()
            contents = [entry["content"] for entry in transcript["entries"]]
            assert "把书放下！" in contents
            assert any("DA☆ZE" in content for content in contents)

            moved = await service.world_move(scene_id="library")
            assert moved["stage"]["__user__"] == "library"
            library_transcript = await service.world_transcript(scene_id="library")
            assert library_transcript["entry_count"] == 1
            assert library_transcript["entries"][0]["speaker_kind"] == "system"

            await service.world_shutdown()

    async def test_world_send_message_idempotent_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp)
            service = _make_service(tmp)
            await service.world_init(config_path="config.yaml")
            self.addAsyncCleanup(service.world_shutdown)

            _WorldRpcProvider.reset(
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                replies=[[StreamChunk(content="借书DA☆ZE")]],
            )
            result = await service.world_send_message("把书放下！", idempotency_key="k1")
            assert result["idempotent_replay"] is False
            assert result["world_id"] == "testworld"
            assert len(result["turns"]) == 1
            assert result["turns"][0]["actor_id"] == "marisa"
            assert result["turns"][0]["content"] == "借书DA☆ZE"
            assert result["waiting_for_user"] is True

            # 同键重放：不再调用模型（Provider script 已空也不影响结果）
            replay = await service.world_send_message("把书放下！", idempotency_key="k1")
            assert replay["idempotent_replay"] is True
            assert replay["turns"] == result["turns"]
            assert replay["generation_id"] == result["generation_id"]

            ledger = service._operation_store.get(result["session_id"], "k1")
            assert ledger is not None
            assert ledger["status"] == "succeeded"

    async def test_world_message_stream_events_and_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp)
            service = _make_service(tmp)
            await service.world_init(config_path="config.yaml")
            self.addAsyncCleanup(service.world_shutdown)

            _WorldRpcProvider.reset(
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                replies=[[StreamChunk(content="借"), StreamChunk(content="书")]],
            )
            events = [
                event
                async for event in service.iter_world_message_stream(
                    "把书放下！", idempotency_key="s1"
                )
            ]
            types = [event["type"] for event in events]
            assert types == [
                "world.actor.started",
                "world.actor.chunk",
                "world.actor.chunk",
                "world.actor.completed",
                "world.waiting_user",
                "world.finish",
            ]
            finish = events[-1]
            assert finish["turns"][0]["content"] == "借书"
            assert finish["idempotent_replay"] is False
            assert all(event.get("generation_id") for event in events)

            # 聚合 RPC 形态：同幂等键直接重放 finish
            aggregate = await service.world_send_message_stream("把书放下！", idempotency_key="s1")
            assert aggregate["idempotent_replay"] is True
            assert aggregate["events"][-1]["type"] == "world.finish"

    async def test_world_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp)
            service = _make_service(tmp)
            first = await service.world_init(config_path="config.yaml")
            first_session = first["session_id"]

            listed = await service.world_session_list()
            assert listed["world_id"] == "testworld"
            assert [record["session_id"] for record in listed["sessions"]] == [first_session]

            second = await service.world_session_create()
            second_session = second["session_id"]
            assert second_session != first_session
            listed = await service.world_session_list()
            assert len(listed["sessions"]) == 2

            resumed = await service.world_session_resume(first_session)
            assert resumed["session_id"] == first_session

            exported = await service.world_session_export(second_session)
            assert exported["format"] == "gensokyoai.world.session.export"
            assert exported["world_session"]["session_id"] == second_session

            deleted = await service.world_session_delete(second_session)
            assert deleted["deleted"] is True
            listed = await service.world_session_list()
            assert len(listed["sessions"]) == 1

            # 活动存档拒绝删除
            with self.assertRaises(RpcError) as active_error:
                await service.world_session_delete(first_session)
            assert active_error.exception.code == "world.session_active"

            await service.world_shutdown()
            # World 已关闭：world_id 需显式给出
            deleted = await service.world_session_delete(first_session, world_id="testworld")
            assert deleted["deleted"] is True

    async def test_world_shutdown_and_not_initialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp)
            service = _make_service(tmp)
            with self.assertRaises(RpcError) as not_init:
                await service.world_state()
            assert not_init.exception.code == "world.not_initialized"

            await service.world_init(config_path="config.yaml")
            result = await service.world_shutdown()
            assert result["ok"] is True
            assert service.state.world is None
            with self.assertRaises(RpcError):
                await service.world_state()

    async def test_world_and_agent_modes_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp)
            service = _make_service(tmp)
            service.state.agent = object()  # type: ignore[assignment]
            with self.assertRaises(RpcError) as agent_active:
                await service.world_init(config_path="config.yaml")
            assert agent_active.exception.code == "world.agent_mode_active"
            service.state.agent = None

            await service.world_init(config_path="config.yaml")
            self.addAsyncCleanup(service.world_shutdown)
            with self.assertRaises(RpcError) as world_active:
                await service.init()
            assert world_active.exception.code == "world.world_mode_active"

    async def test_world_info_advertises_capability_and_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp)
            service = _make_service(tmp)
            info = await service.info()
            assert "world.orchestration" in info["capabilities"]
            for method in (
                "world.init",
                "world.start",
                "world.send_message",
                "world.send_message_stream",
                "world.state",
                "world.roster",
                "world.transcript",
                "world.move",
                "world.session.create",
                "world.session.list",
                "world.session.resume",
                "world.session.delete",
                "world.session.export",
                "world.shutdown",
            ):
                assert method in info["methods"]
                assert method in rpc_methods()


class WorldRuntimeAuthTests(unittest.TestCase):
    def test_required_role_world_branch_not_admin_fallthrough(self):
        for method in (
            "world.init",
            "world.start",
            "world.send_message",
            "world.send_message_stream",
            "world.move",
            "world.session.create",
            "world.session.resume",
            "world.session.delete",
            "world.shutdown",
        ):
            assert required_role(method) == "chat", method
        for method in (
            "world.state",
            "world.roster",
            "world.transcript",
            "world.session.list",
            "world.session.export",
        ):
            assert required_role(method) == "read", method

    def test_network_requirements_for_world_methods(self):
        send_requirements = network_rpc_requirements("world.send_message")
        assert {"agent_id", "idempotency_key"} <= send_requirements
        assert "session_id" not in send_requirements
        stream_requirements = network_rpc_requirements("world.send_message_stream")
        assert {"agent_id", "idempotency_key"} <= stream_requirements
        assert network_rpc_requirements("world.state") == frozenset({"agent_id"})
        assert network_rpc_requirements("world.session.list") == frozenset({"agent_id"})


class WorldRuntimeTenantTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if ProviderFactory.get_provider_definition("world_rpc_test") is None:
            ProviderFactory.register("world_rpc_test", _WorldRpcProvider)

    async def test_tenant_world_init_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp)
            root_service = RuntimeService(Path(tmp))
            token = set_current_principal(
                RuntimePrincipal(
                    user_id="alice", roles=frozenset({"read", "chat", "admin"}), auth_type="test"
                )
            )
            try:
                payload = await root_service.handle(
                    "world.init",
                    {"agent_id": "w1", "config_path": "config.yaml"},
                )
                assert payload["user_id"] == "alice"
                assert payload["agent_id"] == "w1"
                assert payload["world_id"] == "testworld"

                # World 状态在租户 service 上，root 保持无 world（网络路由不被污染）
                assert root_service.state.world is None
                tenant = root_service._tenant_services[("alice", "w1")]
                assert tenant.state.world is not None
                # 租户存档根按租户隔离
                assert str(tenant._storage_root) in str(
                    tenant._world_persistence_path  # type: ignore[arg-type]
                )

                state = await root_service.handle("world.state", {"agent_id": "w1"})
                assert state["user_id"] == "alice"
                assert state["world_id"] == "testworld"
            finally:
                from GensokyoAI.runtime.auth import reset_current_principal

                reset_current_principal(token)

            # 其他用户访问 alice 的 World → not_found
            token = set_current_principal(
                RuntimePrincipal(user_id="bob", roles=frozenset({"read", "chat"}), auth_type="test")
            )
            try:
                with self.assertRaises(RpcError) as not_found:
                    await root_service.handle("world.state", {"agent_id": "w1"})
                assert not_found.exception.code == "agent.not_found"
            finally:
                from GensokyoAI.runtime.auth import reset_current_principal

                reset_current_principal(token)
            await root_service.shutdown()

    async def test_tenant_world_send_requires_idempotency_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_root(tmp)
            root_service = RuntimeService(Path(tmp))
            token = set_current_principal(
                RuntimePrincipal(
                    user_id="alice", roles=frozenset({"read", "chat", "admin"}), auth_type="test"
                )
            )
            try:
                await root_service.handle(
                    "world.init",
                    {"agent_id": "w1", "config_path": "config.yaml"},
                )
                with self.assertRaises(RpcError) as missing_key:
                    await root_service.handle(
                        "world.send_message",
                        {"agent_id": "w1", "message": "你好"},
                    )
                assert missing_key.exception.code == "message.idempotency_key_required"
            finally:
                from GensokyoAI.runtime.auth import reset_current_principal

                reset_current_principal(token)
            await root_service.shutdown()


if __name__ == "__main__":
    unittest.main()
