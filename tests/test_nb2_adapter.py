"""NoneBot2 适配器单元测试（store/config/runtime_host；不导入 nonebot）。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from GensokyoAI.backends.nb2.config import DEFAULT_EXTRA_PROMPT, Nb2Config
from GensokyoAI.backends.nb2.runtime_host import RuntimeHost, RuntimeRpcError
from GensokyoAI.backends.nb2.store import SessionStore
from GensokyoAI.core.events import Event, EventBus, SystemEvent
from GensokyoAI.runtime.resource_control import ResourceLimitError
from GensokyoAI.runtime.rpc import RpcError
from GensokyoAI.runtime.service import RuntimeService
from GensokyoAI.utils.helpers import split_reply_segments, strip_rp_style


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sub" / "sessions.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_put_get_roundtrip(self):
        store = SessionStore(self.path)
        store.put("group:123", agent_id="qq-group-123", session_id="s-1", revision=3)
        self.assertEqual(
            store.get("group:123"),
            {"agent_id": "qq-group-123", "session_id": "s-1", "revision": 3},
        )
        self.assertIsNone(store.get("group:999"))

    def test_persistence_across_instances(self):
        SessionStore(self.path).put("user:1", agent_id="qq-user-1", session_id="s-1", revision=0)
        self.assertEqual(SessionStore(self.path).get("user:1")["session_id"], "s-1")

    def test_update_revision(self):
        store = SessionStore(self.path)
        store.put("group:1", agent_id="a", session_id="s", revision=1)
        store.update_revision("group:1", 7)
        self.assertEqual(store.get("group:1")["revision"], 7)
        store.update_revision("group:missing", 9)  # 键不存在：静默忽略

    def test_corrupted_file_recovers_empty(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        store = SessionStore(self.path)
        self.assertIsNone(store.get("group:1"))
        store.put("group:1", agent_id="a", session_id="s", revision=0)  # 损坏后仍可写入
        self.assertEqual(store.get("group:1")["revision"], 0)

    def test_get_returns_copy(self):
        store = SessionStore(self.path)
        store.put("group:1", agent_id="a", session_id="s", revision=1)
        store.get("group:1")["revision"] = 999
        self.assertEqual(store.get("group:1")["revision"], 1)


class Nb2ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = Nb2Config.from_env({}.get)
        self.assertEqual(config.character, "KirisameMarisa")
        self.assertEqual(config.data_dir, Path("nb2_data"))
        self.assertIsNone(config.root_dir)
        self.assertEqual(config.group_whitelist, frozenset())
        self.assertTrue(config.initiative)
        self.assertEqual(config.extra_prompt, DEFAULT_EXTRA_PROMPT)
        self.assertTrue(config.split_reply)
        self.assertTrue(config.strip_rp_style)

    def test_parse_from_env(self):
        env = {
            "GSK_NB2_CHARACTER": "HakureiReimu",
            "GSK_NB2_DATA_DIR": "data/nb2",
            "GSK_NB2_ROOT_DIR": "D:/gsk",
            "GSK_NB2_GROUP_WHITELIST": "123，456, 789 ,abc,",  # 含中文逗号/空格/非数字
            "GSK_NB2_INITIATIVE": "false",
            "GSK_NB2_EXTRA_PROMPT": " 只说日语。 ",
            "GSK_NB2_SPLIT_REPLY": "0",
            "GSK_NB2_STRIP_RP_STYLE": "false",
        }
        config = Nb2Config.from_env(env.get)
        self.assertEqual(config.character, "HakureiReimu")
        self.assertEqual(config.data_dir, Path("data/nb2"))
        self.assertEqual(config.root_dir, Path("D:/gsk"))
        self.assertEqual(config.group_whitelist, frozenset({123, 456, 789}))
        self.assertFalse(config.initiative)
        self.assertEqual(config.extra_prompt, "只说日语。")
        self.assertFalse(config.split_reply)
        self.assertFalse(config.strip_rp_style)

    def test_initiative_bool_parsing(self):
        self.assertFalse(Nb2Config.from_env({"GSK_NB2_INITIATIVE": "0"}.get).initiative)
        self.assertFalse(Nb2Config.from_env({"GSK_NB2_INITIATIVE": "off"}.get).initiative)
        self.assertTrue(Nb2Config.from_env({"GSK_NB2_INITIATIVE": "yes"}.get).initiative)
        self.assertTrue(Nb2Config.from_env({"GSK_NB2_INITIATIVE": " "}.get).initiative)


class SplitReplySegmentsTests(unittest.TestCase):
    """按行拆段（utils.helpers.split_reply_segments）：行边界即句子边界。"""

    def test_splits_by_lines_and_drops_blanks(self):
        text = "第一句。\n\n第二句。\n   \n第三句。"
        self.assertEqual(split_reply_segments(text), ["第一句。", "第二句。", "第三句。"])

    def test_single_line_single_segment(self):
        self.assertEqual(split_reply_segments("就一句话。"), ["就一句话。"])

    def test_never_cuts_inside_a_line(self):
        # 长行不硬切：宁可整条发出，也不出现「没说完」的半截句
        long_line = "这是一句" + "很长" * 60 + "的话。"
        segments = split_reply_segments(long_line)
        self.assertEqual(segments, [long_line])

    def test_over_limit_merges_tail_without_losing_content(self):
        text = "\n".join(f"第{i}句。" for i in range(1, 9))
        segments = split_reply_segments(text, max_segments=5)
        self.assertEqual(len(segments), 5)
        self.assertEqual(segments[:4], ["第1句。", "第2句。", "第3句。", "第4句。"])
        for part in ("第5句。", "第6句。", "第7句。", "第8句。"):
            self.assertIn(part, segments[4])

    def test_empty_input_returns_single_blank(self):
        self.assertEqual(split_reply_segments("  \n\n "), [""])


class StripRpStyleTests(unittest.TestCase):
    """发送前 RP 风格清洗（utils.helpers.strip_rp_style）。"""

    def test_action_only_line_removed(self):
        text = "你好呀\n*轻笑出声*\n今天天气不错"
        self.assertEqual(strip_rp_style(text), "你好呀\n今天天气不错")

    def test_inline_action_removed(self):
        self.assertEqual(strip_rp_style("看*挥舞扇子*这个"), "看这个")

    def test_corner_quotes_stripped(self):
        self.assertEqual(strip_rp_style("「呵呵，差不多吧」"), "呵呵，差不多吧")

    def test_combined_rp_style(self):
        text = "「那孩子反应总是很大呢～」\n*用扇子轻点下巴*\n「可爱得让人想再捉弄一次呢～」"
        self.assertEqual(
            strip_rp_style(text), "那孩子反应总是很大呢～\n可爱得让人想再捉弄一次呢～"
        )

    def test_empty_after_strip(self):
        self.assertEqual(strip_rp_style("*微笑*"), "")

    def test_plain_text_unchanged(self):
        self.assertEqual(strip_rp_style("Master Spark 天下第一！"), "Master Spark 天下第一！")


class RuntimeHostWrapperTests(unittest.TestCase):
    """ensure_agent / send_message 的参数整形与重试逻辑（_call 打桩）。"""

    def test_ensure_agent_disables_initiative_by_default(self):
        async def run():
            host = RuntimeHost()
            calls = []

            async def fake_call(method, params=None):
                calls.append((method, dict(params or {})))
                if method == "agent.init":
                    return {"session": {"session_id": "s-1", "revision": 5}}
                return {"enabled": False}

            host._call = fake_call
            session_id, revision = await host.ensure_agent("qq-group-1", "KirisameMarisa")
            self.assertEqual((session_id, revision), ("s-1", 5))
            self.assertEqual([m for m, _ in calls], ["agent.init", "initiative_timer.update"])
            self.assertEqual(calls[0][1]["agent_id"], "qq-group-1")
            self.assertEqual(calls[0][1]["character"], "KirisameMarisa")
            self.assertEqual(
                calls[1][1],
                {"agent_id": "qq-group-1", "session_id": "s-1", "enabled": False},
            )

        asyncio.run(run())

    def test_ensure_agent_keeps_initiative_when_delivery_enabled(self):
        async def run():
            host = RuntimeHost()
            calls = []

            async def fake_call(method, params=None):
                calls.append(method)
                return {"session": {"session_id": "s-1", "revision": 0}}

            host._call = fake_call
            await host.ensure_agent("qq-group-1", "KirisameMarisa", disable_initiative=False)
            self.assertEqual(calls, ["agent.init"])

        asyncio.run(run())

    def test_send_message_revision_conflict_refreshes_and_retries(self):
        async def run():
            host = RuntimeHost()
            sends = []

            async def fake_call(method, params=None):
                if method == "session.messages":
                    return {"revision": 8}
                sends.append(dict(params))
                if len(sends) == 1:
                    raise RuntimeRpcError("session.revision_conflict", "冲突")
                return {"content": "ok", "session": {"revision": 9}}

            host._call = fake_call
            reply, revision = await host.send_message(
                "qq-group-1", "s-1", 3, "你好", idempotency_key="k"
            )
            self.assertEqual((reply, revision), ("ok", 9))
            self.assertEqual(sends[0]["expected_revision"], 3)
            self.assertEqual(sends[1]["expected_revision"], 8)
            # 同一幂等键：冲突重试不会产生重复发言
            self.assertEqual(sends[0]["idempotency_key"], sends[1]["idempotency_key"])

        asyncio.run(run())

    def test_send_message_passes_system_contexts_through(self):
        """附加要求透传 RPC system_contexts；不传则不出现在参数里。"""

        async def run():
            host = RuntimeHost()
            captured = []

            async def fake_call(method, params=None):
                captured.append(dict(params or {}))
                return {"content": "ok", "session": {"revision": 4}}

            host._call = fake_call
            await host.send_message(
                "qq-group-1",
                "s-1",
                3,
                "你好",
                idempotency_key="k",
                system_contexts=["【QQ 聊天场景附加要求】\n简短口语化"],
            )
            self.assertEqual(
                captured[0]["system_contexts"], ["【QQ 聊天场景附加要求】\n简短口语化"]
            )

            captured.clear()
            await host.send_message("qq-group-1", "s-1", 4, "再见", idempotency_key="k2")
            self.assertNotIn("system_contexts", captured[0])

        asyncio.run(run())


class RuntimeHostErrorTranslationTests(unittest.TestCase):
    """_call 的错误翻译：RpcError / ResourceLimitError → RuntimeRpcError（code 保留）。"""

    def test_rpc_error_code_preserved(self):
        async def run():
            host = RuntimeHost()
            host._service.handle = AsyncMock(
                side_effect=RpcError("租户不存在", code="agent.not_found")
            )
            with self.assertRaises(RuntimeRpcError) as ctx:
                await host._call("session.messages")
            self.assertEqual(ctx.exception.code, "agent.not_found")

        asyncio.run(run())

    def test_resource_limit_mapped_with_details(self):
        async def run():
            host = RuntimeHost()
            host._service.handle = AsyncMock(
                side_effect=ResourceLimitError("runtime", "queue_full", 4, 8, 4, 8)
            )
            with self.assertRaises(RuntimeRpcError) as ctx:
                await host._call("agent.send_message")
            self.assertEqual(ctx.exception.code, "resource.limit_exceeded")
            self.assertEqual(ctx.exception.details["reason"], "queue_full")

        asyncio.run(run())


class RuntimeHostEventTests(unittest.TestCase):
    """进程内事件推送：订阅 → 租户总线发布 → on_event 按租户收到。"""

    def test_subscribe_pumps_tenant_events_until_cancel(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                service = RuntimeService(root)
                child = RuntimeService(
                    root,
                    tenant_key=("nb2", "agent-1"),
                    storage_root=service._tenant_storage_root("nb2", "agent-1"),
                )
                service._tenant_services[("nb2", "agent-1")] = child

                async def _noop_shutdown():
                    return None

                event_bus = EventBus(enable_trace=False)
                await event_bus.start()
                child.state.agent = SimpleNamespace(
                    event_bus=event_bus, shutdown=_noop_shutdown
                )

                host = RuntimeHost(user_id="nb2", service=service)
                received = []
                done = asyncio.Event()

                async def on_event(agent_id, payload):
                    received.append((agent_id, payload))
                    done.set()

                await host.subscribe_events("agent-1", on_event)
                event_bus.publish(
                    Event(
                        type=SystemEvent.MESSAGE_SENT,
                        source="initiative_timer",
                        data={"content": "主动发言", "initiative": True},
                    )
                )
                await asyncio.wait_for(done.wait(), timeout=5)
                self.assertEqual(received[0][0], "agent-1")
                self.assertEqual(received[0][1]["type"], "message.sent")
                self.assertEqual(received[0][1]["data"]["content"], "主动发言")

                # 取消订阅后不再投递
                await host.cancel_events("agent-1")
                received.clear()
                done.clear()
                event_bus.publish(
                    Event(
                        type=SystemEvent.MESSAGE_SENT,
                        source="test",
                        data={"content": "不应投递", "initiative": True},
                    )
                )
                await asyncio.sleep(0.1)
                self.assertEqual(received, [])

                await event_bus.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
