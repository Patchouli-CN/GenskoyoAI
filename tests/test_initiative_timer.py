import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("刚才的回复")
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
                    working_memory=WorkingMemoryManager(max_turns=10),
                )
                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNotNone(payload)
                discarded = await manager.discard(reason="user_message_received", source="user")
                self.assertIsNotNone(discarded)
                assert discarded is not None
                self.assertEqual(discarded["status"], "discarded")
                self.assertIsNone(manager.current_payload())
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_invalid_decision_json_gives_up_without_forcing(self):
        """解析失败且未开启犹豫：尊重失败，不创建任何定时器（强制 fallback 已删除）。"""

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(),
                    think_engine=_make_think_engine(
                        _FakeModelClient(
                            '{"should_schedule": true, "delay_seconds": 60, "summary": "截断的摘要'
                        ),
                        event_bus,
                    ),
                    event_bus=event_bus,
                    character_name="测试角色",
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNone(payload)
                self.assertIsNone(manager.current_payload())
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_invalid_decision_json_triggers_hesitation_when_enabled(self):
        """显式开启犹豫时，解析失败的 JSON 进入犹豫链。"""

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(hesitation_enabled=True),
                    think_engine=_make_think_engine(
                        _FakeModelClient(
                            '{"should_schedule": true, "delay_seconds": 60, "summary": "截断的摘要'
                        ),
                        event_bus,
                    ),
                    event_bus=event_bus,
                    character_name="测试角色",
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["source"], "reconsider")
                self.assertTrue(payload["hesitation_enabled"])
                self.assertEqual(payload["hesitation_round"], 1)
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_no_schedule_decision_is_respected_without_forcing(self):
        """AI 决定不发言：不创建任何定时器（不再违背意愿强行安排）。"""

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(),
                    think_engine=_make_think_engine(
                        _FakeModelClient(
                            '{"should_schedule": false, "delay_seconds": 60, "summary": "", "reason": "暂时没话说"}'
                        ),
                        event_bus,
                    ),
                    event_bus=event_bus,
                    character_name="测试角色",
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNone(payload)
                self.assertIsNone(manager.current_payload())
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_empty_summary_decision_gives_up_without_forcing(self):
        """决定设置定时器但摘要为空：视为无有效意图，不创建定时器。"""

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(),
                    think_engine=_make_think_engine(
                        _FakeModelClient(
                            '{"should_schedule": true, "delay_seconds": 60, "summary": "", "reason": "漏写摘要"}'
                        ),
                        event_bus,
                    ),
                    event_bus=event_bus,
                    character_name="测试角色",
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNone(payload)
                self.assertIsNone(manager.current_payload())
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_hesitation_exhaustion_gives_up_without_forcing(self):
        """犹豫轮次耗尽后仍不发言：尊重决定，不创建任何定时器。"""

        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(
                        hesitation_enabled=True,
                        hesitation_max_rounds=1,
                        hesitation_delay_seconds=1,
                    ),
                    think_engine=_make_think_engine(
                        _FakeModelClient(
                            '{"should_schedule": false, "delay_seconds": 60, "summary": "", "reason": "暂时没话说"}'
                        ),
                        event_bus,
                    ),
                    event_bus=event_bus,
                    character_name="测试角色",
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["source"], "reconsider")

                await asyncio.sleep(1.2)
                self.assertIsNone(manager.current_payload())
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_markdown_wrapped_decision_json_still_schedules_timer(self):
        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(),
                    think_engine=_make_think_engine(
                        _FakeModelClient(
                            "```json\n"
                            '{"should_schedule": true, "delay_seconds": 60, "summary": "Markdown 里的摘要", "reason": "想补充"}'
                            "\n```"
                        ),
                        event_bus,
                    ),
                    event_bus=event_bus,
                    character_name="测试角色",
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["pending_summary"], "Markdown 里的摘要")
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_decision_prompt_keeps_character_as_decision_owner_with_internal_json(self):
        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                model_client = _FakeModelClient()
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(),
                    think_engine=_make_think_engine(
                        model_client, event_bus, character_name="博丽灵梦"
                    ),
                    event_bus=event_bus,
                    character_name="博丽灵梦",
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("「赛钱箱在那边，随意投一点吧。」")
                self.assertIsNotNone(payload)
                self.assertIsNotNone(model_client.last_messages)
                assert model_client.last_messages is not None
                prompt = model_client.last_messages[0]["content"]
                self.assertIn("你是 博丽灵梦", prompt)
                self.assertIn("内部主动发言决定", prompt)
                self.assertIn(
                    "这个决定仍然必须由你以 博丽灵梦 的身份、性格、动机和当前上下文来完成", prompt
                )
                self.assertIn("不是用户可见台词", prompt)
                self.assertIn("不设置定时器", prompt)
                self.assertIn("用户再次输入前不再主动开口", prompt)
                self.assertIn("优先设置一个短到中等延迟的定时器", prompt)
                self.assertIn("只输出一个原始 JSON 对象", prompt)
                self.assertIn("不要输出 Markdown 代码块、角色引号、解释文本或任何前后缀", prompt)
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_structured_output_options_are_sent_when_supported(self):
        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                model_client = _FakeModelClient(structured_output=True)
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(),
                    think_engine=_make_think_engine(model_client, event_bus),
                    event_bus=event_bus,
                    character_name="测试角色",
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNotNone(payload)
                self.assertIsNotNone(model_client.last_options)
                assert model_client.last_options is not None
                response_format = model_client.last_options.get("response_format")
                self.assertEqual(response_format["type"], "json_schema")
                self.assertEqual(
                    response_format["json_schema"]["name"], "initiative_timer_decision"
                )
                self.assertTrue(response_format["json_schema"]["strict"])
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_structured_output_options_are_not_sent_when_unsupported(self):
        async def run():
            event_bus = EventBus(enable_trace=False)
            await event_bus.start()
            try:
                model_client = _FakeModelClient(structured_output=False)
                manager = InitiativeTimerManager(
                    config=InitiativeTimerConfig(),
                    think_engine=_make_think_engine(model_client, event_bus),
                    event_bus=event_bus,
                    character_name="测试角色",
                    working_memory=WorkingMemoryManager(max_turns=10),
                )

                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNotNone(payload)
                self.assertIsNotNone(model_client.last_options)
                assert model_client.last_options is not None
                self.assertNotIn("response_format", model_client.last_options)
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
                    working_memory=WorkingMemoryManager(max_turns=10),
                )
                manager._consecutive_initiative_count = 2

                payload = await manager.schedule_after_response("刚才的回复")
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
                    working_memory=WorkingMemoryManager(max_turns=10),
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
                    working_memory=WorkingMemoryManager(max_turns=10),
                )
                payload = await manager.schedule_after_response("刚才的回复")
                self.assertIsNotNone(payload)

                manager.increment_consecutive_initiative_count()
                manager.increment_consecutive_initiative_count()
                self.assertEqual(manager._consecutive_initiative_count, 2)

                # 非用户来源不应重置
                await manager.discard(reason="system_cleanup", source="system")
                self.assertEqual(manager._consecutive_initiative_count, 2)

                # 用户回复应重置
                await manager.schedule_after_response("新的回复")
                manager.increment_consecutive_initiative_count()
                await manager.discard(reason="user_message_received", source="user")
                self.assertEqual(manager._consecutive_initiative_count, 0)
            finally:
                await event_bus.stop()

        asyncio.run(run())

    def test_config_loader_persists_hesitation_enabled_in_yaml(self):
        from GensokyoAI.core.config import ConfigLoader

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "initiative_timer:\n  enabled: true\n  hesitation_max_rounds: 2\n",
                encoding="utf-8",
            )

            ConfigLoader.set_initiative_hesitation_enabled(path, True)
            text = path.read_text(encoding="utf-8")
            self.assertIn("  hesitation_enabled: true\n", text)
            self.assertLess(text.index("hesitation_enabled"), text.index("hesitation_max_rounds"))

            ConfigLoader.set_initiative_hesitation_enabled(path, False)
            text = path.read_text(encoding="utf-8")
            self.assertIn("  hesitation_enabled: false\n", text)
            self.assertEqual(text.count("hesitation_enabled"), 1)


if __name__ == "__main__":
    unittest.main()
