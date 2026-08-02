"""适配器家族继承统一（RuntimeAdapter ABC）+ WebAdapter 生命周期定向测试"""

import tempfile
import unittest
from pathlib import Path

import aiohttp

from GensokyoAI.adapters import RuntimeAdapter
from GensokyoAI.backends.console import ConsoleAdapter
from GensokyoAI.backends.nb2.adapter import Nonebot2Adapter
from GensokyoAI.backends.web_server.adapter import WebAdapter


class AdapterInheritanceTests(unittest.TestCase):
    def test_all_adapters_inherit_runtime_adapter(self):
        # 2026-08-02 用户定稿：派生类不许游离，必须挂 RuntimeAdapter
        self.assertTrue(issubclass(Nonebot2Adapter, RuntimeAdapter))
        self.assertTrue(issubclass(ConsoleAdapter, RuntimeAdapter))
        self.assertTrue(issubclass(WebAdapter, RuntimeAdapter))
        self.assertIsInstance(Nonebot2Adapter(), RuntimeAdapter)


class WebAdapterLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_serves_health_and_stop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = WebAdapter(Path(tmpdir), host="127.0.0.1", port=0)
            self.assertEqual(adapter.name, "web")
            await adapter.start()
            self.assertIsNotNone(adapter._runner)
            # port=0 由系统分配端口，从 runner.addresses 取回
            _, port = adapter._runner.addresses[0]
            async with (
                aiohttp.ClientSession() as session,
                session.get(f"http://127.0.0.1:{port}/health") as response,
            ):
                self.assertEqual(response.status, 200)
            await adapter.stop()
            self.assertIsNone(adapter._runner)
            # stop 可重入不抛
            await adapter.stop()


if __name__ == "__main__":
    unittest.main()
