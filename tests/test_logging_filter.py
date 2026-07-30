"""第三方框架日志 sink 过滤器测试（utils/logger.py）。

loguru 级别数值：TRACE=5, DEBUG=10, INFO=20, SUCCESS=25, WARNING=30, ERROR=40。
"""

import unittest
from types import SimpleNamespace

from GensokyoAI.utils.logger import _third_party_noise_filter


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
        self.assertTrue(
            _third_party_noise_filter(_record("GensokyoAI.backends.nb2.plugin", 10))
        )
        self.assertTrue(_third_party_noise_filter(_record("plugin", 20)))

    def test_missing_name_kept(self):
        self.assertTrue(_third_party_noise_filter(_record(None, 10)))


if __name__ == "__main__":
    unittest.main()
