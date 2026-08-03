"""注意力事务管线（core/agent/attention.AttentionThings）定向测试"""

import unittest

from GensokyoAI.core.agent.attention import AttentionThings, AttentionVerdict


class _FakeKind:
    """测试用事务种类：含 'order' 关键词即候选，判定输出固定 JSON。"""

    name = "fake"
    _UNSET = object()

    def __init__(self, parse_result=_UNSET):
        self.candidate_calls = 0
        self._parse_result = {"hit": True} if parse_result is self._UNSET else parse_result

    def candidate(self, text: str) -> bool:
        self.candidate_calls += 1
        return "order" in text

    def judge_prompt(self, text: str) -> str:
        return f"judge: {text}"

    def parse(self, raw: str):
        return self._parse_result


class AttentionThingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefilter_skips_judge_call(self):
        calls = []

        async def generate(prompt: str) -> str:
            calls.append(prompt)
            return "{}"

        things = AttentionThings(generate)
        things.register(_FakeKind())
        verdicts = await things.inspect("nothing relevant here")
        self.assertEqual(verdicts, [])
        self.assertEqual(calls, [])  # 预筛不过：零 LLM 调用

    async def test_candidate_produces_verdict(self):
        async def generate(prompt: str) -> str:
            return '{"hit": true}'

        things = AttentionThings(generate)
        things.register(_FakeKind())
        verdicts = await things.inspect("order something")
        self.assertEqual(verdicts, [AttentionVerdict(kind="fake", data={"hit": True})])

    async def test_parse_none_means_no_verdict(self):
        async def generate(prompt: str) -> str:
            return "not a hit"

        things = AttentionThings(generate)
        things.register(_FakeKind(parse_result=None))
        verdicts = await things.inspect("order something")
        self.assertEqual(verdicts, [])

    async def test_judge_failure_silently_skipped(self):
        async def generate(prompt: str) -> str:
            raise RuntimeError("模型炸了")

        things = AttentionThings(generate)
        things.register(_FakeKind())
        verdicts = await things.inspect("order something")  # 不抛出
        self.assertEqual(verdicts, [])

    async def test_disabled_and_blank_text(self):
        async def generate(prompt: str) -> str:
            raise AssertionError("不应被调用")

        things = AttentionThings(generate, enabled=False)
        things.register(_FakeKind())
        self.assertEqual(await things.inspect("order something"), [])
        things.enabled = True
        self.assertEqual(await things.inspect("   "), [])

    async def test_duplicate_name_registration_ignored(self):
        things = AttentionThings(lambda p: None)
        kind = _FakeKind()
        things.register(kind)
        things.register(kind)
        self.assertEqual(len(things._kinds), 1)

    async def test_only_filter_skips_other_kinds(self):
        calls = []

        async def generate(prompt: str) -> str:
            calls.append(prompt)
            return "{}"

        things = AttentionThings(generate)
        things.register(_FakeKind())
        other = _FakeKind()
        other.name = "other"
        things.register(other)
        # only 指定 fake：other 连 candidate 都不跑，零调用
        verdicts = await things.inspect("order something", only={"fake"})
        self.assertEqual([v.kind for v in verdicts], ["fake"])
        self.assertEqual(other.candidate_calls, 0)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
