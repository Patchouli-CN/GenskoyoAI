"""Runtime 健壮性回归测试（阶段 9 §5.6 审查四条修复）。

覆盖：
- WS 断连/取消落在发送窗口时，幂等账本被确定性收敛为 cancelled（不永久 pending）。
- WS 清理链不被单个任务异常截断。
- 单租户 operations.json 损坏不拖垮整个进程启动（按租户隔离）。
- dependency.install 的 pip subprocess 离开事件循环，且 timeout 钳到配置上限。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import GensokyoAI.runtime.service as service_module
from GensokyoAI.backends.web_server.http_adapter import _await_cancelled_task
from GensokyoAI.runtime.service import RuntimeService


class _FakeSessionManager:
    def __init__(self, session_id: str) -> None:
        self._session = SimpleNamespace(session_id=session_id, revision=0)

        async def _load_messages_async(_sid):
            return []

        self.persistence = SimpleNamespace(
            load_messages=lambda _sid: [], load_messages_async=_load_messages_async
        )

    def get_current_session(self):
        return self._session

    def get_session(self, session_id: str):
        return self._session if session_id == self._session.session_id else None


class _FakeAgent:
    """流式中途永不结束的最小假 Agent（模拟生成中 WS 断开）。"""

    def __init__(self, session_id: str) -> None:
        self.session_manager = _FakeSessionManager(session_id)

    async def send_stream(self, message, system_contexts=None):
        yield SimpleNamespace(type="text", content="半句", reasoning_content=None)
        await asyncio.Event().wait()  # 此后一直阻塞，直到消费者关闭流


class TestStreamCloseConvergesOperationLedger(unittest.IsolatedAsyncioTestCase):
    async def test_aclose_mid_stream_marks_operation_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = RuntimeService(root, storage_root=root / "runtime")
            service.state.agent = _FakeAgent("s1")  # type: ignore[assignment]
            service.state.started = True

            stream = service.iter_message_stream("你好", idempotency_key="k1")
            first = await anext(stream)
            assert first["type"] == "content"
            await stream.aclose()

            record = service._operation_store.get("s1", "k1") if service._operation_store else None
            assert record is not None
            assert record["status"] == "cancelled"


class TestWsCleanupChain(unittest.IsolatedAsyncioTestCase):
    async def test_await_cancelled_task_suppresses_task_exception(self) -> None:
        async def broken() -> None:
            raise RuntimeError("heartbeat send failed")

        task = asyncio.create_task(broken())
        # 不应抛出：清理链任一任务失败都不得截断后续清理
        await _await_cancelled_task(task)
        assert task.done()


class TestTenantCatalogIsolation(unittest.TestCase):
    def test_corrupt_operation_store_only_skips_that_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            probe = RuntimeService(root)

            def write_manifest(storage_root: Path, user_id: str, agent_id: str) -> None:
                storage_root.mkdir(parents=True, exist_ok=True)
                (storage_root / "agent.json").write_text(
                    json.dumps({"user_id": user_id, "agent_id": agent_id}),
                    encoding="utf-8",
                )

            good_root = probe._tenant_storage_root("bob", "b1")
            bad_root = probe._tenant_storage_root("alice", "a1")
            write_manifest(good_root, "bob", "b1")
            write_manifest(bad_root, "alice", "a1")
            # alice 的幂等账本损坏：只应跳过 alice，不影响 bob 与进程启动
            (bad_root / "operations.json").write_text("{ 这不是合法 JSON", encoding="utf-8")

            service = RuntimeService(root)
            assert ("bob", "b1") in service._tenant_services
            assert ("alice", "a1") not in service._tenant_services


class TestDependencyInstallOffload(unittest.IsolatedAsyncioTestCase):
    async def test_install_runs_off_event_loop_and_clamps_timeout(self) -> None:
        service = RuntimeService()
        configured = service._resource_control_config().dependency_install_timeout_seconds
        main_thread = threading.get_ident()
        captured: dict[str, object] = {}

        original = service_module.install_dependencies

        def fake_install(providers, *, scope, timeout):
            captured["providers"] = list(providers)
            captured["scope"] = scope
            captured["timeout"] = timeout
            captured["thread"] = threading.get_ident()
            return {"ok": True, "providers": list(providers)}

        service_module.install_dependencies = fake_install
        try:
            result = await service.install_dependencies(["ollama"], timeout=99999)
        finally:
            service_module.install_dependencies = original

        assert result["ok"] is True
        # timeout 钳到配置上限，调用方无法放大
        assert captured["timeout"] == max(1, min(99999, int(configured)))
        # pip subprocess 不再跑在事件循环线程上
        assert captured["thread"] != main_thread


if __name__ == "__main__":
    unittest.main()
