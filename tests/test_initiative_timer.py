import asyncio
import unittest

from GensokyoAI.core.agent.initiative_timer import InitiativeTimerManager
from GensokyoAI.core.agent.think_engine import ThinkEngine
from GensokyoAI.core.agent.types import UnifiedMessage, UnifiedResponse
from GensokyoAI.core.config import InitiativeTimerConfig, ThinkEngineConfig
from GensokyoAI.core.events import EventBus, SystemEvent
from GensokyoAI.memory.working import WorkingMemoryManager


class _FakeModelClient:
    def __init__(self, content: str | None = None, *, structured_output: bool = False):
        self.contents = [
            content
            if content is not None
            else '{"should_schedule": true, "delay_seconds": 60, "summary": "稍后补充刚才话题的一个想法", "reason": "想补充"}'
        ]
        self.structured_output = structured_output
        self.last_options = None
        self.last_messages = None
        self.call_count = 0

    def supports(self, capability: str) -> bool:
        return self.structured_output and capability == "structured_output"

    async def chat(self, messages, options=None):
        self.last_messages = messages
        self.last_options = options
        index = min(self.call_count, len(self.contents) - 1)
        self.call_count += 1
        return UnifiedResponse(
            message=UnifiedMessage(
                role="assistant",
                content=self.contents[index],
            )
        )


class _FakeSemanticMemory:
    """只暴露 ThinkEngine 需要的 store 接口。"""

    def __init__(self):
        self.store = _FakeTopicStore()


class _FakeTopicStore:
    def __init__(self):
        self._topics = {}

    def get_all_topics(self):
        return list(self._topics.values())


def _make_think_engine(
    model_client: _FakeModelClient,
    event_bus: EventBus,
    character_name: str = "测试角色",
) -> ThinkEngine:
    """构造一个用于 initiative_timer 测试的 ThinkEngine。"""
    return ThinkEngine(
        semantic_memory=_FakeSemanticMemory(),
        model_client=model_client,
        event_bus=event_bus,
        character_name=character_name,
        config=ThinkEngineConfig(),
    )


