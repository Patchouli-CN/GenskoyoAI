"""Director 单元测试：合法调度、非法/离场降级、JSON 失败、超时与轮数熔断。"""

import asyncio
import json
import unittest
from types import SimpleNamespace

from GensokyoAI.core.agent.types import ProviderCapability
from GensokyoAI.core.config import WorldDirectorConfig
from GensokyoAI.core.events import EventBus, SystemEvent
from GensokyoAI.world import (
    USER_OCCUPANT_ID,
    ActorBrief,
    Director,
    DirectorAction,
    DirectorContext,
    DirectorPhase,
)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.message = SimpleNamespace(content=content)


class _FakeModelClient:
    """模拟 ModelClient：按队列返回回复或抛出异常，并记录每次调用。"""

    def __init__(self, replies: list, *, structured_output: bool = False) -> None:
        self._replies = list(replies)
        self._structured_output = structured_output
        self.calls: list[dict] = []

    def supports(self, capability: str) -> bool:
        return capability == ProviderCapability.STRUCTURED_OUTPUT and self._structured_output

    async def chat(self, messages, options=None, **_kwargs):
        self.calls.append({"messages": list(messages), "options": dict(options or {})})
        if not self._replies:
            raise AssertionError("模型不应再被调用")
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


def _reply(
    action: str,
    next_character: str | None = None,
    reason: str = "剧情需要",
    confidence: float = 0.8,
) -> str:
    return json.dumps(
        {
            "action": action,
            "next_character": next_character,
            "reason": reason,
            "confidence": confidence,
        },
        ensure_ascii=False,
    )


def _context(**overrides) -> DirectorContext:
    base: dict = {
        "phase": DirectorPhase.AFTER_ACTOR,
        "scene_id": "scarlet_devil_mansion",
        "candidates": [
            ActorBrief(actor_id="marisa", display_name="雾雨魔理沙", summary="好奇心旺盛的魔法使"),
            ActorBrief(
                actor_id="patchouli", display_name="帕秋莉·诺蕾姬", summary="不动的大图书馆"
            ),
        ],
        "current_actor_id": "marisa",
        "transcript_text": "雾雨魔理沙：这本书我借走啦！",
        "auto_turn_count": 1,
        "same_actor_turn_count": 1,
    }
    base.update(overrides)
    return DirectorContext(**base)


def _director(client: _FakeModelClient, **config_overrides) -> Director:
    return Director(client, WorldDirectorConfig(**config_overrides))


class DirectorLegalDecisionTests(unittest.TestCase):
    def test_continue_legal(self):
        client = _FakeModelClient([_reply("continue", reason="魔理沙话没说完")])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.CONTINUE)
        self.assertIsNone(decision.next_actor_id)
        self.assertEqual(decision.reason, "魔理沙话没说完")
        self.assertAlmostEqual(decision.confidence, 0.8)
        self.assertFalse(decision.fallback_applied)

    def test_switch_legal(self):
        client = _FakeModelClient([_reply("switch", next_character="patchouli")])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.SWITCH)
        self.assertEqual(decision.next_actor_id, "patchouli")
        self.assertFalse(decision.fallback_applied)

    def test_wait_user_legal(self):
        client = _FakeModelClient([_reply("wait_user", reason="该等用户回应了")])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertIsNone(decision.next_actor_id)
        self.assertFalse(decision.fallback_applied)

    def test_confidence_clamped_and_default(self):
        client = _FakeModelClient(
            [
                _reply("wait_user", confidence=1.7),
                json.dumps({"action": "wait_user", "next_character": None, "reason": "x"}),
                json.dumps(
                    {
                        "action": "wait_user",
                        "next_character": None,
                        "reason": "x",
                        "confidence": "高",
                    }
                ),
            ]
        )
        director = _director(client)
        clamped = asyncio.run(director.decide(_context()))
        missing = asyncio.run(director.decide(_context()))
        malformed = asyncio.run(director.decide(_context()))
        self.assertEqual(clamped.confidence, 1.0)
        self.assertEqual(missing.confidence, 0.0)
        self.assertEqual(malformed.confidence, 0.0)


