"""World-turn 桥接（阶段 3）定向测试。

覆盖：
- `send_world_turn(_stream)` 触发文本默认不写入 Actor 私有工作记忆，Actor 回复仍写入
- `MESSAGE_RECEIVED` 事件携带 world_turn / actor_id / record_in_working_memory metadata
- system_contexts 经事件链（MESSAGE_RECEIVED→ACTION_DECIDED→GENERATE_RESPONSE）抵达消息构建
- 工具调用后的 continuation 保留本轮 world contexts
- 单角色 send 路径：用户输入正常入记忆、事件无 world 标记；单角色 continuation 不重复注入
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from GensokyoAI.core.agent import Agent
from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import (
    StreamChunk,
    ToolCall,
    ToolCallFunction,
    UnifiedMessage,
    UnifiedResponse,
)
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
from GensokyoAI.core.events import Event, SystemEvent

_TRIGGER = "魔理沙推门进来，看见你在翻她的书堆。"
_WORLD_CONTEXTS = ["【舞台】魔理沙的家", "【在场】你、魔理沙", "【共享剧本】你：这是哪儿？"]


class _ScriptedProvider(BaseProvider):
    """按脚本流出 chunk 的假 Provider；记录每次调用的 messages。"""

    script: list[list[StreamChunk]] = []
    calls: list[list[dict]] = []

    @classmethod
    def reset(cls, script: list[list[StreamChunk]]) -> None:
        cls.script = list(script)
        cls.calls = []

    async def chat(self, model, messages, tools=None, options=None, **kwargs):
        type(self).calls.append(list(messages))
        return UnifiedResponse(model=model)

    async def chat_stream(self, model, messages, tools=None, options=None, **kwargs):
        type(self).calls.append(list(messages))
        chunks = (
            type(self).script.pop(0) if type(self).script else [StreamChunk(content="（过场）")]
        )
        for chunk in chunks:
            yield chunk


async def _fake_search(query: str = "") -> str:
    return "搜索结果：书在红魔馆地下室"


def _make_config(tmp: str) -> AppConfig:
    return AppConfig(
        character=CharacterConfig(name="Marisa", system_prompt="你是魔理沙。"),
        model=ModelConfig(provider="world_turn_test", name="test-model"),
        session=SessionConfig(save_path=Path(tmp)),
        memory=MemoryConfig(semantic_enabled=False, auto_memory_enabled=False),
        think_engine=ThinkEngineConfig(enabled=False),
        initiative_timer=InitiativeTimerConfig(enabled=False),
        ooc_judge=OocJudgeConfig(enabled=False),  # 隔离被测逻辑，不跑投递前判定
    )


def _system_texts(messages: list[dict]) -> list[str]:
    return [str(m.get("content", "")) for m in messages if m.get("role") == "system"]


class WorldTurnBridgeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("world_turn_test", _ScriptedProvider)

    async def _boot(self, tmp: str, script: list[list[StreamChunk]]) -> Agent:
        _ScriptedProvider.reset(script)
        with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
            agent = Agent(config=_make_config(tmp))
        await agent.create_session()
        await agent.start()
        return agent

    @staticmethod
    def _wm_texts(agent: Agent) -> list[str]:
        return [str(m.get("content", "")) for m in agent.working_memory.get_context()]

    async def _drain_bus(self) -> None:
        """给事件总线留出处理 MESSAGE_SENT 等下游事件的时间。"""
        await asyncio.sleep(0.2)

    async def test_world_turn_trigger_not_recorded_but_reply_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(tmp, [[StreamChunk(content="炸弹来了DA☆ZE")]])
            received: list[Event] = []

            async def _capture(event: Event) -> None:
                received.append(event)

            agent.event_bus.subscribe(SystemEvent.MESSAGE_RECEIVED, _capture)
            try:
                response = await agent.send_world_turn(_TRIGGER, _WORLD_CONTEXTS)
                self.assertIsNotNone(response)
                self.assertEqual(response.content, "炸弹来了DA☆ZE")
                await self._drain_bus()

                wm_texts = self._wm_texts(agent)
                self.assertFalse(
                    any("书堆" in text for text in wm_texts),
                    "触发文本不应写入 Actor 私有工作记忆",
                )
                self.assertTrue(
                    any("炸弹来了" in text for text in wm_texts),
                    "Actor 自己生成的回复应写入私有工作记忆",
                )

                world_events = [e for e in received if e.data.get("world_turn")]
                self.assertTrue(world_events, "MESSAGE_RECEIVED 应携带 world_turn 标记")
                self.assertEqual(world_events[0].data.get("actor_id"), "Marisa")
                self.assertIs(world_events[0].data.get("record_in_working_memory"), False)

                self.assertTrue(_ScriptedProvider.calls, "模型应被调用")
                system_texts = _system_texts(_ScriptedProvider.calls[0])
                self.assertTrue(any("【舞台】魔理沙的家" in t for t in system_texts))
                self.assertTrue(any("【共享剧本】" in t for t in system_texts))
            finally:
                await agent.shutdown()

    async def test_world_turn_trigger_reaches_model_as_ephemeral_user_message(self):
        tool_call = ToolCall(
            id="call_1",
            function=ToolCallFunction(name="fake_search", arguments={"query": "红魔馆"}),
        )
        script = [
            [
                StreamChunk(
                    is_tool_call=True,
                    tool_info={
                        "message": UnifiedMessage(
                            role="assistant", content="", tool_calls=[tool_call]
                        )
                    },
                )
            ],
            [StreamChunk(content="查到了")],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(tmp, script)
            agent.tool_registry.register(_fake_search, name="fake_search")
            try:
                await agent.send_world_turn(_TRIGGER, _WORLD_CONTEXTS)
                await self._drain_bus()

                # 首次调用：触发文本以临时 user 消息抵达模型
                first_users = [
                    str(m.get("content", ""))
                    for m in _ScriptedProvider.calls[0]
                    if m.get("role") == "user"
                ]
                self.assertTrue(
                    any("书堆" in text for text in first_users),
                    "触发文本应以临时 user 消息注入本轮生成",
                )
                # 工具 continuation：触发文本仍保留
                cont_users = [
                    str(m.get("content", ""))
                    for m in _ScriptedProvider.calls[1]
                    if m.get("role") == "user"
                ]
                self.assertTrue(
                    any("书堆" in text for text in cont_users),
                    "continuation 应保留本轮触发文本",
                )
                # 但触发文本始终不进私有工作记忆
                self.assertFalse(any("书堆" in text for text in self._wm_texts(agent)))
            finally:
                await agent.shutdown()

    async def test_world_turn_record_trigger_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(tmp, [[StreamChunk(content="哦？")]])
            try:
                await agent.send_world_turn(_TRIGGER, _WORLD_CONTEXTS, record_trigger=True)
                await self._drain_bus()
                self.assertTrue(
                    any("书堆" in text for text in self._wm_texts(agent)),
                    "record_trigger=True 时触发文本应写入私有工作记忆",
                )
            finally:
                await agent.shutdown()

    async def test_world_turn_stream_yields_chunks_and_skips_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(
                tmp, [[StreamChunk(content="火力"), StreamChunk(content="至上")]]
            )
            try:
                chunks = [
                    chunk async for chunk in agent.send_world_turn_stream(_TRIGGER, _WORLD_CONTEXTS)
                ]
                content = "".join(chunk.content or "" for chunk in chunks)
                self.assertEqual(content, "火力至上")
                await self._drain_bus()
                self.assertFalse(any("书堆" in text for text in self._wm_texts(agent)))
            finally:
                await agent.shutdown()

    async def test_tool_continuation_preserves_world_contexts(self):
        tool_call = ToolCall(
            id="call_1",
            function=ToolCallFunction(name="fake_search", arguments={"query": "红魔馆"}),
        )
        script = [
            [
                StreamChunk(
                    is_tool_call=True,
                    tool_info={
                        "message": UnifiedMessage(
                            role="assistant", content="", tool_calls=[tool_call]
                        )
                    },
                )
            ],
            [StreamChunk(content="查到了，在地下室")],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(tmp, script)
            agent.tool_registry.register(_fake_search, name="fake_search")
            try:
                response = await agent.send_world_turn(_TRIGGER, _WORLD_CONTEXTS)
                self.assertIsNotNone(response)
                await self._drain_bus()

                self.assertEqual(len(_ScriptedProvider.calls), 2, "工具调用后应发起第二次流式调用")
                continuation_system = _system_texts(_ScriptedProvider.calls[1])
                self.assertTrue(
                    any("【舞台】魔理沙的家" in t for t in continuation_system),
                    "continuation 应保留本轮舞台上下文",
                )
                self.assertTrue(
                    any("【共享剧本】" in t for t in continuation_system),
                    "continuation 应保留共享剧本",
                )
                self.assertTrue(
                    any("书在红魔馆地下室" in text for text in self._wm_texts(agent)),
                    "工具结果应写入私有工作记忆",
                )
            finally:
                await agent.shutdown()

    async def test_normal_send_unchanged_and_contexts_now_forwarded(self):
        tool_call = ToolCall(
            id="call_1",
            function=ToolCallFunction(name="fake_search", arguments={"query": "x"}),
        )
        script = [
            [
                StreamChunk(
                    is_tool_call=True,
                    tool_info={
                        "message": UnifiedMessage(
                            role="assistant", content="", tool_calls=[tool_call]
                        )
                    },
                )
            ],
            [StreamChunk(content="嗯哼")],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(tmp, script)
            agent.tool_registry.register(_fake_search, name="fake_search")
            received: list[Event] = []

            async def _capture(event: Event) -> None:
                received.append(event)

            agent.event_bus.subscribe(SystemEvent.MESSAGE_RECEIVED, _capture)
            try:
                await agent.send("帮我查一下书", ["【注入知识】测试标记"])
                await self._drain_bus()

                # 用户输入照常写入私有工作记忆
                self.assertTrue(any("帮我查一下书" in text for text in self._wm_texts(agent)))
                # 事件不携带 world 标记
                for event in received:
                    self.assertNotIn("world_turn", event.data)
                    self.assertNotIn("record_in_working_memory", event.data)

                # 事件链修复：调用方注入的 system_contexts 首次抵达消息构建
                first_system = _system_texts(_ScriptedProvider.calls[0])
                self.assertTrue(any("【注入知识】测试标记" in t for t in first_system))
                # 单角色 continuation 维持原行为：不重复注入本轮 contexts
                continuation_system = _system_texts(_ScriptedProvider.calls[1])
                self.assertFalse(any("【注入知识】" in t for t in continuation_system))
            finally:
                await agent.shutdown()


if __name__ == "__main__":
    unittest.main()
