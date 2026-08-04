"""WorldMemoryProjector（阶段 6）定向测试。

覆盖：
- 段落结束（wait_user）后批量为在场角色各写各视角记忆（一次模型调用，非每角色一次）
- 不在场角色不被写入（即使模型幻觉出她的条目，校验层也会丢弃）
- 模型失败/JSON 失败 → 确定性公开事实降级摘要，不阻塞
- config.project_perspective_memories=False 时完全停用
- 游标语义：第二次投影只包含新段落（不重复投影旧剧本）
- 后台任务不阻塞用户回复，flush_projections 可等待落笔
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import StreamChunk, UnifiedMessage, UnifiedResponse
from GensokyoAI.core.config import (
    AppConfig,
    InitiativeTimerConfig,
    MemoryConfig,
    ModelConfig,
    OocJudgeConfig,
    SceneConfig,
    SessionConfig,
    ThinkEngineConfig,
    WorldActorConfig,
    WorldConfig,
    WorldDirectorConfig,
    WorldPersistenceConfig,
)
from GensokyoAI.world import GensokyoWorld

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


def _projection(memories: list[dict]) -> str:
    return json.dumps({"memories": memories}, ensure_ascii=False)


def _memory(actor_id: str, summary: str, importance=0.8, valence=0.5, topic="测试话题") -> dict:
    return {
        "actor_id": actor_id,
        "summary": summary,
        "importance": importance,
        "emotional_valence": valence,
        "topic_name": topic,
    }


class _ProjectorProvider(BaseProvider):
    """三路假 Provider：chat() 按系统提示词内容分流导演/记忆投影，chat_stream() 出演员正文。"""

    director_script: list[str] = []
    projector_script: list = []
    reply_script: list[list[StreamChunk]] = []
    chat_calls: list[list[dict]] = []
    stream_calls: list[list[dict]] = []
    projector_delay: float = 0.0

    @classmethod
    def reset(cls, director=(), projector=(), replies=(), projector_delay=0.0) -> None:
        cls.director_script = list(director)
        cls.projector_script = list(projector)
        cls.reply_script = list(replies)
        cls.chat_calls = []
        cls.stream_calls = []
        cls.projector_delay = projector_delay

    @classmethod
    def projector_calls(cls) -> list[list[dict]]:
        """只取记忆投影调用（系统提示词含「记忆管理员」）。"""
        return [
            call
            for call in cls.chat_calls
            if call and "记忆管理员" in str(call[0].get("content", ""))
        ]

    async def chat(self, model, messages, tools=None, options=None, **kwargs):
        cls = type(self)
        cls.chat_calls.append(list(messages))
        system_text = str(messages[0].get("content", "")) if messages else ""
        if "记忆管理员" in system_text:
            if cls.projector_delay:
                await asyncio.sleep(cls.projector_delay)
            item = cls.projector_script.pop(0) if cls.projector_script else _projection([])
            if isinstance(item, Exception):
                raise item
            return UnifiedResponse(
                model=model, message=UnifiedMessage(role="assistant", content=item)
            )
        content = cls.director_script.pop(0) if cls.director_script else _decision("wait_user")
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


class _SemanticRecorder:
    """替换 Agent 语义记忆的记录器（绕过真实 TopicAwareStore 的 LLM 打分路径）。"""

    def __init__(self) -> None:
        self.adds: list[dict] = []

    async def add_async(self, content, importance=0.0, emotional_valence=0.0, topic_name=None):
        self.adds.append(
            {
                "content": content,
                "importance": importance,
                "emotional_valence": emotional_valence,
                "topic_name": topic_name,
            }
        )
        return None


def _write_characters(tmp: str) -> None:
    (Path(tmp) / "marisa.yaml").write_text(_MARISA_YAML, encoding="utf-8")
    (Path(tmp) / "patchouli.yaml").write_text(_PATCHOULI_YAML, encoding="utf-8")


def _make_config(tmp: str, *, project_memories: bool = True) -> AppConfig:
    return AppConfig(
        model=ModelConfig(provider="world_projector_test", name="test-model"),
        session=SessionConfig(save_path=Path(tmp)),
        memory=MemoryConfig(semantic_enabled=False, auto_memory_enabled=False),
        think_engine=ThinkEngineConfig(enabled=False),
        ooc_judge=OocJudgeConfig(enabled=False),  # 隔离被测逻辑，不跑投递前判定
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
            persistence=WorldPersistenceConfig(save_path=Path(tmp) / "worlds"),
            project_perspective_memories=project_memories,
        ),
    )


class WorldProjectorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("world_projector_test", _ProjectorProvider)

    async def _boot(self, tmp: str, *, director=(), projector=(), replies=(), delay=0.0, **kw):
        _ProjectorProvider.reset(director, projector, replies, projector_delay=delay)
        world = await GensokyoWorld.create(_make_config(tmp, **kw))
        self.addAsyncCleanup(world.shutdown)
        await world.start()
        recorders = {}
        for actor_id, agent in world._actors.items():
            recorders[actor_id] = _SemanticRecorder()
            agent._semantic_memory = recorders[actor_id]
        return world, recorders

    async def test_segment_end_projects_to_all_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world, recorders = await self._boot(
                tmp,
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                projector=[
                    _projection(
                        [
                            _memory("marisa", "我去红魔馆把书借走了", 0.8, 0.6, "借书风波"),
                            _memory("patchouli", "我的藏书被抢了", 0.7, -0.6, "书被抢了"),
                        ]
                    )
                ],
                replies=[[StreamChunk(content="书我借走啦DA☆ZE")]],
            )

            await world.send_message("把书放下！")
            await world.flush_projections()

            # 一次批量调用为所有在场角色生成（不是每角色一次调用）
            self.assertEqual(len(_ProjectorProvider.projector_calls()), 1)
            self.assertEqual(len(recorders["marisa"].adds), 1)
            self.assertEqual(recorders["marisa"].adds[0]["content"], "我去红魔馆把书借走了")
            self.assertAlmostEqual(recorders["marisa"].adds[0]["importance"], 0.8)
            self.assertAlmostEqual(recorders["marisa"].adds[0]["emotional_valence"], 0.6)
            self.assertEqual(recorders["marisa"].adds[0]["topic_name"], "借书风波")
            # 帕秋莉没说话但在场——作为亲历者也拿到自己视角的记忆
            self.assertEqual(len(recorders["patchouli"].adds), 1)
            self.assertEqual(recorders["patchouli"].adds[0]["content"], "我的藏书被抢了")

    async def test_projection_skips_offscene_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world, recorders = await self._boot(
                tmp,
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                projector=[
                    _projection(
                        [
                            _memory("marisa", "我在森林里被人拦下了", 0.5, 0.1, "拦路"),
                            _memory("patchouli", "（模型幻觉出的不在场者记忆）", 0.9, 0.0, "幻觉"),
                        ]
                    )
                ],
                replies=[[StreamChunk(content="别挡道DA☆ZE")]],
            )
            await world._stage.move("patchouli", "scarlet_devil_mansion")

            await world.send_message("站住，把书还我")
            await world.flush_projections()

            self.assertEqual(len(recorders["marisa"].adds), 1)
            # 不在场角色：即使模型幻觉出她的条目，校验层也会丢弃
            self.assertEqual(recorders["patchouli"].adds, [])
            prompt = str(_ProjectorProvider.projector_calls()[0][-1].get("content", ""))
            self.assertIn("marisa", prompt)
            self.assertNotIn("patchouli", prompt)

    async def test_fallback_on_model_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world, recorders = await self._boot(
                tmp,
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                projector=["这不是合法的 JSON"],
                replies=[[StreamChunk(content="火力就是正义DA☆ZE")]],
            )

            await world.send_message("又偷书？")
            await world.flush_projections()

            # 降级：确定性的公开事实摘要，低重要性，所有在场角色都有
            for actor_id in ("marisa", "patchouli"):
                self.assertEqual(len(recorders[actor_id].adds), 1, f"{actor_id} 应有降级记忆")
                add = recorders[actor_id].adds[0]
                self.assertIn("我在", add["content"])
                self.assertIn("又偷书？", add["content"])  # 公开剧本摘要
                self.assertAlmostEqual(add["importance"], 0.3)
                self.assertAlmostEqual(add["emotional_valence"], 0.0)

    async def test_projection_disabled_by_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world, recorders = await self._boot(
                tmp,
                project_memories=False,
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                replies=[[StreamChunk(content="嘿")]],
            )

            await world.send_message("说点什么吧")
            await world.flush_projections()

            self.assertEqual(_ProjectorProvider.projector_calls(), [])
            self.assertEqual(recorders["marisa"].adds, [])
            self.assertEqual(recorders["patchouli"].adds, [])

    async def test_cursor_projects_only_new_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world, _recorders = await self._boot(
                tmp,
                director=[
                    _decision("switch", "marisa"),
                    _decision("wait_user"),
                    _decision("switch", "marisa"),
                    _decision("wait_user"),
                ],
                projector=[
                    _projection([_memory("marisa", "第一段", 0.5, 0.0, "段一")]),
                    _projection([_memory("marisa", "第二段", 0.5, 0.0, "段二")]),
                ],
                replies=[
                    [StreamChunk(content="第一段回复")],
                    [StreamChunk(content="第二段回复")],
                ],
            )

            await world.send_message("第一句话说给你听")
            await world.flush_projections()
            await world.send_message("第二句话接着说")
            await world.flush_projections()

            calls = _ProjectorProvider.projector_calls()
            self.assertEqual(len(calls), 2)
            second_prompt = str(calls[1][-1].get("content", ""))
            self.assertIn("第二句话接着说", second_prompt)
            self.assertNotIn("第一句话说给你听", second_prompt)

    async def test_projection_runs_in_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world, recorders = await self._boot(
                tmp,
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                projector=[_projection([_memory("marisa", "后台写入", 0.5, 0.0, "后台")])],
                replies=[[StreamChunk(content="嗯")]],
                delay=0.3,
            )

            # 投影模型调用被人为放慢：用户回合先返回，记忆后落笔
            await world.send_message("说点什么吧")
            self.assertEqual(recorders["marisa"].adds, [])
            await world.flush_projections()
            self.assertEqual(len(recorders["marisa"].adds), 1)


if __name__ == "__main__":
    unittest.main()