class DirectorFallbackTests(unittest.TestCase):
    def test_switch_to_offscene_actor_falls_back(self):
        client = _FakeModelClient([_reply("switch", next_character="reimu")])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)
        self.assertIn("reimu", decision.reason)

    def test_switch_to_current_actor_falls_back(self):
        client = _FakeModelClient([_reply("switch", next_character="marisa")])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)

    def test_switch_to_user_falls_back(self):
        client = _FakeModelClient([_reply("switch", next_character=USER_OCCUPANT_ID)])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)

    def test_switch_missing_target_falls_back(self):
        client = _FakeModelClient([_reply("switch", next_character=None)])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)

    def test_invalid_switch_with_continue_fallback(self):
        client = _FakeModelClient([_reply("switch", next_character="reimu")])
        director = _director(client, fallback_action="continue")
        decision = asyncio.run(director.decide(_context()))
        self.assertEqual(decision.action, DirectorAction.CONTINUE)
        self.assertTrue(decision.fallback_applied)

    def test_continue_fallback_blocked_by_same_actor_cap(self):
        client = _FakeModelClient([_reply("switch", next_character="reimu")])
        director = _director(client, fallback_action="continue")
        decision = asyncio.run(director.decide(_context(same_actor_turn_count=2)))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)

    def test_continue_without_current_actor_falls_back(self):
        client = _FakeModelClient([_reply("continue")])
        context = _context(phase=DirectorPhase.AFTER_USER, current_actor_id=None)
        decision = asyncio.run(_director(client).decide(context))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)

    def test_continue_with_offscene_current_actor_falls_back(self):
        client = _FakeModelClient([_reply("continue")])
        context = _context(current_actor_id="reimu")  # 不在候选列表内
        decision = asyncio.run(_director(client).decide(context))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)

    def test_continue_at_same_actor_cap_falls_back(self):
        client = _FakeModelClient([_reply("continue")])
        decision = asyncio.run(_director(client).decide(_context(same_actor_turn_count=2)))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)


class DirectorCircuitBreakerTests(unittest.TestCase):
    def test_max_auto_turns_forces_wait_user_without_model_call(self):
        client = _FakeModelClient([])
        decision = asyncio.run(_director(client).decide(_context(auto_turn_count=4)))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)
        self.assertIn("max_auto_turns", decision.reason)
        self.assertEqual(len(client.calls), 0)  # 硬熔断不调用模型，省 token

    def test_empty_candidates_waits_user_without_model_call(self):
        client = _FakeModelClient([])
        decision = asyncio.run(_director(client).decide(_context(candidates=[])))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)
        self.assertEqual(len(client.calls), 0)

    def test_json_parse_failure_retries_then_succeeds(self):
        client = _FakeModelClient(["这不是 JSON 而是角色台词", _reply("wait_user")])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertFalse(decision.fallback_applied)
        self.assertEqual(len(client.calls), 2)
        # 第二次调用带上了自我修正提示
        retry_messages = client.calls[1]["messages"]
        self.assertEqual(len(retry_messages), 4)
        self.assertEqual(retry_messages[-1]["role"], "user")

    def test_json_parse_failure_twice_waits_user(self):
        client = _FakeModelClient(["垃圾输出", "依然是垃圾"])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)
        self.assertEqual(len(client.calls), 2)

    def test_invalid_action_value_treated_as_parse_failure(self):
        client = _FakeModelClient([_reply("dance"), _reply("dance")])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)
        self.assertEqual(len(client.calls), 2)

    def test_model_exception_waits_user(self):
        client = _FakeModelClient([RuntimeError("模型超时")])
        decision = asyncio.run(_director(client).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)
        self.assertTrue(decision.fallback_applied)
        self.assertEqual(len(client.calls), 1)


