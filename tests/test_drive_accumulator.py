"""对话欲累积（§7.3）定向测试。

覆盖：
- DriveAccumulator 纯算术：基础增量、四维动机回灌、情感尖峰、场景匹配、
  沉默低权重累积、心情非对称衰减（正快负慢）、泄压、持久化往返
- 短期思考接入四维动机：一次 LLM 输出四维动机 + 调度决策；决策上下文携带
  对话欲/心情状态；动机回灌累积器；AI 说不就无定时器（即使 fallback 显式
  开启，对话欲路径也绝不强制）；会话 metadata 持久化
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from GensokyoAI.core.agent import Agent
from GensokyoAI.core.agent.drive_accumulator import DriveAccumulator
from GensokyoAI.core.agent.motivation_evaluator import MotivationProfile
from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import StreamChunk, UnifiedMessage, UnifiedResponse
from GensokyoAI.core.config import (
    AppConfig,
    CharacterConfig,
    InitiativeTimerConfig,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
    ThinkEngineConfig,
)

# ==================== 纯算术单元测试 ====================


def _config(**overrides) -> InitiativeTimerConfig:
    return InitiativeTimerConfig(**overrides)


class DriveAccumulatorTests(unittest.TestCase):
    def test_turn_increment_accumulates(self):
        acc = DriveAccumulator(_config())
        first = acc.record_turn()
        self.assertAlmostEqual(first, 0.12, places=3)
        second = acc.record_turn()
        self.assertAlmostEqual(second, 0.24, places=3)

    def test_motivation_boost_uses_total_drive(self):
        acc = DriveAccumulator(_config())
        profile = MotivationProfile(
            expression_drive=0.8,
            emotional_charge=0.6,
            relational_need=0.4,
            situational_relevance=0.2,
        )
        # total_drive = 0.8*0.3 + 0.6*0.35 + 0.4*0.2 + 0.2*0.15 = 0.56
        drive = acc.record_turn(motivation=profile)
        self.assertAlmostEqual(drive, 0.12 + 0.2 * 0.56, places=3)

    def test_emotion_boost_only_above_half(self):
        acc = DriveAccumulator(_config())
        low = acc.record_turn(emotional_valence=0.4)
        self.assertAlmostEqual(low, 0.12, places=3)  # 未达 0.5 不计尖峰
        acc2 = DriveAccumulator(_config())
        high = acc2.record_turn(emotional_valence=-0.8)
        self.assertAlmostEqual(high, 0.12 + 0.25 * 0.8, places=3)  # 绝对值计

    def test_scene_match_boost(self):
        acc = DriveAccumulator(_config())
        self.assertAlmostEqual(acc.record_turn(scene_match=True), 0.12 + 0.1, places=3)

    def test_silence_accumulates_slowly(self):
        config = _config(drive_silence_rate_per_minute=0.01)
        acc = DriveAccumulator.from_dict(
            config, {"drive": 0.1, "mood": 0.0, "last_update": time.time() - 600}
        )
        # 沉默 10 分钟 × 0.01/分 = +0.1（低权重）
        self.assertAlmostEqual(acc.current_drive(), 0.2, delta=0.01)

    def test_mood_decays_asymmetrically(self):
        config = _config(
            mood_half_life_positive_minutes=10.0,
            mood_half_life_negative_minutes=40.0,
        )
        ten_minutes_ago = time.time() - 600
        positive = DriveAccumulator.from_dict(
            config, {"drive": 0.0, "mood": 0.8, "last_update": ten_minutes_ago}
        )
        positive.current_drive()
        # 正面心情 10 分钟（一个半衰期）后减半
        self.assertAlmostEqual(positive.mood, 0.4, delta=0.01)

        negative = DriveAccumulator.from_dict(
            config, {"drive": 0.0, "mood": -0.8, "last_update": ten_minutes_ago}
        )
        negative.current_drive()
        # 负面心情半衰期 40 分钟：10 分钟后只衰减到约 -0.67（衰减慢但仍衰减）
        self.assertAlmostEqual(negative.mood, -0.8 * (0.5**0.25), delta=0.01)

    def test_vent_releases_drive(self):
        acc = DriveAccumulator(_config(drive_vent_factor=0.4))
        acc.record_turn()
        acc.record_turn()
        before = acc.drive
        acc.vent()
        self.assertAlmostEqual(acc.drive, before * 0.4, places=3)

    def test_persistence_roundtrip_and_corrupt_fallback(self):
        acc = DriveAccumulator(_config())
        acc.record_turn(emotional_valence=0.9)
        data = acc.to_dict()
        restored = DriveAccumulator.from_dict(_config(), data)
        self.assertAlmostEqual(restored.drive, acc.drive, places=3)
        self.assertAlmostEqual(restored.mood, acc.mood, places=3)

        # 损坏数据从全新状态开始，不炸
        corrupt = DriveAccumulator.from_dict(_config(), {"drive": "垃圾", "mood": None})
        self.assertEqual(corrupt.drive, 0.0)
        self.assertEqual(corrupt.mood, 0.0)


# ==================== 集成测试（Agent + 短期思考四维动机） ====================


def _drive_decision(
    *, should=True, summary="想接着说刚才的魔法实验", delay=120, motivation=None
) -> str:
    return json.dumps(
        {
            "should_schedule": should,
            "delay_seconds": delay,
            "summary": summary if should else "",
            "reason": "测试决策",
            "enthusiasm": 0.5,
            "motivation": motivation
            or {
                "expression_drive": 0.8,
                "emotional_charge": 0.6,
                "relational_need": 0.4,
                "situational_relevance": 0.2,
            },
        },
        ensure_ascii=False,
    )


class _DriveProvider(BaseProvider):
    """chat() 出对话欲决策 JSON（记录调用），chat_stream() 出回复。"""

    decision_script: list[str] = []
    reply_script: list[list[StreamChunk]] = []
    chat_calls: list[list[dict]] = []
    chat_options: list[dict] = []

    @classmethod
    def reset(cls, decisions=(), replies=()) -> None:
        cls.decision_script = list(decisions)
        cls.reply_script = list(replies)
        cls.chat_calls = []
        cls.chat_options = []

    async def chat(self, model, messages, tools=None, options=None, **kwargs):
        type(self).chat_calls.append(list(messages))
        type(self).chat_options.append(dict(options or {}))
        content = (
            type(self).decision_script.pop(0)
            if type(self).decision_script
            else _drive_decision(should=False)
        )
        return UnifiedResponse(
            model=model, message=UnifiedMessage(role="assistant", content=content)
        )

    async def chat_stream(self, model, messages, tools=None, options=None, **kwargs):
        chunks = (
            type(self).reply_script.pop(0)
            if type(self).reply_script
            else [StreamChunk(content="嗯")]
        )
        for chunk in chunks:
            yield chunk


def _make_config(tmp: str, *, drive_enabled: bool) -> AppConfig:
    return AppConfig(
        character=CharacterConfig(name="Marisa", system_prompt="你是魔理沙。"),
        model=ModelConfig(provider="drive_test", name="test-model"),
        session=SessionConfig(save_path=Path(tmp)),
        memory=MemoryConfig(semantic_enabled=False, auto_memory_enabled=False),
        think_engine=ThinkEngineConfig(enabled=False),
        initiative_timer=InitiativeTimerConfig(
            enabled=True,
            drive_enabled=drive_enabled,
        ),
    )


class DriveSchedulingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("drive_test", _DriveProvider)

    async def _boot(self, tmp: str, *, decisions=(), replies=(), drive_enabled=True) -> Agent:
        _DriveProvider.reset(decisions, replies)
        with patch("GensokyoAI.core.agent.lifecycle.LifecycleManager.setup_signal_handlers"):
            agent = Agent(config=_make_config(tmp, drive_enabled=drive_enabled))
        agent.create_session()
        await agent.start()
        self.addAsyncCleanup(agent.shutdown)
        return agent

    @staticmethod
    async def _drain() -> None:
        await asyncio.sleep(0.3)

    async def test_drive_decision_schedules_with_motivation_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(
                tmp,
                decisions=[_drive_decision(should=True)],
                replies=[[StreamChunk(content="火力就是正义DA☆ZE")]],
            )

            await agent.send("教我个魔法吧")
            await self._drain()

            # 定时器已按决策调度（source=drive，摘要是意图不是话术）
            timer = agent.current_initiative_timer()
            self.assertIsNotNone(timer, "should_schedule=true 应创建对话欲定时器")
            self.assertEqual(timer["source"], "drive")
            self.assertEqual(timer["pending_summary"], "想接着说刚才的魔法实验")

            # 决策上下文携带对话欲/心情状态（短期思考接入对话欲模型）
            system_prompt = str(_DriveProvider.chat_calls[0][0].get("content", ""))
            self.assertIn("对话欲强度", system_prompt)
            self.assertIn("心情效价", system_prompt)

            # 四维动机回灌：0.12 + 0.2*0.56 ≈ 0.232
            status = agent._initiative_coordinator.drive_status()
            self.assertAlmostEqual(status["drive"], 0.232, delta=0.02)

            # 对话欲状态写入会话 metadata（持久化，重启不重置人格）
            session = agent.session_manager.get_current_session()
            self.assertIn("initiative_drive", session.metadata)
            self.assertGreater(session.metadata["initiative_drive"]["drive"], 0)

    async def test_no_schedule_decision_is_respected_without_forcing(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(
                tmp,
                decisions=[_drive_decision(should=False)],
                replies=[[StreamChunk(content="哦")]],
            )

            await agent.send("嗯，这样啊")
            await self._drain()

            # AI 决定不说：无定时器——强制 fallback 链已删除，系统尊重角色意愿
            self.assertIsNone(agent.current_initiative_timer())
            # 只有一次决策调用，没有第二次强制调度调用
            self.assertEqual(len(_DriveProvider.chat_calls), 1)

    async def test_parse_failure_retries_once_then_gives_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(
                tmp,
                decisions=["这不是 JSON", "依然不是"],
                replies=[[StreamChunk(content="哦")]],
            )

            await agent.send("嗯，知道了")
            await self._drain()

            self.assertIsNone(agent.current_initiative_timer())
            self.assertEqual(len(_DriveProvider.chat_calls), 2)  # 重试一次后放弃

    async def test_legacy_path_unchanged_without_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = await self._boot(
                tmp,
                drive_enabled=False,
                decisions=[_drive_decision(should=True)],
                replies=[[StreamChunk(content="嗯")]],
            )

            await agent.send("说点什么吧")
            await self._drain()

            # 旧路径决策不注入对话欲状态，也不要求四维动机
            system_prompt = str(_DriveProvider.chat_calls[0][0].get("content", ""))
            self.assertNotIn("对话欲强度", system_prompt)
            self.assertNotIn("expression_drive", system_prompt)
            timer = agent.current_initiative_timer()
            self.assertIsNotNone(timer)
            self.assertEqual(timer["source"], "ai")  # 旧路径 source 不变


if __name__ == "__main__":
    unittest.main()
