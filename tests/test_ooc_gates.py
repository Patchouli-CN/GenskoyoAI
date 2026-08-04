"""OOC 投递前自查的门控回归：World 回合与流式实时消费必须跳过——
流式 chunk 早已投递给用户，重写只改进记忆的版本会造成
「用户看到的 ≠ 角色记住的」分叉；舞台旁白按 QQ 口语标准会误改。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from GensokyoAI.core.agent import Agent
from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import StreamChunk, UnifiedResponse
from GensokyoAI.core.config import (
    AppConfig,
    CharacterConfig,
    InitiativeTimerConfig,
    MemoryConfig,
    ModelConfig,
    OocJudgeConfig,
    SessionConfig,
    ThinkEngineConfig,
)


class _EchoProvider(BaseProvider):
    async def chat(self, model, messages, tools=None, options=None, **kwargs):
        return UnifiedResponse(model=model)

    async def chat_stream(self, model, messages, tools=None, options=None, **kwargs):
        yield StreamChunk(content="原汁原味回复")


class _FakeReplyer:
    def __init__(self):
        self.calls = 0

    async def ensure_in_character(self, text, **kwargs):
        self.calls += 1
        return "被重写的话"


def _make_config(tmp: str) -> AppConfig:
    return AppConfig(
        character=CharacterConfig(name="Marisa", system_prompt="你是魔理沙。"),
        model=ModelConfig(provider="ooc_gate_test", name="test-model"),
        session=SessionConfig(save_path=Path(tmp)),
        memory=MemoryConfig(semantic_enabled=False, auto_memory_enabled=False),
        think_engine=ThinkEngineConfig(enabled=False),
        initiative_timer=InitiativeTimerConfig(enabled=False),
        ooc_judge=OocJudgeConfig(enabled=False),
    )


class OocGateTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if ProviderFactory.get_provider_definition("ooc_gate_test") is None:
            ProviderFactory.register("ooc_gate_test", _EchoProvider)

    async def _boot(self, tmp: str):
        with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
            agent = Agent(config=_make_config(tmp))
        await agent.create_session()
        await agent.start()
        replyer = _FakeReplyer()
        agent._replyer = replyer
        return agent, replyer

    async def test_nonstream_send_runs_ooc(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, replyer = await self._boot(tmp)
            try:
                await agent.send("你好")
                self.assertEqual(replyer.calls, 1)
            finally:
                await agent.shutdown()

    async def test_stream_send_skips_ooc_and_cleans_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, replyer = await self._boot(tmp)
            try:
                chunks = [chunk async for chunk in agent.send_stream("你好")]
                self.assertEqual(replyer.calls, 0)  # 流式已投递，不得重写
                self.assertTrue(any(c.content == "原汁原味回复" for c in chunks))
                self.assertEqual(agent._live_stream_request_ids, set())  # 标记不泄漏
            finally:
                await agent.shutdown()

    async def test_world_turn_skips_ooc(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, replyer = await self._boot(tmp)
            try:
                await agent.send_world_turn("舞台触发", ["你在红魔馆"])
                self.assertEqual(replyer.calls, 0)
            finally:
                await agent.shutdown()


if __name__ == "__main__":
    unittest.main()