class InitiativeTimerManagerTests(unittest.TestCase):
    def test_schedule_update_trigger_and_discard_flow(self):
        async def run():
            event_bus = EventBus(enable_trace=False)
            events = []
            event_bus.subscribe(
                SystemEvent.INITIATIVE_TIMER_CREATED, lambda event: events.append(event)
            )
            event_bus.subscribe(
                SystemEvent.INITIATIVE_TIMER_UPDATED, lambda event: events.append(event)
            )
            event_bus.subscribe(
                SystemEvent.INITIATIVE_TIMER_TRIGGERED, lambda event: events.append(event)
            )
            await event_bus.start()
            try:
                model_client = _FakeModelClient()
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(
                        min_delay_seconds=10,
                        max_delay_seconds=120,
                        max_pending_summary_chars=50,
                    ),
                    think_engine=_make_think_engine(model_client, event_bus),
                    event_bus=event_bus,
                    character_name="测试角色",
                )

                payload = await manager.schedule_intent(
                    summary="稍后补充刚才话题的一个想法", delay_seconds=60
                )
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["status"], "scheduled")
                self.assertEqual(payload["delay_seconds"], 60)
                self.assertEqual(payload["pending_summary"], "稍后补充刚才话题的一个想法")

                updated = await manager.update(
                    timer_id=payload["timer_id"],
                    delay_seconds=30,
                    pending_summary="用户编辑后的积存摘要",
                )
                self.assertTrue(updated["user_modified"])
                self.assertEqual(updated["delay_seconds"], 30)
                self.assertEqual(updated["pending_summary"], "用户编辑后的积存摘要")
                self.assertGreater(updated["generation"], payload["generation"])

                triggered = await manager.trigger(timer_id=payload["timer_id"])
                self.assertTrue(triggered["triggered"])
                self.assertEqual(triggered["pending_summary"], "用户编辑后的积存摘要")
                self.assertIsNone(manager.current_payload())

                await asyncio.sleep(0.05)
                event_types = [event.type for event in events]
                self.assertIn(SystemEvent.INITIATIVE_TIMER_CREATED, event_types)
                self.assertIn(SystemEvent.INITIATIVE_TIMER_UPDATED, event_types)
                self.assertIn(SystemEvent.INITIATIVE_TIMER_TRIGGERED, event_types)
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_discard_invalidates_current_payload(self):
        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(),
                    think_engine=_make_think_engine(_FakeModelClient(), event_bus),
                    event_bus=event_bus,
                    character_name="测试角色",
                )
                payload = await manager.schedule_intent(
                    summary="稍后补充刚才话题的一个想法", delay_seconds=60
                )
                self.assertIsNotNone(payload)
                discarded = await manager.discard(reason="user_message_received", source="user")
                self.assertIsNotNone(discarded)
                assert discarded is not None
                self.assertEqual(discarded["status"], "discarded")
                self.assertIsNone(manager.current_payload())
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_consecutive_initiative_limit_blocks_new_timer(self):
        """达到最大连续主动次数后，schedule_after_response 应返回 None。"""

        async def run():
            event_bus = EventBus(enable_trace=False)
            events = []
            event_bus.subscribe(
                SystemEvent.INITIATIVE_TIMER_CREATED, lambda event: events.append(event)
            )
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(max_initiative_times=2),
                    think_engine=_make_think_engine(_FakeModelClient(), event_bus),
                    event_bus=event_bus,
                    character_name="测试角色",
                )
                manager._consecutive_initiative_count = 2

                payload = await manager.schedule_intent(
                    summary="稍后补充刚才话题的一个想法", delay_seconds=60
                )
                self.assertIsNone(payload)
                self.assertEqual(len(events), 0)
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_consecutive_initiative_counter_increments_and_resets(self):
        """计数器递增和重置逻辑正确。"""

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(max_initiative_times=3),
                    think_engine=_make_think_engine(_FakeModelClient(), event_bus),
                    event_bus=event_bus,
                    character_name="测试角色",
                )
                self.assertEqual(manager._consecutive_initiative_count, 0)
                self.assertFalse(manager._has_reached_initiative_limit())

                manager.increment_consecutive_initiative_count()
                self.assertEqual(manager._consecutive_initiative_count, 1)
                self.assertFalse(manager._has_reached_initiative_limit())

                manager._consecutive_initiative_count = 3
                self.assertTrue(manager._has_reached_initiative_limit())

                manager.reset_consecutive_initiative_count()
                self.assertEqual(manager._consecutive_initiative_count, 0)
                self.assertFalse(manager._has_reached_initiative_limit())
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_user_discard_resets_consecutive_initiative_counter(self):
        """用户来源的 discard 会重置连续主动计数器。"""

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(),
                    think_engine=_make_think_engine(_FakeModelClient(), event_bus),
                    event_bus=event_bus,
                    character_name="测试角色",
                )
                payload = await manager.schedule_intent(
                    summary="稍后补充刚才话题的一个想法", delay_seconds=60
                )
                self.assertIsNotNone(payload)

                manager.increment_consecutive_initiative_count()
                manager.increment_consecutive_initiative_count()
                self.assertEqual(manager._consecutive_initiative_count, 2)

                # 非用户来源不应重置
                await manager.discard(reason="system_cleanup", source="system")
                self.assertEqual(manager._consecutive_initiative_count, 2)

                # 用户回复应重置
                await manager.schedule_intent(summary="新的想法", delay_seconds=60)
                manager.increment_consecutive_initiative_count()
                await manager.discard(reason="user_message_received", source="user")
                self.assertEqual(manager._consecutive_initiative_count, 0)
            finally:
                await event_bus.stop()

        asyncio.run(run())


class InitiativeCoordinatorDriveTests(unittest.TestCase):
    """对话欲调度链（coordinator）：ThinkEngine 阈值判断 → schedule_intent / 沉默。"""

    def _make_agent(self, event_bus: EventBus, decision):
        from types import SimpleNamespace


        class _FakeThinkEngine:
            def __init__(self, drive_decision):
                self._decision = drive_decision
                self.calls = []

            async def evaluate_speaking_drive(self, trigger_text, recent, **kwargs):
                self.calls.append((trigger_text, kwargs))
                return self._decision

        return SimpleNamespace(
            _think_engine=_FakeThinkEngine(decision),
            working_memory=WorkingMemoryManager(max_turns=10),
            config=SimpleNamespace(
                initiative_timer=InitiativeTimerConfig(),
                debug_silent_output=False,
            ),
            event_bus=event_bus,
            character_name="测试角色",
        )

    def test_want_speak_schedules_timer_via_schedule_intent(self):
        from GensokyoAI.core.agent.initiative_coordinator import InitiativeCoordinator
        from GensokyoAI.core.agent.think_engine import SpeakingDriveDecision

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                decision = SpeakingDriveDecision(
                    want_speak=True,
                    total_drive=0.81,
                    message="赛钱箱该擦擦了",
                    delay_seconds=120,
                    enthusiasm=0.0,
                    reason="有想法",
                )
                coordinator = InitiativeCoordinator(self._make_agent(event_bus, decision))
                payload = await coordinator.schedule("刚才的回复")
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["status"], "scheduled")
                self.assertEqual(payload["source"], "drive")
                self.assertEqual(payload["pending_summary"], "赛钱箱该擦擦了")
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_below_threshold_stays_silent(self):
        from GensokyoAI.core.agent.initiative_coordinator import InitiativeCoordinator
        from GensokyoAI.core.agent.think_engine import SpeakingDriveDecision

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                decision = SpeakingDriveDecision(
                    want_speak=False,
                    total_drive=0.12,
                    message="没什么想说的",
                    reason="平淡",
                )
                coordinator = InitiativeCoordinator(self._make_agent(event_bus, decision))
                payload = await coordinator.schedule("刚才的回复")
                self.assertIsNone(payload)
                self.assertIsNone(coordinator.current())
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_evaluation_failure_stays_silent(self):
        from GensokyoAI.core.agent.initiative_coordinator import InitiativeCoordinator

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                coordinator = InitiativeCoordinator(self._make_agent(event_bus, None))
                payload = await coordinator.schedule("刚才的回复")
                self.assertIsNone(payload)
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_generate_aborts_when_consecutive_limit_reached(self):
        """连续主动上限在生成管线统一把关：思考冲动路径（不过 schedule_intent）也被拦。"""
        from GensokyoAI.core.agent.initiative_coordinator import InitiativeCoordinator

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                coordinator = InitiativeCoordinator(self._make_agent(event_bus, None))
                manager = coordinator._ensure_manager()
                manager._consecutive_initiative_count = (
                    manager.config.max_initiative_times  # 达到上限
                )
                result = await coordinator.generate_initiative_message(
                    timer_id="thought-test", pending_summary="还想说点啥"
                )
                self.assertIsNotNone(result)
                assert result is not None
                self.assertFalse(result["sent"])
                self.assertTrue(result["limited"])
                # 用户回复后计数重置，主动发言恢复
                manager.reset_consecutive_initiative_count()
                self.assertFalse(manager._has_reached_initiative_limit())
            finally:
                await event_bus.stop()

        asyncio.run(run())


