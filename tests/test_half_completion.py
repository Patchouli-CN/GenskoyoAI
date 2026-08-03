"""HalfCompletionMessage：响应中断的半截回复计入中间状态并续说。

覆盖：
- 中断时不发 MESSAGE_SENT（半截正文与错误标记都不入工作记忆）
- 干净半截正文计入 HalfCompletionMessage 中间状态
- 下一轮生成注入「接着说完」提示词上下文（错误标记不提供给模型）
- 正常说完后按普通消息入记忆并清除中间状态
- 空半截（首 chunk 即失败）不留状态；会话切换清除状态
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from GensokyoAI.core.agent import Agent
from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.response_handler import strip_interrupt_marker
from GensokyoAI.core.agent.types import StreamChunk, UnifiedResponse
from GensokyoAI.core.config import (
    AppConfig,
    CharacterConfig,
    InitiativeTimerConfig,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
    ThinkEngineConfig,
)


class _InterruptProvider(BaseProvider):
    """fail_next 时下一次流式调用中途抛错（模拟网络中断），之后恢复正常。"""

    calls: list[list[dict]] = []
    fail_next: bool = False
    fail_content: str = "我今天想去魔法森林"

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.fail_next = False
        cls.fail_content = "我今天想去魔法森林"

    async def chat(self, model, messages, tools=None, options=None, **kwargs):
        type(self).calls.append(list(messages))
        return UnifiedResponse(model=model)

    async def chat_stream(self, model, messages, tools=None, options=None, **kwargs):
        type(self).calls.append(list(messages))
        if type(self).fail_next:
            type(self).fail_next = False
            if content := type(self).fail_content:
                yield StreamChunk(content=content)
            raise ConnectionError("connection reset")
        yield StreamChunk(content="……那边采点蘑菇，你要一起吗DA☆ZE？")


def _make_config(tmp: str) -> AppConfig:
    return AppConfig(
        character=CharacterConfig(name="Marisa", system_prompt="你是魔理沙。"),
        model=ModelConfig(provider="half_completion_test", name="test-model"),
        session=SessionConfig(save_path=Path(tmp)),
        memory=MemoryConfig(semantic_enabled=False, auto_memory_enabled=False),
        think_engine=ThinkEngineConfig(enabled=False),
        initiative_timer=InitiativeTimerConfig(enabled=False),
    )


class StripInterruptMarkerTests(unittest.TestCase):
    def test_strips_marker_and_keeps_partial(self):
        text = "我今天想去魔法森林\n[响应中断: connection reset]\n"
        self.assertEqual(strip_interrupt_marker(text), "我今天想去魔法森林")

    def test_marker_only_gives_empty(self):
        self.assertEqual(strip_interrupt_marker("\n[响应中断: boom]\n"), "")


class HalfCompletionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if ProviderFactory.get_provider_definition("half_completion_test") is None:
            ProviderFactory.register("half_completion_test", _InterruptProvider)

    async def _boot(self, tmp: str) -> Agent:
        _InterruptProvider.reset()
        with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
            agent = Agent(config=_make_config(tmp))
        await agent.create_session()
        await agent.start()
        return agent

    @staticmethod
    def _wm_texts(agent: Agent) -> list[str]:
        return [str(m.get("content", "")) for m in agent.working_memory.get_context()]

    @staticmethod
    async def _drain() -> None:
        await asyncio.sleep(0.2)

    async def test_interrupted_reply_becomes_half_completion_then_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(tmp)
            try:
                _InterruptProvider.fail_next = True
                await agent.send("今天去哪玩？")
                await self._drain()

                # 中断：半截正文与错误标记都不入工作记忆
                wm_texts = self._wm_texts(agent)
                self.assertFalse(any("魔法森林" in t for t in wm_texts))
                self.assertFalse(any("响应中断" in t for t in wm_texts))
                # 中间状态持有干净的半截正文
                self.assertIsNotNone(agent._half_completion)
                self.assertEqual(agent._half_completion.content, "我今天想去魔法森林")

                # 下一轮：续说上下文注入模型调用，错误标记不提供给模型
                await agent.send("然后呢？")
                await self._drain()
                flattened = "\n".join(
                    str(m.get("content", "")) for m in _InterruptProvider.calls[-1]
                )
                self.assertIn("你上一段话没说完", flattened)
                self.assertIn("我今天想去魔法森林", flattened)
                self.assertNotIn("响应中断", flattened)

                # 正常说完：按普通消息入记忆，中间状态清除
                wm_texts = self._wm_texts(agent)
                self.assertTrue(any("采点蘑菇" in t for t in wm_texts))
                self.assertIsNone(agent._half_completion)
            finally:
                await agent.shutdown()

    async def test_empty_partial_leaves_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(tmp)
            try:
                # 首 chunk 即失败：没有半截正文可记
                _InterruptProvider.fail_content = ""
                _InterruptProvider.fail_next = True
                await agent.send("你好")
                await self._drain()
                self.assertIsNone(agent._half_completion)
                self.assertFalse(any("响应中断" in t for t in self._wm_texts(agent)))
            finally:
                await agent.shutdown()

    async def test_session_switch_clears_half_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(tmp)
            try:
                _InterruptProvider.fail_next = True
                await agent.send("今天去哪玩？")
                await self._drain()
                self.assertIsNotNone(agent._half_completion)

                await agent.create_session()
                self.assertIsNone(agent._half_completion)
            finally:
                await agent.shutdown()


if __name__ == "__main__":
    unittest.main()
