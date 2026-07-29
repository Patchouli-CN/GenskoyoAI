"""World 控制台后端（阶段 9）定向测试。

覆盖：
- 流式/非流式用户回合的动态发言者显示
- /world /roster /stage /transcript 命令
- 单角色会话命令的 World 模式拦截
- World 总线事件的用户回合去重与空闲主动剧情显示
"""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

from rich.console import Console as RichConsole

from GensokyoAI.backends.console.world_backend import WorldConsoleBackend
from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import StreamChunk, UnifiedMessage, UnifiedResponse
from GensokyoAI.core.config import (
    AppConfig,
    InitiativeTimerConfig,
    MemoryConfig,
    ModelConfig,
    SceneConfig,
    SessionConfig,
    ThinkEngineConfig,
    WorldActorConfig,
    WorldConfig,
    WorldDirectorConfig,
    WorldPersistenceConfig,
)
from GensokyoAI.core.events import Event, SystemEvent
from GensokyoAI.world import GensokyoWorld
from GensokyoAI.world.events import WorldActorTurnPayload

_MARISA_YAML = """\
name: "雾雨魔理沙"
system_prompt: "你是雾雨魔理沙，好奇心旺盛的魔法使。"
metadata:
  description: "好奇心旺盛的魔法使"
"""

_PATCHOULI_YAML = """\
name: "帕秋莉·诺蕾姬"
system_prompt: "你是帕秋莉·诺蕾姬，不动的大图书馆。"
metadata:
  description: "不动的大图书馆"
"""


def _decision(action: str, next_actor: str | None = None) -> str:
    return json.dumps(
        {"action": action, "next_character": next_actor, "reason": "剧情需要", "confidence": 0.9},
        ensure_ascii=False,
    )


class _ConsoleProvider(BaseProvider):
    director_script: list[str] = []
    reply_script: list[list[StreamChunk]] = []

    @classmethod
    def reset(cls, director=(), replies=()) -> None:
        cls.director_script = list(director)
        cls.reply_script = list(replies)

    async def chat(self, model, messages, tools=None, options=None, **kwargs):
        content = (
            type(self).director_script.pop(0)
            if type(self).director_script
            else _decision("wait_user")
        )
        return UnifiedResponse(
            model=model, message=UnifiedMessage(role="assistant", content=content)
        )

    async def chat_stream(self, model, messages, tools=None, options=None, **kwargs):
        chunks = (
            type(self).reply_script.pop(0)
            if type(self).reply_script
            else [StreamChunk(content="（过场）")]
        )
        for chunk in chunks:
            yield chunk


def _make_config(tmp: str) -> AppConfig:
    return AppConfig(
        model=ModelConfig(provider="world_console_test", name="test-model", stream=True),
        session=SessionConfig(save_path=Path(tmp)),
        memory=MemoryConfig(semantic_enabled=False, auto_memory_enabled=False),
        think_engine=ThinkEngineConfig(enabled=False),
        initiative_timer=InitiativeTimerConfig(enabled=False),
        scene=SceneConfig(enabled=False),
        world=WorldConfig(
            enabled=True,
            id="testworld",
            protagonist="__user__",
            actors=[
                WorldActorConfig(id="marisa", character_file=Path(tmp) / "marisa.yaml"),
                WorldActorConfig(id="patchouli", character_file=Path(tmp) / "patchouli.yaml"),
            ],
            director=WorldDirectorConfig(max_auto_turns=4, max_same_actor_turns=2),
            persistence=WorldPersistenceConfig(enabled=False),
            project_perspective_memories=False,
        ),
    )


class WorldConsoleBackendTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if ProviderFactory.get_provider_definition("world_console_test") is None:
            ProviderFactory.register("world_console_test", _ConsoleProvider)

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        (Path(tmp) / "marisa.yaml").write_text(_MARISA_YAML, encoding="utf-8")
        (Path(tmp) / "patchouli.yaml").write_text(_PATCHOULI_YAML, encoding="utf-8")
        _ConsoleProvider.reset()
        self.world = await GensokyoWorld.create(_make_config(tmp))
        self.backend = WorldConsoleBackend(self.world)
        self.output = io.StringIO()
        self.backend.console = RichConsole(
            file=self.output, force_terminal=False, width=200, legacy_windows=False
        )
        self.backend._running = True

    async def asyncTearDown(self):
        await self.world.shutdown()
        self._tmp.cleanup()

    def _printed(self) -> str:
        return self.output.getvalue()

    async def test_send_stream_displays_dynamic_speaker(self):
        _ConsoleProvider.reset(
            director=[_decision("switch", "marisa"), _decision("wait_user")],
            replies=[[StreamChunk(content="书我借走了DA☆ZE")]],
        )
        result = await self.backend.send("把书放下！")
        assert "书我借走了DA☆ZE" in result
        # 显示经 World 总线异步投递，给回调任务调度机会
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        printed = self._printed()
        assert "雾雨魔理沙: " in printed
        assert "书我借走了DA☆ZE" in printed
        assert "帕秋莉·诺蕾姬: " not in printed

    async def test_send_non_stream_displays_each_turn(self):
        self.backend.set_stream_mode(False)
        _ConsoleProvider.reset(
            director=[_decision("switch", "patchouli"), _decision("wait_user")],
            replies=[[StreamChunk(content="还给我。")]],
        )
        result = await self.backend.send("把书放下！")
        assert "还给我。" in result
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        printed = self._printed()
        assert "帕秋莉·诺蕾姬: " in printed
        assert "还给我。" in printed

    async def test_world_commands_execute(self):
        for text in ("/world", "/roster", "/stage", "/transcript"):
            self.output.truncate(0)
            self.output.seek(0)
            result = await self.backend.send(text)
            assert result == ""
            printed = self._printed()
            assert "✗" not in printed, f"{text} 应成功执行: {printed}"

        # 面板内容抽查
        await self.backend.send("/roster")
        assert "雾雨魔理沙" in self._printed()
        await self.backend.send("/stage")
        assert "你" in self._printed()

    async def test_agent_only_command_is_intercepted(self):
        result = await self.backend.send("/back")
        assert result == ""
        printed = self._printed()
        assert "World 模式下不可用" in printed
        # 拦截生效：没有进入世界回合，共享剧本仍为空
        assert self.world.transcript_history("world_default") == []

    async def test_bus_chunk_events_not_duplicated_during_user_turn(self):
        _ConsoleProvider.reset(
            director=[_decision("switch", "marisa"), _decision("wait_user")],
            replies=[[StreamChunk(content="独一无二")]],
        )
        await self.backend.send("你好")
        # 用户回合的 World 总线事件被抑制：正文只显示一次（流式那次）
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert self._printed().count("独一无二") == 1

    async def test_idle_initiative_bus_events_are_displayed(self):
        # 非用户回合（World 主循环主动剧情）：总线事件直接显示
        self.world.event_bus.publish(
            Event(
                type=SystemEvent.WORLD_ACTOR_TURN_STARTED,
                source="test",
                data=WorldActorTurnPayload(
                    actor_id="marisa", actor_name="雾雨魔理沙", scene_id="world_default"
                ),
            )
        )
        self.world.event_bus.publish(
            Event(
                type=SystemEvent.WORLD_ACTOR_TURN_CHUNK,
                source="test",
                data={"actor_id": "marisa", "actor_name": "雾雨魔理沙", "content": "主动开口"},
            )
        )
        self.world.event_bus.publish(
            Event(
                type=SystemEvent.WORLD_ACTOR_TURN_COMPLETED,
                source="test",
                data=WorldActorTurnPayload(
                    actor_id="marisa",
                    actor_name="雾雨魔理沙",
                    scene_id="world_default",
                    content="主动开口",
                ),
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        printed = self._printed()
        assert "雾雨魔理沙: " in printed
        assert "主动开口" in printed


if __name__ == "__main__":
    unittest.main()