class InitiativeCoordinatorEnabledTests(unittest.TestCase):
    """enabled 运行时开关：无主动消息投递通道的接入方（如 QQ Bot）彻底停用主动发言。"""

    def _make_drive_agent(self, event_bus: EventBus, decision):
        from types import SimpleNamespace

        class _FakeThinkEngine:
            def __init__(self, drive_decision):
                self._decision = drive_decision

            async def evaluate_speaking_drive(self, trigger_text, recent, **kwargs):
                return self._decision

        return SimpleNamespace(
            _think_engine=_FakeThinkEngine(decision),
            working_memory=WorkingMemoryManager(max_turns=10),
            config=SimpleNamespace(
                initiative_timer=InitiativeTimerConfig(),
                debug_silent_output=False,
            ),
            event_bus=event_bus,
            character_name="测试角色",
        )

    def test_update_enabled_toggles_runtime_switch_and_blocks_schedule(self):
        from types import SimpleNamespace

        from GensokyoAI.core.agent.initiative_coordinator import InitiativeCoordinator

        async def run():
            agent = SimpleNamespace(
                config=SimpleNamespace(initiative_timer=InitiativeTimerConfig())
            )
            coordinator = InitiativeCoordinator(agent)

            result = await coordinator.update(enabled=False)
            self.assertFalse(result["enabled"])
            self.assertIsNone(result["timer"])
            self.assertFalse(agent.config.initiative_timer.enabled)
            # 关闭后 schedule() 在 config.enabled 检查处直接短路，不触及 ThinkEngine
            self.assertIsNone(await coordinator.schedule("任意回复"))

            result = await coordinator.update(enabled=True)
            self.assertTrue(result["enabled"])
            self.assertTrue(agent.config.initiative_timer.enabled)

        asyncio.run(run())

    def test_disabling_discards_pending_timer(self):
        from GensokyoAI.core.agent.initiative_coordinator import InitiativeCoordinator
        from GensokyoAI.core.agent.think_engine import SpeakingDriveDecision

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                decision = SpeakingDriveDecision(
                    want_speak=True,
                    total_drive=0.9,
                    message="待表达的意图",
                    delay_seconds=120,
                    enthusiasm=0.0,
                    reason="测试",
                )
                coordinator = InitiativeCoordinator(self._make_drive_agent(event_bus, decision))
                payload = await coordinator.schedule("刚才的回复")
                self.assertIsNotNone(payload)

                result = await coordinator.update(enabled=False)
                self.assertFalse(result["enabled"])
                self.assertIsNone(coordinator.current())  # 待发计划已废止
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_disabled_config_blocks_schedule_intent_without_model_call(self):
        """manager 层同样短路：enabled=False 时不创建定时器、不调用模型。"""

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                model_client = _FakeModelClient()
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(enabled=False),
                    think_engine=_make_think_engine(model_client, event_bus),
                    event_bus=event_bus,
                    character_name="测试角色",
                )
                payload = await manager.schedule_intent(summary="测试", delay_seconds=60)
                self.assertIsNone(payload)
                self.assertEqual(model_client.call_count, 0)
            finally:
                await event_bus.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
