"""回合请求绑定回归测试（ActionExecutor 的 request_id 槽位语义）。

锁定「请求绑定终止符」：超时/取消后仍在跑的孤儿生成，其 feed/complete/WAIT
必须凭 id 判过期丢弃，不得污染下一个请求（旧事故 02#12 的回归锁）。
"""

import unittest
from types import SimpleNamespace

from GensokyoAI.core.agent.action_executor import ActionExecutor
from GensokyoAI.core.agent.types import StreamChunk
from GensokyoAI.core.events import EventBus


def _make_executor() -> ActionExecutor:
    # ActionExecutor 构造只需 agent 引用与事件总线（本组用例不触发决策路径）
    return ActionExecutor(agent=SimpleNamespace(), event_bus=EventBus(enable_trace=False))


class TurnRequestBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_resolves_current_request(self):
        executor = _make_executor()
        future = executor.prepare_response("req-1")
        executor.complete_response("你好", request_id="req-1")
        self.assertTrue(future.done())
        self.assertEqual(future.result(), "你好")

    async def test_stale_complete_does_not_resolve_new_future(self):
        executor = _make_executor()
        executor.prepare_response("req-1")
        new_future = executor.prepare_response("req-2")
        # 旧请求迟到 complete：不得解决新请求的 future
        executor.complete_response("旧回复", request_id="req-1")
        self.assertFalse(new_future.done())

    async def test_stale_chunk_dropped(self):
        executor = _make_executor()
        executor.prepare_response("req-1")
        executor.prepare_response("req-2")
        await executor.feed_chunk(
            StreamChunk(type="content", content="旧chunk"), request_id="req-1"
        )
        self.assertIsNone(executor.get_chunk_nowait())

    async def test_current_chunk_accepted(self):
        executor = _make_executor()
        executor.prepare_response("req-1")
        await executor.feed_chunk(
            StreamChunk(type="content", content="新chunk"), request_id="req-1"
        )
        chunk = executor.get_chunk_nowait()
        self.assertIsNotNone(chunk)
        self.assertEqual(chunk.content, "新chunk")

    async def test_stale_wait_ignored(self):
        executor = _make_executor()
        executor.prepare_response("req-1")
        new_future = executor.prepare_response("req-2")
        event = SimpleNamespace(data={"request_id": "req-1", "action": {"type": "WAIT"}}, id="e1")
        await executor._execute_wait(event)
        self.assertFalse(new_future.done())

    async def test_current_wait_resolves_empty(self):
        executor = _make_executor()
        future = executor.prepare_response("req-1")
        event = SimpleNamespace(data={"request_id": "req-1", "action": {"type": "WAIT"}}, id="e2")
        await executor._execute_wait(event)
        self.assertTrue(future.done())
        self.assertEqual(future.result(), "")

    async def test_legacy_unbound_event_accepted(self):
        # request_id=None 的旧式事件视为当前（兼容路径）
        executor = _make_executor()
        future = executor.prepare_response("req-1")
        executor.complete_response("兼容", request_id=None)
        self.assertTrue(future.done())


if __name__ == "__main__":
    unittest.main()
