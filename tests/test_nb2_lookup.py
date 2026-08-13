"""工具查证（AttentionThings lookup 种类）定向测试

覆盖：lookup 种类判定解析（白名单校验）、派发（经租户 ToolExecutor 执行并
注入上下文）、租户前置装配（_ensure_session 幂等 + 降级）。plugin 经
nonebot.init 导入，模块状态以 mock 替换（对齐 test_nb2_reminders）。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init(driver="~fastapi")  # plugin 导入期 get_driver() 需要驱动实例

from GensokyoAI.backends.nb2 import plugin  # noqa: E402
from GensokyoAI.core.agent.attention import AttentionVerdict  # noqa: E402


class _LookupCase(unittest.IsolatedAsyncioTestCase):
    """lookup 判定/派发共用的模块状态补丁（假 host + 假 store）。"""

    def setUp(self):
        self._patches = [
            patch.object(plugin, "_host", self._make_host()),
            patch.object(plugin, "_store", _FakeStore()),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()

    @staticmethod
    def _make_host(result: str | None = "2026-08-13 星期四 14:00:00"):
        host = SimpleNamespace()
        host.execute_lookup_tool = AsyncMock(return_value=result)
        return host


class _FakeStore:
    """够 _ensure_session 用的假 SessionStore（get/put 语义）。"""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self.put_calls: list[tuple[str, dict]] = []

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def put(self, key: str, **values) -> None:
        self._data[key] = dict(values)
        self.put_calls.append((key, dict(values)))


class LookupAttentionParseTests(unittest.TestCase):
    """判定解析：工具名白名单校验（判定全权 LLM，代码只解析）。"""

    def test_candidate_always_true(self):
        kind = plugin._LookupAttentionKind()
        self.assertTrue(kind.candidate("现在几点"))
        self.assertTrue(kind.candidate("今天天气怎么样"))  # 恒真全交 LLM

    def test_parse_valid_whitelist_tool(self):
        kind = plugin._LookupAttentionKind()
        self.assertEqual(
            kind.parse('{"tool": "get_current_time", "arguments": {}}'),
            {"tool": "get_current_time", "arguments": {}},
        )
        self.assertEqual(
            kind.parse('{"tool": "get_current_dateinfo"}'),
            {"tool": "get_current_dateinfo", "arguments": {}},
        )

    def test_parse_out_of_whitelist_tool_returns_none(self):
        kind = plugin._LookupAttentionKind()
        # 白名单外的工具（含网络/写状态工具）一律判无效
        self.assertIsNone(kind.parse('{"tool": "web_search", "arguments": {}}'))
        self.assertIsNone(kind.parse('{"tool": "remember", "arguments": {}}'))

    def test_parse_null_and_garbage(self):
        kind = plugin._LookupAttentionKind()
        self.assertIsNone(kind.parse('{"tool": null}'))
        self.assertIsNone(kind.parse("不是 JSON"))
        self.assertIsNone(kind.parse('{"intent": "reminder"}'))

    def test_tools_desc_lists_whitelist_with_descriptions(self):
        desc = plugin._build_lookup_tools_desc()
        self.assertIn("get_current_time", desc)
        self.assertIn("get_current_dateinfo", desc)
        self.assertIn("当前时间", desc)  # 描述来自真实工具定义


class LookupAttentionDispatchTests(_LookupCase):
    """派发：经租户 ToolExecutor 执行白名单工具，注入【已查证】上下文。"""

    async def test_dispatch_executes_and_returns_context(self):
        verdict = AttentionVerdict(kind="lookup", data={"tool": "get_current_time"})
        note = await plugin._dispatch_lookup(verdict, "qq-user-999")
        self.assertIsNotNone(note)
        self.assertIn("已查证", note)
        self.assertIn("2026-08-13", note)  # 工具结果进了注入上下文
        host = plugin._host
        host.execute_lookup_tool.assert_awaited_once_with(
            "qq-user-999", "get_current_time", {}
        )

    async def test_dispatch_failure_returns_none(self):
        # 租户未装配/工具失败：host 返回 None → 静默跳过（不拖垮主回复）
        verdict = AttentionVerdict(kind="lookup", data={"tool": "get_current_time"})
        host = plugin._host
        host.execute_lookup_tool.return_value = None
        note = await plugin._dispatch_lookup(verdict, "qq-user-999")
        self.assertIsNone(note)

    async def test_dispatch_passes_arguments_through(self):
        verdict = AttentionVerdict(
            kind="lookup",
            data={"tool": "get_current_dateinfo", "arguments": {"tz": "Asia/Shanghai"}},
        )
        await plugin._dispatch_lookup(verdict, "qq-user-999")
        plugin._host.execute_lookup_tool.assert_awaited_once_with(
            "qq-user-999", "get_current_dateinfo", {"tz": "Asia/Shanghai"}
        )


class EnsureSessionTests(_LookupCase):
    """租户前置装配：幂等 + 未装配时 ensure + 失败降级。"""

    async def test_already_initialized_is_noop(self):
        plugin._initialized.add("qq-user-999")
        plugin._store.put("user:999", session_id="s1", revision=1)
        with patch.object(plugin, "_ensure_agent", AsyncMock()) as ensure:
            await plugin._ensure_session("qq-user-999", "user:999")
            ensure.assert_not_awaited()
        plugin._initialized.discard("qq-user-999")

    async def test_not_initialized_ensures_and_puts(self):
        plugin._initialized.discard("qq-user-999")
        with patch.object(plugin, "_ensure_agent", AsyncMock(return_value=("s9", 3))) as ensure:
            await plugin._ensure_session("qq-user-999", "user:999")
            ensure.assert_awaited_once_with("qq-user-999", None)
        self.assertEqual(
            plugin._store.get("user:999"),
            {"agent_id": "qq-user-999", "session_id": "s9", "revision": 3},
        )
        plugin._initialized.discard("qq-user-999")

    async def test_ensure_failure_degrades_without_put(self):
        from GensokyoAI.runtime.host import RuntimeRpcError

        plugin._initialized.discard("qq-user-999")
        with patch.object(
            plugin,
            "_ensure_agent",
            AsyncMock(side_effect=RuntimeRpcError("internal_error", "boom")),
        ):
            await plugin._ensure_session("qq-user-999", "user:999")  # 不抛
        self.assertEqual(plugin._store.get("user:999"), None)


if __name__ == "__main__":
    unittest.main()
