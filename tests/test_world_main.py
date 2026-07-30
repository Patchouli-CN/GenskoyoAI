"""GensokyoWorld 主类与状态机（阶段 5）定向测试。

覆盖：
- 装配：共享 ModelClient、Actor 独立总线/会话/记忆根、roster 与存档创建
- 开场：protagonist=__user__ 只布置舞台不说话；protagonist=角色 主动开场进剧本
- 用户回合：导演 switch/continue 调度循环、非法目标降级、离场角色不被选中
- 场景切换联动：scene_switch → WorldStage 更新 + 用户跟随 + 公开过渡事件
- World 事件与流式协议、状态快照、装配失败结构化诊断
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

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
from GensokyoAI.core.events import SystemEvent
from GensokyoAI.world import (
    DEFAULT_SCENE_ID,
    USER_OCCUPANT_ID,
    GensokyoWorld,
    SpeakerKind,
    WorldAssemblyError,
)

_MARISA_YAML = """\
name: "雾雨魔理沙"
system_prompt: "你是雾雨魔理沙，好奇心旺盛的魔法使。"
metadata:
  description: "好奇心旺盛的魔法使，喜欢「借」东西"
"""

_PATCHOULI_YAML = """\
name: "帕秋莉·诺蕾姬"
system_prompt: "你是帕秋莉·诺蕾姬，不动的大图书馆。"
metadata:
  description: "不动的大图书馆，哮喘的魔法使"
"""

_MARISA_BEGIN = """\
begin_scene:
  scene: magic_forest
  action: "正在翻她的书堆，念叨着收藏怎么又少了"
