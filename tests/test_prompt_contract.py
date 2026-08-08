"""prompt ↔ 解析器/清洗器 契约测试（借鉴 creature-chat OutputFormatContractTests）。

prompts.py 里给模型看的输出约定，与代码侧的解析器/清洗器存在隐式耦合：
改了 prompt 措辞而没同步解析端，是静默破坏（编译不报错）。本测试锁定这些
契约字符串——失败即提示「prompt/解析器契约被本地改动」，请两端原子同步。
"""

import inspect
import unittest

from GensokyoAI.core.agent import prompts

_SOURCE = inspect.getsource(prompts)


class PromptContractTests(unittest.TestCase):
    """锁定解析器/清洗器依赖的 prompt 条款字符串。"""

    def _assertInSource(self, needle: str) -> None:
        self.assertIn(
            needle,
            _SOURCE,
            f"契约字符串缺失: {needle!r}——若 prompt 有意修改，请同步解析器/清洗器与本测试",
        )

    def test_ooc_judge_verdict_fields(self):
        # ooc_judge.py 按这些键解析判定 JSON
        for key in (
            "ooc_score",
            "character_match",
            "naturalness",
            "copied_inner_monologue",
            "issues",
        ):
            self._assertInSource(key)

    def test_attention_verdict_keys(self):
        # nb2 plugin 注意力种类解析：reply_focus / jargon / reminder
        self._assertInSource('"focus"')
        self._assertInSource('"terms"')
        self._assertInSource('"cancel"')
        self._assertInSource('"due_at"')
        self._assertInSource('"target_name"')

    def test_framework_output_conventions(self):
        # 框架规则是清洗器的 prompt 侧约定（strip_rp_style 的存在前提）
        self._assertInSource("禁止第三人称描述自己")
        self._assertInSource("星号动作")
        self._assertInSource("禁止跳脱角色")

    def test_speaker_tag_convention(self):
        # 【昵称】是入站注入格式——prompt 与清洗器共同依赖这个约定
        self._assertInSource("【昵称】")


if __name__ == "__main__":
    unittest.main()
