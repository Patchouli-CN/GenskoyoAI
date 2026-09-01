"""租户会话前置装配（nb2 `_ensure_session`）定向测试

覆盖：已装配时幂等跳过、未装配时 ensure 并写回 store、ensure 失败时降级不抛
且不写 store。plugin 经 nonebot.init 导入，模块状态以 mock 替换（对齐
test_nb2_reminders）。

原属 test_nb2_lookup.py；lookup 注意力种类已于 de17ed1 删除，该文件其余用例
随之失效，仅本组覆盖仍有效（`_ensure_session` 仍被 `_process_batch` 调用）。
"""

import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init(driver="~fastapi")  # plugin 导入期 get_driver() 需要驱动实例

from GensokyoAI.backends.nb2 import plugin  # noqa: E402
from GensokyoAI.runtime.host import RuntimeRpcError  # noqa: E402


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


class EnsureSessionTests(unittest.IsolatedAsyncioTestCase):
    """租户前置装配：幂等 + 未装配时 ensure + 失败降级。"""

    def setUp(self):
        self._patcher = patch.object(plugin, "_store", _FakeStore())
        self._patcher.start()
        plugin._initialized.discard("qq-user-999")

    def tearDown(self):
        self._patcher.stop()
        plugin._initialized.discard("qq-user-999")  # 跨用例泄漏会让幂等断言假绿

    async def test_already_initialized_is_noop(self):
        plugin._initialized.add("qq-user-999")
        plugin._store.put("user:999", session_id="s1", revision=1)
        with patch.object(plugin, "_ensure_agent", AsyncMock()) as ensure:
            await plugin._ensure_session("qq-user-999", "user:999")
            ensure.assert_not_awaited()

    async def test_not_initialized_ensures_and_puts(self):
        with patch.object(plugin, "_ensure_agent", AsyncMock(return_value=("s9", 3))) as ensure:
            await plugin._ensure_session("qq-user-999", "user:999")
            ensure.assert_awaited_once_with("qq-user-999", None)
        self.assertEqual(
            plugin._store.get("user:999"),
            {"agent_id": "qq-user-999", "session_id": "s9", "revision": 3},
        )

    async def test_ensure_failure_degrades_without_put(self):
        with patch.object(
            plugin,
            "_ensure_agent",
            AsyncMock(side_effect=RuntimeRpcError("internal_error", "boom")),
        ):
            await plugin._ensure_session("qq-user-999", "user:999")  # 不抛
        self.assertIsNone(plugin._store.get("user:999"))


if __name__ == "__main__":
    unittest.main()