"""

_FOREST_YAML = """\
id: magic_forest
name: 魔法森林
description: "枝叶遮天蔽日的魔法森林。"
"""

_SDM_YAML = """\
id: scarlet_devil_mansion
name: 红魔馆
description: "吸血鬼居住的洋馆。"
"""


def _decision(action: str, next_actor: str | None = None) -> str:
    return json.dumps(
        {"action": action, "next_character": next_actor, "reason": "剧情需要", "confidence": 0.9},
        ensure_ascii=False,
    )


class _WorldProvider(BaseProvider):
    """双通道假 Provider：chat() 出导演决策 JSON，chat_stream() 出演员正文。"""

    director_script: list[str] = []
    reply_script: list[list[StreamChunk]] = []
    chat_calls: list[list[dict]] = []
    stream_calls: list[list[dict]] = []

    @classmethod
    def reset(cls, director=(), replies=()) -> None:
        cls.director_script = list(director)
        cls.reply_script = list(replies)
        cls.chat_calls = []
        cls.stream_calls = []

    async def chat(self, model, messages, tools=None, options=None, **kwargs):
        type(self).chat_calls.append(list(messages))
        content = (
            type(self).director_script.pop(0)
            if type(self).director_script
            else _decision("wait_user")
        )
        return UnifiedResponse(
            model=model, message=UnifiedMessage(role="assistant", content=content)
        )

    async def chat_stream(self, model, messages, tools=None, options=None, **kwargs):
        type(self).stream_calls.append(list(messages))
        chunks = (
            type(self).reply_script.pop(0)
            if type(self).reply_script
            else [StreamChunk(content="（过场）")]
        )
        for chunk in chunks:
            yield chunk


def _write_characters(tmp: str, *, marisa_begin: bool = False) -> None:
    marisa = _MARISA_YAML + (_MARISA_BEGIN if marisa_begin else "")
    (Path(tmp) / "marisa.yaml").write_text(marisa, encoding="utf-8")
    (Path(tmp) / "patchouli.yaml").write_text(_PATCHOULI_YAML, encoding="utf-8")


def _write_scenes(tmp: str) -> None:
    scenes_dir = Path(tmp) / "scenes"
    scenes_dir.mkdir(exist_ok=True)
    (scenes_dir / "magic_forest.yaml").write_text(_FOREST_YAML, encoding="utf-8")
    (scenes_dir / "scarlet_devil_mansion.yaml").write_text(_SDM_YAML, encoding="utf-8")


def _make_config(
    tmp: str,
    *,
    protagonist: str = "__user__",
    scene_enabled: bool = False,
    actors: list[WorldActorConfig] | None = None,
) -> AppConfig:
    return AppConfig(
        model=ModelConfig(provider="world_main_test", name="test-model"),
        session=SessionConfig(save_path=Path(tmp)),
        memory=MemoryConfig(semantic_enabled=False, auto_memory_enabled=False),
        think_engine=ThinkEngineConfig(enabled=False),
        initiative_timer=InitiativeTimerConfig(enabled=False),
        scene=SceneConfig(enabled=scene_enabled, library_path=Path(tmp) / "scenes"),
        world=WorldConfig(
            enabled=True,
            id="testworld",
            protagonist=protagonist,
            actors=actors
            or [
                WorldActorConfig(
                    id="marisa",
                    character_file=Path(tmp) / "marisa.yaml",
                    initial_scene="magic_forest",
                ),
                WorldActorConfig(
                    id="patchouli",
                    character_file=Path(tmp) / "patchouli.yaml",
                    initial_scene="magic_forest",
                ),
            ],
            director=WorldDirectorConfig(max_auto_turns=4, max_same_actor_turns=2),
            persistence=WorldPersistenceConfig(save_path=Path(tmp) / "worlds"),
            # 投影在 test_world_projector.py 专门覆盖；这里停用避免抢占导演脚本
            project_perspective_memories=False,
        ),
    )


class WorldMainTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("world_main_test", _WorldProvider)

    async def _boot(self, tmp: str, *, director=(), replies=(), **config_kwargs) -> GensokyoWorld:
        _WorldProvider.reset(director, replies)
        world = await GensokyoWorld.create(_make_config(tmp, **config_kwargs))
        self.addAsyncCleanup(world.shutdown)
        return world

    @staticmethod
    async def _drain() -> None:
        await asyncio.sleep(0.2)

    # ==================== 装配 ====================

    async def test_assembly_shared_brain_and_actor_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(tmp)
            marisa = world._actors["marisa"]
            patchouli = world._actors["patchouli"]

            # 共享大脑（且绑 World 总线），私有总线/会话/记忆根隔离
            self.assertIs(marisa._model_client, patchouli._model_client)
            self.assertIs(marisa._model_client, world._model_client)
            self.assertIsNot(marisa.event_bus, patchouli.event_bus)
            self.assertIsNot(marisa.event_bus, world.event_bus)
            self.assertEqual(marisa.actor_id, "marisa")
            self.assertEqual(marisa.world_id, "testworld")
            self.assertFalse(marisa._manage_initiative_timer)
            self.assertEqual(
                marisa._semantic_memory_root,
                Path(tmp) / "world" / "testworld" / "memory" / "雾雨魔理沙",
            )
            self.assertNotEqual(
                marisa.session_manager.get_current_session().session_id,
                patchouli.session_manager.get_current_session().session_id,
            )

            snapshot = world.state_snapshot()
            self.assertEqual(
                snapshot.roster, {"marisa": "雾雨魔理沙", "patchouli": "帕秋莉·诺蕾姬"}
            )
            self.assertTrue(snapshot.waiting_for_user)
            # 存档已创建（含 roster 与 actor session 关联）
            saved = list((Path(tmp) / "worlds").rglob("*.json"))
            self.assertTrue(saved, "World 存档文件应已创建")

    async def test_missing_character_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            actors = [
                WorldActorConfig(id="ghost", character_file=Path(tmp) / "nope.yaml"),
            ]
            with self.assertRaises(WorldAssemblyError) as ctx:
                await GensokyoWorld.create(_make_config(tmp, actors=actors))
            self.assertEqual(ctx.exception.diagnostics[0].code, "world.actor_file_missing")

    async def test_duplicate_display_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            # 第二张卡同名「雾雨魔理沙」——共享记忆根会互踩，装配必须硬报错
            (Path(tmp) / "marisa2.yaml").write_text(_MARISA_YAML, encoding="utf-8")
            actors = [
                WorldActorConfig(id="marisa", character_file=Path(tmp) / "marisa.yaml"),
                WorldActorConfig(id="marisa2", character_file=Path(tmp) / "marisa2.yaml"),
            ]
            with self.assertRaises(WorldAssemblyError) as ctx:
                await GensokyoWorld.create(_make_config(tmp, actors=actors))
            self.assertEqual(ctx.exception.diagnostics[0].code, "world.actor_name_collision")

    # ==================== 开场 ====================

    async def test_user_protagonist_waits_without_opening(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(tmp)
            await world.start()
            self.assertTrue(world.waiting_for_user)
            self.assertEqual(_WorldProvider.stream_calls, [])  # 不生成虚假欢迎词

    async def test_actor_protagonist_opening(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp, marisa_begin=True)
            world = await self._boot(
                tmp,
                protagonist="marisa",
                director=[_decision("wait_user")],
                replies=[[StreamChunk(content="谁在我的书堆里乱翻DA☆ZE")]],
            )
            await world.start()

            # 场景系统未启用：所有占位都在合成场景
            entries = world.transcript_history(DEFAULT_SCENE_ID)
            self.assertTrue(any("乱翻" in e.content for e in entries), "开场白应进共享剧本")
            self.assertTrue(world.waiting_for_user)
            # 开场触发来自角色卡的 begin_scene.action
            first_call = _WorldProvider.stream_calls[0]
            user_msgs = [str(m.get("content", "")) for m in first_call if m.get("role") == "user"]
            self.assertTrue(any("收藏" in text for text in user_msgs))
            # 开场后导演接管调度（after_actor → wait_user）
            self.assertEqual(len(_WorldProvider.chat_calls), 1)

    # ==================== 用户回合与导演调度 ====================

    async def test_user_turn_director_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(
                tmp,
                director=[
                    _decision("switch", "marisa"),
                    _decision("switch", "patchouli"),
                    _decision("wait_user"),
                ],
                replies=[
                    [StreamChunk(content="偷书？这叫借DA☆ZE")],
                    [StreamChunk(content="把书放下，小偷。")],
                ],
            )
            await world.start()
            turns = await world.send_message("把书还给我！")

            # 顺序由导演剧情决策而非 roster 顺序（marisa → patchouli）
            self.assertEqual([t.actor_id for t in turns], ["marisa", "patchouli"])
            self.assertEqual(turns[0].actor_name, "雾雨魔理沙")
            self.assertEqual(turns[1].content, "把书放下，小偷。")

            entries = world.transcript_history(DEFAULT_SCENE_ID)
            speakers = [(e.speaker_kind, e.speaker_id) for e in entries]
            self.assertEqual(speakers[0], (SpeakerKind.USER, USER_OCCUPANT_ID))
            self.assertEqual(speakers[1], (SpeakerKind.CHARACTER, "marisa"))
            self.assertEqual(speakers[2], (SpeakerKind.CHARACTER, "patchouli"))

            snapshot = world.state_snapshot()
            self.assertTrue(snapshot.waiting_for_user)
            self.assertEqual(snapshot.current_actor_id, "patchouli")

    async def test_continue_then_invalid_switch_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(
                tmp,
                director=[
                    _decision("switch", "marisa"),
                    _decision("continue"),
                    _decision("switch", "reimu"),  # 不在 roster → 导演降级 wait_user
                ],
                replies=[
                    [StreamChunk(content="第一句")],
                    [StreamChunk(content="第二句")],
                ],
            )
            await world.start()
            turns = await world.send_message("说话")

            self.assertEqual([t.actor_id for t in turns], ["marisa", "marisa"])
            self.assertTrue(world.waiting_for_user)

    async def test_offscene_actor_never_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(
                tmp,
                director=[_decision("switch", "patchouli")],
            )
            await world.start()
            # 帕秋莉离场 → 导演候选只剩 marisa，switch patchouli 非法降级
            await world._stage.move("patchouli", "scarlet_devil_mansion")
            turns = await world.send_message("有人在吗")
            self.assertEqual(turns, [])
            self.assertTrue(world.waiting_for_user)

            # 移回用户所在场景后可以被选中（场景未启用时大家都在合成场景）
            await world._stage.move("patchouli", DEFAULT_SCENE_ID)
            _WorldProvider.director_script.extend(
                [_decision("switch", "patchouli"), _decision("wait_user")]
            )
            _WorldProvider.reply_script.append([StreamChunk(content="我回来了")])
            turns = await world.send_message("再说一遍吧")
            self.assertEqual([t.actor_id for t in turns], ["patchouli"])

    # ==================== 场景切换联动 ====================

    async def test_scene_switch_updates_stage_and_user_follows(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            _write_scenes(tmp)
            tool_call = ToolCall(
                id="call_1",
                function=ToolCallFunction(
                    name="scene_switch", arguments={"scene_id": "scarlet_devil_mansion"}
                ),
            )
            world = await self._boot(
                tmp,
                scene_enabled=True,
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                replies=[
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
                    [StreamChunk(content="到红魔馆了DA☆ZE")],
                ],
            )
            await world.start()
            moved: list = []

            async def _capture(event):
                moved.append(event.data)

            world.event_bus.subscribe(SystemEvent.WORLD_SCENE_MOVED, _capture)
            await world.send_message("走，去红魔馆")
            await self._drain()

            stage = world.state_snapshot().stage
            self.assertEqual(stage["marisa"], "scarlet_devil_mansion")
            # 当前演员移动时用户原子跟随
            self.assertEqual(stage[USER_OCCUPANT_ID], "scarlet_devil_mansion")
            self.assertTrue(moved, "应广播 WORLD_SCENE_MOVED")
            self.assertTrue(moved[0].user_moved)
            self.assertEqual(moved[0].from_scene_id, "magic_forest")

            # 公开过渡事件与回合正文都落在目的地场景分片
            contents = [e.content for e in world.transcript_history("scarlet_devil_mansion")]
            self.assertTrue(any("来到红魔馆" in c for c in contents))
            self.assertTrue(any("DA☆ZE" in c for c in contents))
            # 旧场景分片只有用户那句话，不穿帮
            forest_contents = [e.content for e in world.transcript_history("magic_forest")]
            self.assertEqual(forest_contents, ["走，去红魔馆"])

    # ==================== 事件与流式协议 ====================

    async def test_stream_events_and_world_bus_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(
                tmp,
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                replies=[[StreamChunk(content="火"), StreamChunk(content="力")]],
            )
            await world.start()
            bus_events: list[SystemEvent] = []

            async def _capture(event):
                bus_events.append(event.type)

            for event_type in (
                SystemEvent.WORLD_ACTOR_TURN_STARTED,
                SystemEvent.WORLD_ACTOR_TURN_COMPLETED,
                SystemEvent.WORLD_WAITING_USER,
                SystemEvent.WORLD_DIRECTOR_DECISION,
            ):
                world.event_bus.subscribe(event_type, _capture)

            stream_types = [
                event["type"] async for event in world.send_message_stream("说点什么吧")
            ]
            await self._drain()

            self.assertEqual(
                stream_types,
                [
                    "world.actor.started",
                    "world.actor.chunk",
                    "world.actor.chunk",
                    "world.actor.completed",
                    "world.waiting_user",
                ],
            )
            for expected in (
                SystemEvent.WORLD_ACTOR_TURN_STARTED,
                SystemEvent.WORLD_ACTOR_TURN_COMPLETED,
                SystemEvent.WORLD_WAITING_USER,
                SystemEvent.WORLD_DIRECTOR_DECISION,
            ):
                self.assertIn(expected, bus_events)


if __name__ == "__main__":
    unittest.main()
