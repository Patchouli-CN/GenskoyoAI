"""World 持久化恢复（阶段 8）定向测试。

覆盖：
- resume 完整还原：舞台位置、共享剧本分片、actor 私有会话延续、活动存档续写
- 会话不存在 → WorldAssemblyError；actor 私有会话缺失 → 降级新建 + warning 诊断
- roster 差异诊断：新增 actor（actor_added）、幽灵 stage 占位与 current_actor
  （actor_missing，§5.6 修复），恢复后幽灵不带入舞台
- create_async 并发排他；migrations 高版本文件不静默降级
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
from GensokyoAI.core.migrations import (
    migrate_memory_store_payload,
    migrate_session_file_payload,
)
from GensokyoAI.utils.helpers import build_world_memory_root
from GensokyoAI.world import GensokyoWorld, WorldAssemblyError, WorldPersistence
from GensokyoAI.world.persistence import WorldPersistenceError

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


class _ResumeProvider(BaseProvider):
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


def _write_characters(tmp: str) -> None:
    (Path(tmp) / "marisa.yaml").write_text(_MARISA_YAML, encoding="utf-8")
    (Path(tmp) / "patchouli.yaml").write_text(_PATCHOULI_YAML, encoding="utf-8")


def _make_config(tmp: str, *, actors: list[WorldActorConfig] | None = None) -> AppConfig:
    return AppConfig(
        model=ModelConfig(provider="world_resume_test", name="test-model"),
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
            actors=actors
            or [
                WorldActorConfig(id="marisa", character_file=Path(tmp) / "marisa.yaml"),
                WorldActorConfig(id="patchouli", character_file=Path(tmp) / "patchouli.yaml"),
            ],
            director=WorldDirectorConfig(max_auto_turns=4, max_same_actor_turns=2),
            persistence=WorldPersistenceConfig(save_path=Path(tmp) / "worlds"),
            project_perspective_memories=False,
        ),
    )


class WorldResumeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("world_resume_test", _ResumeProvider)

    async def _create_and_play(self, tmp: str) -> tuple[str, str]:
        """创建 World 并演一段（用户发言 + marisa 回复），返回 (world_session_id, marisa 私有会话 id)。"""
        _ResumeProvider.reset(
            director=[_decision("switch", "marisa"), _decision("wait_user")],
            replies=[[StreamChunk(content="书我借走了DA☆ZE")]],
        )
        world = await GensokyoWorld.create(_make_config(tmp))
        self.addAsyncCleanup(world.shutdown)
        await world.start()
        await world.send_message("把书放下！")
        marisa_session = world._actors["marisa"].session_manager.get_current_session().session_id
        world_session = world.session_id
        assert world_session is not None
        await world.shutdown()
        return world_session, marisa_session

    async def test_actor_sessions_live_under_world_session_root(self):
        """Actor 私有会话统一收进 save_path/world/<world_id>/（不与单角色目录混居）。"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await GensokyoWorld.create(_make_config(tmp))
            self.addAsyncCleanup(world.shutdown)

            expected_root = Path(tmp) / "world" / "testworld"
            expected_memory_names = {"marisa": "雾雨魔理沙", "patchouli": "帕秋莉·诺蕾姬"}
            for actor_id, agent in world._actors.items():
                self.assertEqual(agent.config.session.save_path, expected_root)
                # 语义记忆根不随 session 根嵌套：仍由 build_world_memory_root 决定
                self.assertEqual(
                    agent.runtime_context.semantic_memory_root,
                    build_world_memory_root(
                        Path(tmp), "testworld", expected_memory_names[actor_id]
                    ),
                )
            # 会话文件实际落盘位置：world/<world_id>/<角色名>/<session>.json
            marisa_session = (
                world._actors["marisa"].session_manager.get_current_session().session_id
            )
            session_file = expected_root / "雾雨魔理沙" / f"{marisa_session}.json"
            self.assertTrue(session_file.exists())
            # 单角色目录不被创建
            self.assertFalse((Path(tmp) / "雾雨魔理沙").exists())

    async def test_resumed_world_start_waits_for_user_without_opening(self):
        """resume 恢复的世界 start() 不再主动开场：剧本零新增，直接等待用户。"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world_session, _ = await self._create_and_play(tmp)

            _ResumeProvider.reset()
            world2 = await GensokyoWorld.resume(_make_config(tmp), world_session)
            self.addAsyncCleanup(world2.shutdown)

            def total_entries() -> int:
                return sum(world2.state_snapshot().transcript_counts.values())

            before = total_entries()
            await world2.start()
            assert total_entries() == before
            assert world2.waiting_for_user is True

    async def test_resume_restores_stage_transcript_and_actor_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world_session, marisa_session = await self._create_and_play(tmp)

            _ResumeProvider.reset()
            world2 = await GensokyoWorld.resume(_make_config(tmp), world_session)
            self.addAsyncCleanup(world2.shutdown)

            # 共享剧本还原（用户台词与 marisa 回复都在）
            contents = [e.content for e in world2.transcript_history("world_default")]
            self.assertIn("把书放下！", contents)
            self.assertTrue(any("DA☆ZE" in c for c in contents))
            # actor 私有会话延续（同一 session id，不是新建）
            self.assertEqual(
                world2._actors["marisa"].session_manager.get_current_session().session_id,
                marisa_session,
            )
            # 舞台/状态还原
            snapshot = world2.state_snapshot()
            self.assertEqual(snapshot.session_id, world_session)
            self.assertIn("marisa", snapshot.stage)
            self.assertTrue(snapshot.waiting_for_user)
            # roster 一致：无诊断
            self.assertEqual(world2.resume_diagnostics, [])
            # 活动存档仍是原 session（继续写而不是新建）
            self.assertEqual(world2.session_id, world_session)

    async def test_resume_missing_session_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            with self.assertRaises(WorldAssemblyError) as ctx:
                await GensokyoWorld.resume(_make_config(tmp), "不存在的会话")
            self.assertEqual(ctx.exception.diagnostics[0].code, "world.session_not_found")

    async def test_resume_missing_actor_session_degrades_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world_session, marisa_session = await self._create_and_play(tmp)

            # 篡改存档：marisa 的私有会话指向不存在的 id
            persistence = WorldPersistence(Path(tmp) / "worlds")
            result = persistence.resume("testworld", world_session)
            assert result is not None
            result.record.actor_sessions["marisa"] = "nonexistent-session"
            persistence.save(result.record)

            world2 = await GensokyoWorld.resume(_make_config(tmp), world_session)
            self.addAsyncCleanup(world2.shutdown)

            codes = {d.code for d in world2.resume_diagnostics}
            self.assertIn("world.persistence.actor_session_missing", codes)
            # 降级为新建会话：不是篡改的 id，也不是原 id
            current = world2._actors["marisa"].session_manager.get_current_session()
            self.assertNotIn(current.session_id, {"nonexistent-session", marisa_session})

    async def test_resume_added_actor_gets_fresh_session_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            # 只用 marisa 创建存档
            actors = [WorldActorConfig(id="marisa", character_file=Path(tmp) / "marisa.yaml")]
            _ResumeProvider.reset(
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                replies=[[StreamChunk(content="嗯")]],
            )
            world = await GensokyoWorld.create(_make_config(tmp, actors=actors))
            self.addAsyncCleanup(world.shutdown)
            await world.start()
            await world.send_message("说点什么吧")
            world_session = world.session_id
            await world.shutdown()

            # 用 marisa+patchouli 的配置恢复：patchouli 是新增 actor
            world2 = await GensokyoWorld.resume(_make_config(tmp), world_session)
            self.addAsyncCleanup(world2.shutdown)

            added = {
                d.actor_id for d in world2.resume_diagnostics if d.code.endswith("actor_added")
            }
            self.assertEqual(added, {"patchouli"})
            self.assertIn("patchouli", world2._actors)
            self.assertIsNotNone(world2._actors["patchouli"].session_manager.get_current_session())

    async def test_ghost_stage_occupant_diagnosed_and_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world_session, _ = await self._create_and_play(tmp)

            # 篡改存档：加入幽灵占位并把 current_actor 指向幽灵
            persistence = WorldPersistence(Path(tmp) / "worlds")
            result = persistence.resume("testworld", world_session)
            assert result is not None
            result.record.stage["ghost"] = "magic_forest"
            result.record.current_actor_id = "ghost"
            persistence.save(result.record)

            world2 = await GensokyoWorld.resume(_make_config(tmp), world_session)
            self.addAsyncCleanup(world2.shutdown)

            missing = {
                d.actor_id
                for d in world2.resume_diagnostics
                if d.code == "world.persistence.actor_missing"
            }
            # stage 键与 current_actor_id 也被诊断覆盖（§5.6 修复）
            self.assertIn("ghost", missing)
            # 幽灵不带入恢复后的舞台与状态
            self.assertNotIn("ghost", world2.state_snapshot().stage)
            self.assertIsNone(world2.state_snapshot().current_actor_id)

    async def test_resume_projection_cursor_skips_old_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world_session, _ = await self._create_and_play(tmp)

            world2 = await GensokyoWorld.resume(_make_config(tmp), world_session)
            self.addAsyncCleanup(world2.shutdown)

            entry_count = len(world2.transcript_history("world_default"))
            self.assertGreater(entry_count, 0)
            # 游标放到末尾：旧剧本不会被重复投影
            self.assertEqual(world2._projection_cursors.get("world_default"), entry_count)


class PersistenceHardeningTests(unittest.IsolatedAsyncioTestCase):
    """§5.6 归档修复：create 排他与版本契约。"""

    async def test_create_async_is_exclusive_under_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = WorldPersistence(Path(tmp))
            results = await asyncio.gather(
                persistence.create_async("w1", session_id="same-session"),
                persistence.create_async("w1", session_id="same-session"),
                return_exceptions=True,
            )
            successes = [r for r in results if not isinstance(r, Exception)]
            failures = [r for r in results if isinstance(r, WorldPersistenceError)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)

    async def test_migrations_keep_newer_version_untouched(self):
        # 更高版本文件不被静默降级重写版本号（§5.6 版本契约统一）
        session_data = {
            "schema_version": 999,
            "format": "gensokyoai.session",
            "session": {"session_id": "s1"},
            "messages": [],
            "future_field": "未来字段",
        }
        migrated, changed = migrate_session_file_payload(session_data)
        self.assertFalse(changed)
        self.assertEqual(migrated["schema_version"], 999)
        self.assertEqual(migrated["future_field"], "未来字段")

        memory_data = {
            "schema_version": 999,
            "format": "gensokyoai.memory.store",
            "topics": [],
            "memories": [],
        }
        migrated, changed = migrate_memory_store_payload(memory_data)
        self.assertFalse(changed)
        self.assertEqual(migrated["schema_version"], 999)


if __name__ == "__main__":
    unittest.main()
