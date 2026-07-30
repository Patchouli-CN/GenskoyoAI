"""适配器组装入口（GensokyoAI.adapters）生命周期测试。"""

import asyncio
import contextlib
import unittest

from GensokyoAI.adapters import serve_adapters
from GensokyoAI.runtime.host import RuntimeHost


class ServeAdaptersTests(unittest.TestCase):
    def test_start_in_order_stop_in_reverse_on_cancel(self):
        async def run():
            calls: list[str] = []

            class FakeAdapter:
                def __init__(self, name: str) -> None:
                    self.name = name

                async def start(self, host: RuntimeHost) -> None:
                    assert isinstance(host, RuntimeHost)
                    calls.append(f"start:{self.name}")

                async def stop(self) -> None:
                    calls.append(f"stop:{self.name}")

            task = asyncio.create_task(serve_adapters(FakeAdapter("a"), FakeAdapter("b")))
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.assertEqual(calls, ["start:a", "start:b", "stop:b", "stop:a"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
