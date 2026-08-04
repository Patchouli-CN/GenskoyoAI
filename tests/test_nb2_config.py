"""nb2 适配器配置解析测试（只依赖 config.py，不 import nonebot 可跑）。"""

import unittest
from pathlib import Path

from GensokyoAI.backends.nb2.config import Nb2Config


def _env(**overrides) -> dict:
    base = {
        "GSK_NB2_OWNER_QQ": "10001",
        "GSK_NB2_OWNER_PROMPT_PATH": "config/nb2/owner_prompt.txt",
    }
    base.update(overrides)
    return base


class Nb2ConfigOwnerPromptTests(unittest.TestCase):
    def test_owner_prompt_path_parsed_from_env(self):
        config = Nb2Config.from_env(_env().get)
        self.assertEqual(config.owner_qq, frozenset({10001}))
        self.assertEqual(config.owner_prompt_path, Path("config/nb2/owner_prompt.txt"))

    def test_owner_prompt_path_defaults_none_without_env(self):
        config = Nb2Config.from_env(_env(GSK_NB2_OWNER_PROMPT_PATH="").get)
        self.assertIsNone(config.owner_prompt_path)
        # 两字段独立解析：owner_qq 清空只影响 owner_qq，不连带 owner_prompt_path
        config2 = Nb2Config.from_env(_env(GSK_NB2_OWNER_QQ="").get)
        self.assertEqual(config2.owner_qq, frozenset())
        self.assertEqual(config2.owner_prompt_path, Path("config/nb2/owner_prompt.txt"))
        # 全空 env → 两字段都默认
        config3 = Nb2Config.from_env(
            _env(GSK_NB2_OWNER_QQ="", GSK_NB2_OWNER_PROMPT_PATH="").get
        )
        self.assertEqual(config3.owner_qq, frozenset())
        self.assertIsNone(config3.owner_prompt_path)

    def test_multiple_owner_qq(self):
        config = Nb2Config.from_env(
            _env(GSK_NB2_OWNER_QQ="10001, 10002").get
        )
        self.assertEqual(config.owner_qq, frozenset({10001, 10002}))


if __name__ == "__main__":
    unittest.main()
