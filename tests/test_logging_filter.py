"""第三方框架日志 sink 过滤器测试（utils/logger.py）。

loguru 级别数值：TRACE=5, DEBUG=10, INFO=20, SUCCESS=25, WARNING=30, ERROR=40。
"""

import logging as std_logging
import unittest
from types import SimpleNamespace

from loguru import logger

from GensokyoAI.utils.logger import LoguruHandler, _third_party_noise_filter


def _record(name: str | None, level_no: int):
    return {"name": name, "level": SimpleNamespace(no=level_no)}


class ThirdPartyNoiseFilterTests(unittest.TestCase):
    def test_nonebot_below_warning_dropped(self):
        self.assertFalse(_third_party_noise_filter(_record("nonebot.handle_event", 10)))
        self.assertFalse(_third_party_noise_filter(_record("nonebot", 20)))
        # nonebot 的事件流水是 SUCCESS(25) 级，同样属于刷屏噪音
        self.assertFalse(_third_party_noise_filter(_record("nonebot.log", 25)))

    def test_nonebot_warning_and_above_kept(self):
        self.assertTrue(_third_party_noise_filter(_record("nonebot", 30)))
        self.assertTrue(_third_party_noise_filter(_record("nonebot.handle_event", 40)))

    def test_uvicorn_websockets_below_warning_dropped(self):
        self.assertFalse(_third_party_noise_filter(_record("uvicorn.error", 20)))
        self.assertFalse(_third_party_noise_filter(_record("websockets.server", 20)))
        self.assertTrue(_third_party_noise_filter(_record("uvicorn.error", 40)))

    def test_own_logs_never_filtered(self):
        self.assertTrue(_third_party_noise_filter(_record("GensokyoAI.backends.nb2.plugin", 10)))
        self.assertTrue(_third_party_noise_filter(_record("plugin", 20)))

    def test_missing_name_kept(self):
        self.assertTrue(_third_party_noise_filter(_record(None, 10)))


class UvicornShutdownNoiseTests(unittest.TestCase):
    """LoguruHandler 定点丢弃 uvicorn 的 Ctrl+C ASGI 堆栈（退出噪音）。"""

    def _record_with_exc(self, name: str, exc_info) -> std_logging.LogRecord:
        return std_logging.LogRecord(
            name, std_logging.ERROR, __file__, 1, "Exception in ASGI application", (), exc_info
        )

    def test_keyboardinterrupt_asgi_noise_dropped(self):
        received = []
        handler_id = logger.add(lambda message: received.append(str(message)), level=0)
        try:
            handler = LoguruHandler()
            handler.emit(
                self._record_with_exc(
                    "uvicorn.error", (KeyboardInterrupt, KeyboardInterrupt(), None)
                )
            )
            self.assertEqual(received, [])

            # 真实的 uvicorn 错误不受影响
            handler.emit(self._record_with_exc("uvicorn.error", None))
            self.assertEqual(len(received), 1)
        finally:
            logger.remove(handler_id)

    def test_shutdown_noise_dropped_across_uvicorn_loggers(self):
        """run_asgi（KI 堆栈）与 send（CancelledError 堆栈）同属关闭噪音。"""
        import asyncio

        received = []
        handler_id = logger.add(lambda message: received.append(str(message)), level=0)
        try:
            handler = LoguruHandler()
            handler.emit(
                self._record_with_exc(
                    "uvicorn.run_asgi", (KeyboardInterrupt, KeyboardInterrupt(), None)
                )
            )
            handler.emit(
                self._record_with_exc(
                    "uvicorn.send",
                    (asyncio.CancelledError, asyncio.CancelledError(), None),
                )
            )
            self.assertEqual(received, [])
        finally:
            logger.remove(handler_id)


if __name__ == "__main__":
    unittest.main()
