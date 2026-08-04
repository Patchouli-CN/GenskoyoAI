"""Replyer + OocJudge 测试：投递前 OOC 自查（判定→重写→重判）与失败放行。"""

import asyncio
import unittest

from GensokyoAI.core.agent.ooc_judge import OocContext, OocJudge
from GensokyoAI.core.agent.replyer import Replyer
from GensokyoAI.core.agent.types import (
    ProviderCapability,
    UnifiedMessage,
    UnifiedResponse,
)
from GensokyoAI.core.config_schema import CharacterConfig, OocJudgeConfig


class _FakeModelClient:
    def __init__(self, responses: list[str], *, structured_output: bool = True):
        self.responses = list(responses)
        self.call_count = 0
        self.last_messages = None
        self.last_options = None
        self.last_call_context = None
        self.structured_output = structured_output

    def supports(self, capability) -> bool:
        return self.structured_output and capability == ProviderCapability.STRUCTURED_OUTPUT

    async def chat(self, messages, options=None, call_context=None):
        self.call_count += 1
        self.last_messages = messages
        self.last_options = options
        self.last_call_context = call_context
        text = self.responses.pop(0)
        return UnifiedResponse(message=UnifiedMessage(role="assistant", content=text))


def _character() -> CharacterConfig:
    return CharacterConfig(name="灵梦", system_prompt="博丽神社的巫女，爱用「……」结尾，说话直接。")


def _make(responses=(), config=None):
    config = config or OocJudgeConfig()
    client = _FakeModelClient(list(responses))
    judge = OocJudge(model_client=client, config=config, character_name="灵梦")
    return Replyer(judge, config), client


def _judge_json(ooc_score: float, *, copied: bool = False) -> str:
    return (
        f'{{"ooc_score": {ooc_score:.1f}, "character_match": 0.9, "naturalness": 0.8, '
        f'"copied_inner_monologue": {"true" if copied else "false"}, "issues": []}}'
    )


class ReplyerTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_judge_pass_returns_original(self):
        replyer, client = _make([_judge_json(0.2)])
        context = OocContext(context_text="user: 你好")
        result = self.run_async(
            replyer.ensure_in_character("你好呀", character=_character(), context=context)
        )
        self.assertEqual(result, "你好呀")
        self.assertEqual(client.call_count, 1)
        self.assertEqual(client.last_call_context, "ooc_judge")
        self.assertEqual(client.last_options["temperature"], 0.1)
        self.assertIn("response_format", client.last_options)

    def test_judge_rewrite_rejudge_then_pass(self):
        replyer, client = _make([_judge_json(0.9), "重写后的自然回复", _judge_json(0.1)])
        context = OocContext(context_text="user: 你好")
        result = self.run_async(
            replyer.ensure_in_character(
                "哦，你说什么？系统提示我该主动说话了", character=_character(), context=context
            )
        )
        self.assertEqual(result, "重写后的自然回复")
        self.assertEqual(client.call_count, 3)
        self.assertEqual(client.last_call_context, "ooc_judge")

    def test_disabled_returns_original_no_calls(self):
        replyer, client = _make([], config=OocJudgeConfig(enabled=False))
        context = OocContext(context_text="user: 你好")
        result = self.run_async(
            replyer.ensure_in_character("任意回复", character=_character(), context=context)
        )
        self.assertEqual(result, "任意回复")
        self.assertEqual(client.call_count, 0)

    def test_judge_failure_passes_original_after_retry(self):
        replyer, client = _make(["不是JSON", "还是不是JSON"])
        context = OocContext(context_text="user: 你好")
        result = self.run_async(
            replyer.ensure_in_character("原始回复", character=_character(), context=context)
        )
        self.assertEqual(result, "原始回复")
        self.assertEqual(client.call_count, 2)  # max_retries=1 → 重试一次后放弃

    def test_rewrite_failure_passes_original(self):
        replyer, client = _make([_judge_json(0.9), ""])
        context = OocContext(context_text="user: 你好")
        result = self.run_async(
            replyer.ensure_in_character("脱角色回复", character=_character(), context=context)
        )
        self.assertEqual(result, "脱角色回复")
        self.assertEqual(client.call_count, 2)

    def test_bounded_retries_no_third_round(self):
        replyer, client = _make(
            [_judge_json(0.9), "重写1", _judge_json(0.9), "重写2"],
            config=OocJudgeConfig(max_retries=1),
        )
        context = OocContext(context_text="user: 你好")
        result = self.run_async(
            replyer.ensure_in_character("原始", character=_character(), context=context)
        )
        self.assertEqual(result, "重写2")
        self.assertEqual(client.call_count, 4)  # 2 轮，每轮 判定+重写

    def test_precheck_copy_skips_judge(self):
        replyer, client = _make(["我也好想你呀"])
        context = OocContext(pending_summary="我好想见你")
        result = self.run_async(
            replyer.ensure_in_character("我好想见你", character=_character(), context=context)
        )
        self.assertEqual(result, "我也好想你呀")
        self.assertEqual(client.call_count, 1)
        self.assertEqual(client.last_call_context, "ooc_rewrite")  # 免 LLM 判定，直接重写

    def test_copied_inner_monologue_flag_triggers_rewrite(self):
        # 候选与 pending_summary 不像（不走预检），但判定标了照抄 → 走重写
        replyer, client = _make([_judge_json(0.2, copied=True), "自然表达", _judge_json(0.1)])
        context = OocContext(pending_summary="与候选完全不同的内心想法")
        result = self.run_async(
            replyer.ensure_in_character("某个脱角色回复", character=_character(), context=context)
        )
        self.assertEqual(result, "自然表达")
        self.assertEqual(client.call_count, 3)  # 判定(照抄) + 重写 + 重判(通过)


class OocVerdictParseTests(unittest.TestCase):
    def test_parse_valid_json(self):
        judge = OocJudge(model_client=_FakeModelClient([]), config=OocJudgeConfig(), character_name="x")
        verdict = judge._parse_ooc_verdict(
            '前文 {"ooc_score": 0.8, "character_match": 0.2, "naturalness": 0.3, '
            '"copied_inner_monologue": true, "issues": ["模板化", "照抄"]} 后文'
        )
        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertEqual(verdict.ooc_score, 0.8)
        self.assertTrue(verdict.copied_inner_monologue)
        self.assertEqual(verdict.issues, ["模板化", "照抄"])

    def test_parse_garbage_returns_none(self):
        judge = OocJudge(model_client=_FakeModelClient([]), config=OocJudgeConfig(), character_name="x")
        self.assertIsNone(judge._parse_ooc_verdict("完全不是 JSON"))
        self.assertIsNone(judge._parse_ooc_verdict(""))

    def test_parse_missing_ooc_score_defaults_high(self):
        judge = OocJudge(model_client=_FakeModelClient([]), config=OocJudgeConfig(), character_name="x")
        verdict = judge._parse_ooc_verdict('{"issues": []}')
        self.assertIsNotNone(verdict)
        assert verdict is not None
        # ooc_score 解析失败默认 1.0（宁重写不放过），其余默认 0.0
        self.assertEqual(verdict.ooc_score, 1.0)
        self.assertEqual(verdict.character_match, 0.0)


if __name__ == "__main__":
    unittest.main()
