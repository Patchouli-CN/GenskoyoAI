"""DialogueLoop 抽象与 World 主动循环（阶段 7）定向测试。

覆盖：
- InitiativeScheduler 纯调度器：到点触发、取代、取消、空计划即取消、fire 时效
- World 主动循环：段落结束统一规划（全世界一个定时器）、用户发言取消并重规划、
  到点后 Director initiative 基于当下在场角色选角开口、initiative_timer 禁用时不装循环
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
    SceneConfig,
    SessionConfig,
    ThinkEngineConfig,
    WorldActorConfig,
    WorldConfig,
    WorldDirectorConfig,
    WorldPersistenceConfig,
)
from GensokyoAI.core.dialogue_loop import InitiativePlan
from GensokyoAI.core.events import EventBus, SystemEvent
from GensokyoAI.core.initiative_scheduler import InitiativeScheduler
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


def _plan(summary: str, *, should=True, delay=0, enthusiasm=0.8) -> str:
    return json.dumps(
        {
            "should_schedule": should,
            "delay_seconds": delay,
            "summary": summary,
            "reason": "节奏需要",
            "enthusiasm": enthusiasm,
        },
        ensure_ascii=False,
    )


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    """InitiativeScheduler 纯调度器单元测试。"""

    def _scheduler(self, fired: list, **kwargs) -> InitiativeScheduler:
        async def _trigger(plan: InitiativePlan, fire_id: str):
            fired.append((plan, fire_id))

        kwargs.setdefault("min_delay_seconds", 0)
        kwargs.setdefault("trigger_callback", _trigger)
        return InitiativeScheduler(**kwargs)

    async def test_schedule_fires_after_delay(self):
        fired: list = []
        scheduler = self._scheduler(fired)
        payload = await scheduler.schedule(
            InitiativePlan(should_schedule=True, delay_seconds=0, summary="想说点事")
        )
        self.assertIsNotNone(payload)
        await asyncio.sleep(0.2)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0].summary, "想说点事")
        self.assertIsNone(scheduler.current())

    async def test_new_plan_replaces_old(self):
        fired: list = []
        scheduler = self._scheduler(fired)
        await scheduler.schedule(
            InitiativePlan(should_schedule=True, delay_seconds=60, summary="旧计划")
        )
        await scheduler.schedule(
            InitiativePlan(should_schedule=True, delay_seconds=0, summary="新计划")
        )
        await asyncio.sleep(0.2)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0].summary, "新计划")

    async def test_cancel_prevents_fire(self):
        fired: list = []
        scheduler = self._scheduler(fired)
        await scheduler.schedule(
            InitiativePlan(should_schedule=True, delay_seconds=60, summary="被取消")
        )
        cancelled = await scheduler.cancel(reason="user_speaks")
        self.assertTrue(cancelled)
        self.assertIsNone(scheduler.current())
        await asyncio.sleep(0.1)
        self.assertEqual(fired, [])

    async def test_empty_plan_means_cancel(self):
        fired: list = []
        scheduler = self._scheduler(fired)
        await scheduler.schedule(
            InitiativePlan(should_schedule=True, delay_seconds=60, summary="有计划")
        )
        result = await scheduler.schedule(InitiativePlan(should_schedule=False))
        self.assertIsNone(result)
        self.assertIsNone(scheduler.current())

    async def test_fire_validity_check(self):
        scheduler = self._scheduler([])
        payload = await scheduler.schedule(
            InitiativePlan(should_schedule=True, delay_seconds=0, summary="触发一次")
        )
        fire_id = payload["timer_id"]
        await asyncio.sleep(0.2)
        # 触发完成后 fire id 已失效
        self.assertFalse(scheduler.is_active_fire(fire_id))

    async def test_events_published(self):
        bus = EventBus(enable_trace=False)
        await bus.start()
        seen: list[SystemEvent] = []

        async def _capture(event):
            seen.append(event.type)

        for event_type in (
            SystemEvent.INITIATIVE_TIMER_CREATED,
            SystemEvent.INITIATIVE_TIMER_TRIGGERED,
        ):
            bus.subscribe(event_type, _capture)
        scheduler = self._scheduler([], event_bus=bus)
        await scheduler.schedule(
            InitiativePlan(should_schedule=True, delay_seconds=0, summary="事件")
        )
        await asyncio.sleep(0.2)
        await bus.stop()
        self.assertIn(SystemEvent.INITIATIVE_TIMER_CREATED, seen)
        self.assertIn(SystemEvent.INITIATIVE_TIMER_TRIGGERED, seen)


class _InitiativeProvider(BaseProvider):
    """三路假 Provider：节奏导演=世界规划、多角色舞台的导演=Director、chat_stream=演员。"""

    director_script: list[str] = []
    plan_script: list[str] = []
    reply_script: list[list[StreamChunk]] = []
    chat_calls: list[list[dict]] = []
    stream_calls: list[list[dict]] = []

    @classmethod
    def reset(cls, director=(), plans=(), replies=()) -> None:
        cls.director_script = list(director)
        cls.plan_script = list(plans)
        cls.reply_script = list(replies)
        cls.chat_calls = []
        cls.stream_calls = []

    @classmethod
    def plan_calls(cls) -> list[list[dict]]:
        return [
            call
            for call in cls.chat_calls
            if call and "节奏导演" in str(call[0].get("content", ""))
        ]

    async def chat(self, model, messages, tools=None, options=None, **kwargs):
        cls = type(self)
        cls.chat_calls.append(list(messages))
        system_text = str(messages[0].get("content", "")) if messages else ""
        if "节奏导演" in system_text:
            content = cls.plan_script.pop(0) if cls.plan_script else _plan("", should=False)
        else:
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


def _write_characters(tmp: str) -> None:
    (Path(tmp) / "marisa.yaml").write_text(_MARISA_YAML, encoding="utf-8")
    (Path(tmp) / "patchouli.yaml").write_text(_PATCHOULI_YAML, encoding="utf-8")


def _make_config(tmp: str, *, initiative_enabled: bool) -> AppConfig:
    return AppConfig(
        model=ModelConfig(provider="world_initiative_test", name="test-model"),
        session=SessionConfig(save_path=Path(tmp)),
        memory=MemoryConfig(semantic_enabled=False, auto_memory_enabled=False),
        think_engine=ThinkEngineConfig(enabled=False),
        initiative_timer=InitiativeTimerConfig(
            enabled=initiative_enabled, min_delay_seconds=0, max_delay_seconds=600
        ),
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
            project_perspective_memories=False,
        ),
    )


class WorldInitiativeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("world_initiative_test", _InitiativeProvider)

    async def _boot(self, tmp: str, *, director=(), plans=(), replies=(), enabled=True):
        _InitiativeProvider.reset(director, plans, replies)
        world = await GensokyoWorld.create(_make_config(tmp, initiative_enabled=enabled))
        self.addAsyncCleanup(world.shutdown)
        await world.start()
        return world

    async def test_segment_end_schedules_world_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(
                tmp,
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                plans=[_plan("魔理沙似乎还惦记着她的书", delay=300)],
                replies=[[StreamChunk(content="书我借走了DA☆ZE")]],
            )

            await world.send_message("把书放下！")

            current = world._initiative_loop.current_plan()
            self.assertIsNotNone(current, "段落结束后应产生一个世界主动计划")
            self.assertEqual(current["summary"], "魔理沙似乎还惦记着她的书")
            # 规划 prompt 是世界级的（节奏导演），并带场景与在场信息
            self.assertEqual(len(_InitiativeProvider.plan_calls()), 1)

    async def test_no_loop_when_initiative_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(
                tmp,
                enabled=False,
                director=[_decision("switch", "marisa"), _decision("wait_user")],
                replies=[[StreamChunk(content="嗯")]],
            )

            await world.send_message("说点什么吧")

            self.assertIsNone(world._initiative_loop)
            self.assertEqual(_InitiativeProvider.plan_calls(), [])

    async def test_user_message_cancels_and_replans(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(
                tmp,
                director=[
                    _decision("switch", "marisa"),
                    _decision("wait_user"),
                    _decision("switch", "marisa"),
                    _decision("wait_user"),
                ],
                plans=[_plan("旧意图", delay=300), _plan("新意图", delay=300)],
                replies=[
                    [StreamChunk(content="第一段")],
                    [StreamChunk(content="第二段")],
                ],
            )

            await world.send_message("第一句话")
            first_plan = world._initiative_loop.current_plan()
            self.assertEqual(first_plan["summary"], "旧意图")

            await world.send_message("第二句话打断了沉默")
            # 用户发言取消了旧计划，段落结束后重新规划
            second_plan = world._initiative_loop.current_plan()
            self.assertEqual(second_plan["summary"], "新意图")
            self.assertNotEqual(first_plan["timer_id"], second_plan["timer_id"])

    async def test_timer_expiry_runs_director_initiative(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_characters(tmp)
            world = await self._boot(
                tmp,
                director=[
                    _decision("switch", "marisa"),
                    _decision("wait_user"),
                    # 主动触发后：Director initiative 让当前演员继续（switch 到自己是
                    # 非法决策——阶段 4 的校验语义），然后收场
                    _decision("continue"),
                    _decision("wait_user"),
                ],
                plans=[_plan("她想起了没说完的话", delay=0), _plan("", should=False)],
                replies=[
                    [StreamChunk(content="第一段")],
                    [StreamChunk(content="对了还有一件事DA☆ZE")],
                ],
            )

            await world.send_message("先说句话")
            # delay=0 的计划立即到点：等触发链跑完（回合锁释放后执行主动段）
            await asyncio.sleep(1.0)

            # 主动消息进了共享剧本（phase=initiative 选角，不是轮流）
            contents = [e.content for e in world.transcript_history("world_default")]
            self.assertTrue(any("还有一件事" in c for c in contents), contents)
            # Director 的 initiative 决策收到了世界意图摘要
            initiative_prompts = [
                str(call[-1].get("content", ""))
                for call in _InitiativeProvider.chat_calls
                if call and "多角色舞台的导演" in str(call[0].get("content", ""))
            ]
            self.assertTrue(any("她想起了没说完的话" in p for p in initiative_prompts))
            self.assertTrue(any("沉默" in p for p in initiative_prompts))


if __name__ == "__main__":
    unittest.main()
