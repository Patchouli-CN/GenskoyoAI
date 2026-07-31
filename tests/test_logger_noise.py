"""LoguruHandler 噪音过滤测试：uvicorn 关闭级联的两种形态都被丢弃，正常错误保留。"""

import logging as std_logging
import sys
import unittest

from GensokyoAI.utils.logger import LoguruHandler, logger


def _make_record(name: str, message: str, exc_info=None) -> std_logging.LogRecord:
    return std_logging.LogRecord(name, std_logging.ERROR, __file__, 1, message, None, exc_info)


class UvicornShutdownNoiseFilterTests(unittest.TestCase):
    def _emit_and_capture(self, record) -> list[str]:
        lines: list[str] = []
        handler_id = logger.add(lambda m: lines.append(str(m)), format="{message}", level="TRACE")
        try:
            LoguruHandler().emit(record)
        finally:
            logger.remove(handler_id)
        return lines

    def test_exc_info_variant_dropped(self):
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            record = _make_record("uvicorn.error", "Exception in ASGI application", sys.exc_info())
        self.assertEqual(self._emit_and_capture(record), [])

    def test_plain_text_traceback_variant_dropped(self):
        record = _make_record(
            "uvicorn.error", "Traceback (most recent call last):\n  ...\nKeyboardInterrupt\n"
        )
        self.assertEqual(self._emit_and_capture(record), [])
        record2 = _make_record(
            "uvicorn.send",
            "Traceback (most recent call last):\n  ...\nasyncio.exceptions.CancelledError\n",
        )
        self.assertEqual(self._emit_and_capture(record2), [])

    def test_normal_uvicorn_error_kept(self):
        record = _make_record("uvicorn.error", "Unexpected server error: connection reset")
        self.assertEqual(len(self._emit_and_capture(record)), 1)

    def test_normal_own_error_kept(self):
        record = _make_record("GensokyoAI", "Traceback (most recent call last):\nKeyboardInterrupt")
        self.assertEqual(len(self._emit_and_capture(record)), 1)


if __name__ == "__main__":
    unittest.main()