class DirectorPromptAndOptionsTests(unittest.TestCase):
    def test_structured_output_response_format(self):
        client = _FakeModelClient([_reply("wait_user")], structured_output=True)
        asyncio.run(_director(client).decide(_context()))
        options = client.calls[0]["options"]
        self.assertEqual(options["temperature"], 0.2)
        # thinking 预算下限：max(配置 384, DECISION_MIN_MAX_TOKENS 1024)
        self.assertEqual(options["max_tokens"], 1024)
        self.assertEqual(
            options["response_format"]["json_schema"]["name"], "world_director_decision"
        )

    def test_no_response_format_without_capability(self):
        client = _FakeModelClient([_reply("wait_user")], structured_output=False)
        asyncio.run(_director(client).decide(_context()))
        self.assertNotIn("response_format", client.calls[0]["options"])

    def test_prompt_assembly(self):
        client = _FakeModelClient([_reply("wait_user")])
        context = _context(
            phase=DirectorPhase.INITIATIVE,
            initiative_summary="图书馆的寂静被打破了",
            scene_description="红魔馆大图书馆，书山如海",
            same_actor_turn_count=2,  # 达到默认 max_same_actor_turns
        )
        asyncio.run(_director(client).decide(context))
        messages = client.calls[0]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        user_msg = messages[1]["content"]
        self.assertIn("marisa（雾雨魔理沙）：好奇心旺盛的魔法使", user_msg)
        self.assertIn("patchouli（帕秋莉·诺蕾姬）：不动的大图书馆", user_msg)
        self.assertIn("雾雨魔理沙：这本书我借走啦！", user_msg)
        self.assertIn("红魔馆大图书馆", user_msg)
        self.assertIn('本轮不允许 "continue"', user_msg)
        self.assertIn("图书馆的寂静被打破了", user_msg)

    def test_prompt_marks_no_current_actor(self):
        client = _FakeModelClient([_reply("wait_user")])
        context = _context(phase=DirectorPhase.AFTER_USER, current_actor_id=None)
        asyncio.run(_director(client).decide(context))
        user_msg = client.calls[0]["messages"][1]["content"]
        self.assertIn("无（等待首个回应者）", user_msg)
        self.assertIn('本轮不允许 "continue"', user_msg)


class DirectorEventTests(unittest.TestCase):
    def test_decision_event_published(self):
        async def scenario():
            bus = EventBus(enable_trace=False)
            await bus.start()
            received = asyncio.Event()
            payload: dict = {}

            async def handler(event):
                payload.update(event.data)
                received.set()

            bus.subscribe(SystemEvent.WORLD_DIRECTOR_DECISION, handler)
            client = _FakeModelClient([_reply("switch", next_character="patchouli")])
            director = Director(client, WorldDirectorConfig(), event_bus=bus)
            decision = await director.decide(_context())
            await asyncio.wait_for(received.wait(), timeout=2)
            await bus.stop()
            return decision, payload

        decision, payload = asyncio.run(scenario())
        self.assertEqual(decision.action, DirectorAction.SWITCH)
        self.assertEqual(payload["action"], "switch")
        self.assertEqual(payload["next_actor_id"], "patchouli")
        self.assertEqual(payload["phase"], "after_actor")
        self.assertEqual(payload["scene_id"], "scarlet_devil_mansion")
        self.assertEqual(payload["candidates"], ["marisa", "patchouli"])
        self.assertEqual(payload["current_actor_id"], "marisa")
        self.assertFalse(payload["fallback_applied"])

    def test_no_event_bus_still_decides(self):
        client = _FakeModelClient([_reply("wait_user")])
        decision = asyncio.run(Director(client, WorldDirectorConfig()).decide(_context()))
        self.assertEqual(decision.action, DirectorAction.WAIT_USER)


if __name__ == "__main__":
    unittest.main()
