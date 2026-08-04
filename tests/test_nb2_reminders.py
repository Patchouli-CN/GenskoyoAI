"""到点提醒（backends/nb2/reminders 存储 + plugin 注意力代办链路）定向测试

判定全权归 LLM（AttentionThings）；本文件覆盖：ReminderStore CRUD/取消/
持久化、提醒种类的判定解析（intent 三态 + ISO due_at）、代办登记/取消/
待确认处置、到点投递。plugin 经 nonebot.init 导入，模块状态以 mock 替换。
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init(driver="~fastapi")  # plugin 导入期 get_driver() 需要驱动实例

from GensokyoAI.backends.nb2 import plugin  # noqa: E402
from GensokyoAI.backends.nb2.reminders import (  # noqa: E402
    REMINDER_MAX_ATTEMPTS,
    Reminder,
    ReminderStore,
    local_now,
)
from GensokyoAI.core.agent.attention import AttentionVerdict  # noqa: E402

_TZ8 = timezone(timedelta(hours=8))
_DUE_SOON = local_now() + timedelta(minutes=10)
_DUE_SOON_ISO = _DUE_SOON.isoformat()


def _make_reminder(store_due: datetime, **overrides) -> Reminder:
    options = {
        "id": "abc123",
        "agent_id": "qq-group-123",
        "key": "group:123",
        "kind": "group",
        "target_id": 123,
        "remind_qq": 456,
        "remind_name": "栗子",
        "content": "吃饭",
        "due": store_due,
        "created_at": store_due - timedelta(minutes=10),
    }
    options.update(overrides)
    return Reminder(**options)


class ReminderStoreTests(unittest.TestCase):
    def test_add_due_and_mark_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReminderStore(Path(tmpdir) / "r.json")
            past = _make_reminder(local_now() - timedelta(seconds=1))
            future = _make_reminder(local_now() + timedelta(hours=1), id="future")
            store.add(past)
            store.add(future)
            due = store.due(local_now())
            self.assertEqual([item.id for item in due], ["abc123"])
            store.mark_done("abc123")
            self.assertEqual(store.due(local_now()), [])

    def test_persistence_and_attempts_reset_on_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "r.json"
            store = ReminderStore(path)
            store.add(_make_reminder(local_now() + timedelta(hours=1)))
            store.bump_attempts("abc123")
            reloaded = ReminderStore(path)
            self.assertEqual(reloaded.pending_count("qq-group-123"), 1)
            due = reloaded.due(local_now() + timedelta(hours=2))
            self.assertEqual(due[0].attempts, 0)  # 新进程新一轮投递机会

    def test_expired_reminder_dropped_on_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "r.json"
            store = ReminderStore(path)
            store.add(_make_reminder(local_now() - timedelta(days=2)))
            reloaded = ReminderStore(path)
            self.assertEqual(reloaded.pending_count("qq-group-123"), 0)

    def test_attempts_cap_excluded_from_due(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReminderStore(Path(tmpdir) / "r.json")
            reminder = _make_reminder(local_now() - timedelta(seconds=1))
            reminder.attempts = REMINDER_MAX_ATTEMPTS
            store.add(reminder)
            # 重试耗尽的僵尸提醒：不返回，且被直接清出存储（不占 pending 配额）
            self.assertEqual(store.due(local_now()), [])
            self.assertEqual(store.pending_count("qq-group-123"), 0)

    def test_pending_and_cancel_latest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReminderStore(Path(tmpdir) / "r.json")
            now = local_now()
            store.add(
                _make_reminder(
                    now + timedelta(hours=1), id="old", created_at=now - timedelta(hours=1)
                )
            )
            store.add(_make_reminder(now + timedelta(hours=2), id="new", created_at=now))
            self.assertEqual(
                [item.id for item in store.pending("qq-group-123")], ["old", "new"]
            )  # due 升序
            # 取消最近创建的（不是最早到点的）
            cancelled = store.cancel_latest("qq-group-123")
            self.assertEqual(cancelled.id, "new")
            self.assertEqual(store.pending_count("qq-group-123"), 1)
            self.assertIsNone(store.cancel_latest("qq-user-999"))  # 无待办返回 None

    def test_cancel_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ReminderStore(Path(tmpdir) / "r.json")
            store.add(_make_reminder(local_now() + timedelta(hours=1), id="a"))
            store.add(_make_reminder(local_now() + timedelta(hours=2), id="b"))
            store.add(
                _make_reminder(
                    local_now() + timedelta(hours=3),
                    id="c",
                    agent_id="qq-user-999",
                    key="user:999",
                    kind="user",
                    target_id=999,
                )
            )
            items = store.cancel_all("qq-group-123")
            self.assertEqual({item.id for item in items}, {"a", "b"})
            self.assertEqual(store.pending_count("qq-group-123"), 0)
            self.assertEqual(store.pending_count("qq-user-999"), 1)  # 别家不动


class _AttentionCase(unittest.IsolatedAsyncioTestCase):
    """提醒种类/代办共用的模块状态补丁（tmp 存储 + 群目标 + 名片缓存）。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._store = ReminderStore(Path(self._tmpdir.name) / "r.json")
        self._patches = [
            patch.object(plugin, "_reminders", self._store),
            patch.object(plugin, "_targets", {"qq-group-123": ("group", 123)}),
            patch.object(plugin, "_member_names", {(123, 456): "栗子"}),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()
        self._tmpdir.cleanup()


class ReminderAttentionParseTests(_AttentionCase):
    """判定解析：intent 三态 + ISO due_at（判定全权 LLM，代码只解析）。"""

    def test_candidate_always_true(self):
        kind = plugin._ReminderAttentionKind()
        # 用户定稿「别代码里判断」：预筛恒真，全部交给 LLM 判定
        self.assertTrue(kind.candidate("3分钟后叫我一下"))
        self.assertTrue(kind.candidate("今天天气怎么样"))

    def test_parse_reminder_intent_with_iso_due(self):
        kind = plugin._ReminderAttentionKind()
        data = kind.parse(
            f'{{"intent": "reminder", "due_at": "{_DUE_SOON_ISO}", '
            '"content": "喊一下", "target_name": "栗子"}'
        )
        self.assertEqual(data["intent"], "reminder")
        self.assertEqual(data["content"], "喊一下")
        self.assertEqual(data["target_name"], "栗子")
        self.assertIsNotNone(data["due"])

    def test_parse_reminder_without_due_or_bad_iso(self):
        kind = plugin._ReminderAttentionKind()
        data = kind.parse('{"intent": "reminder", "due_at": "", "content": "喊一下"}')
        self.assertIsNone(data["due"])  # 待确认路径
        data = kind.parse('{"intent": "reminder", "due_at": "不是日期", "content": "x"}')
        self.assertIsNone(data["due"])

    def test_parse_cancel_intent(self):
        kind = plugin._ReminderAttentionKind()
        self.assertEqual(
            kind.parse('{"intent": "cancel", "scope": "all"}'),
            {"intent": "cancel", "scope": "all"},
        )
        self.assertEqual(
            kind.parse('{"intent": "cancel"}'),
            {"intent": "cancel", "scope": "latest"},  # 默认最近一条
        )

    def test_parse_none_and_garbage(self):
        kind = plugin._ReminderAttentionKind()
        self.assertIsNone(kind.parse('{"intent": "none"}'))
        self.assertIsNone(kind.parse("不是 JSON"))
        self.assertIsNone(kind.parse('{"intent": "reminder", "due_at": "", "content": ""}'))


class ReminderAttentionDispatchTests(_AttentionCase):
    async def test_dispatch_registers_and_returns_directive(self):
        verdict = AttentionVerdict(
            kind="reminder",
            data={
                "intent": "reminder",
                "due": _DUE_SOON,
                "content": "吃饭",
                "target_name": "栗子",
            },
        )
        note = await plugin._dispatch_attention(verdict, "qq-group-123")
        self.assertIsNotNone(note)
        self.assertIn("已代办", note)
        self.assertIn("吃饭", note)
        # 直接代办登记（不求模型调工具）：存储里已有一条
        reminder = self._store.due(local_now() + timedelta(minutes=11))[0]
        self.assertEqual(reminder.remind_qq, 456)
        self.assertEqual(reminder.content, "吃饭")

    async def test_dispatch_no_due_returns_clarify_note(self):
        verdict = AttentionVerdict(
            kind="reminder",
            data={"intent": "reminder", "due": None, "content": "那个事", "target_name": ""},
        )
        note = await plugin._dispatch_attention(verdict, "qq-group-123")
        self.assertIsNotNone(note)
        self.assertIn("待确认", note)
        self.assertEqual(self._store.pending_count("qq-group-123"), 0)

    async def test_dispatch_cancel_latest(self):
        self._store.add(_make_reminder(local_now() + timedelta(hours=1)))
        verdict = AttentionVerdict(kind="reminder", data={"intent": "cancel", "scope": "latest"})
        note = await plugin._dispatch_attention(verdict, "qq-group-123")
        self.assertIn("已代办", note)
        self.assertIn("取消", note)
        self.assertEqual(self._store.pending_count("qq-group-123"), 0)

    async def test_dispatch_cancel_all(self):
        store = self._store
        store.add(_make_reminder(local_now() + timedelta(hours=1), id="a"))
        store.add(_make_reminder(local_now() + timedelta(hours=2), id="b"))
        verdict = AttentionVerdict(kind="reminder", data={"intent": "cancel", "scope": "all"})
        note = await plugin._dispatch_attention(verdict, "qq-group-123")
        self.assertIn("取消", note)
        self.assertEqual(store.pending_count("qq-group-123"), 0)

    async def test_dispatch_cancel_with_nothing_pending(self):
        verdict = AttentionVerdict(kind="reminder", data={"intent": "cancel", "scope": "all"})
        note = await plugin._dispatch_attention(verdict, "qq-group-123")
        self.assertIn("没有登记", note)


class FireReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._store = ReminderStore(Path(self._tmpdir.name) / "r.json")
        self._store_patch = patch.object(plugin, "_reminders", self._store)
        self._store_patch.start()

    def tearDown(self):
        self._store_patch.stop()
        self._tmpdir.cleanup()

    async def test_group_fire_sends_at_and_marks_done(self):
        reminder = _make_reminder(local_now() - timedelta(seconds=1))
        self._store.add(reminder)
        bot = AsyncMock()
        with (
            patch.object(plugin, "get_bots", return_value={"1": bot}),
            patch.object(
                plugin, "_generate_for_tenant", new=AsyncMock(return_value="喂，吃饭啦")
            ) as generate,
        ):
            await plugin._fire_reminder(reminder)
        generate.assert_awaited_once()
        # 触发上下文带上了到点提醒注入
        contexts = generate.await_args.args[3]
        self.assertTrue(any("到点提醒" in item for item in contexts))
        bot.send_group_msg.assert_awaited_once()
        message = bot.send_group_msg.await_args.kwargs["message"]
        self.assertIn("[CQ:at,qq=456]", str(message))
        self.assertIn("吃饭啦", str(message))
        self.assertEqual(self._store.pending_count("qq-group-123"), 0)

    async def test_group_fire_strips_model_own_text_mention(self):
        # 模型自己开头写的文本 @ 与程序真 at 重复：剥掉只留程序 at
        reminder = _make_reminder(local_now() - timedelta(seconds=1))
        self._store.add(reminder)
        bot = AsyncMock()
        with (
            patch.object(plugin, "get_bots", return_value={"1": bot}),
            patch.object(
                plugin, "_generate_for_tenant", new=AsyncMock(return_value="@栗子 吃饭啦")
            ),
        ):
            await plugin._fire_reminder(reminder)
        message = str(bot.send_group_msg.await_args.kwargs["message"])
        self.assertIn("[CQ:at,qq=456]", message)
        self.assertNotIn("@栗子", message)
        self.assertIn("吃饭啦", message)

    async def test_private_fire_without_at(self):
        reminder = _make_reminder(
            local_now() - timedelta(seconds=1),
            kind="user",
            target_id=3072252442,
            key="user:3072252442",
            agent_id="qq-user-3072252442",
            remind_qq=3072252442,
            remind_name="帕秋莉",
        )
        bot = AsyncMock()
        with (
            patch.object(plugin, "get_bots", return_value={"1": bot}),
            patch.object(plugin, "_generate_for_tenant", new=AsyncMock(return_value="该吃饭啦")),
        ):
            await plugin._fire_reminder(reminder)
        bot.send_private_msg.assert_awaited_once()
        message = bot.send_private_msg.await_args.kwargs["message"]
        self.assertNotIn("CQ:at", str(message))

    async def test_no_bot_bumps_attempts_and_keeps_pending(self):
        reminder = _make_reminder(local_now() - timedelta(seconds=1))
        self._store.add(reminder)
        with patch.object(plugin, "get_bots", return_value={}):
            await plugin._fire_reminder(reminder)
        self.assertEqual(self._store.pending_count("qq-group-123"), 1)
        self.assertEqual(self._store.due(local_now())[0].attempts, 1)

    async def test_generate_failure_retries_then_gives_up(self):
        reminder = _make_reminder(local_now() - timedelta(seconds=1))
        reminder.attempts = REMINDER_MAX_ATTEMPTS - 1
        self._store.add(reminder)
        with (
            patch.object(plugin, "get_bots", return_value={"1": AsyncMock()}),
            patch.object(
                plugin,
                "_generate_for_tenant",
                new=AsyncMock(side_effect=RuntimeError("炸了")),
            ),
        ):
            await plugin._fire_reminder(reminder)
        # 达到重试上限后放弃（从待办移除，不再反复烧 token）
        self.assertEqual(self._store.pending_count("qq-group-123"), 0)


if __name__ == "__main__":
    unittest.main()
