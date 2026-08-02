"""到点提醒（backends/nb2/reminders + plugin 提醒链路）定向测试

parse_when / ReminderStore 为纯逻辑直测；工具闭包与投递路径经 nonebot.init
后导入 plugin、以 mock 替换模块状态（_reminders/_targets/_member_names/
get_bots/_generate_for_tenant）验证。
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
    parse_when,
)

_TZ8 = timezone(timedelta(hours=8))
_NOW = datetime(2026, 8, 2, 10, 0, 0, tzinfo=_TZ8)  # 周日 10:00


class ParseWhenTests(unittest.TestCase):
    def test_relative_units(self):
        self.assertEqual(parse_when("30秒后", _NOW), _NOW + timedelta(seconds=30))
        self.assertEqual(parse_when("10分钟后", _NOW), _NOW + timedelta(minutes=10))
        self.assertEqual(parse_when("2小时后", _NOW), _NOW + timedelta(hours=2))
        self.assertEqual(parse_when("1天后", _NOW), _NOW + timedelta(days=1))
        self.assertEqual(parse_when("1.5小时", _NOW), _NOW + timedelta(hours=1.5))

    def test_clock_today_and_rollover(self):
        self.assertEqual(
            parse_when("15:30", _NOW), datetime(2026, 8, 2, 15, 30, tzinfo=_TZ8)
        )
        # 时刻已过 → 顺延到明天
        self.assertEqual(
            parse_when("08:00", _NOW), datetime(2026, 8, 3, 8, 0, tzinfo=_TZ8)
        )

    def test_day_prefix(self):
        self.assertEqual(
            parse_when("明天 08:00", _NOW), datetime(2026, 8, 3, 8, 0, tzinfo=_TZ8)
        )
        self.assertEqual(
            parse_when("后天 7:30", _NOW), datetime(2026, 8, 4, 7, 30, tzinfo=_TZ8)
        )

    def test_absolute_datetime(self):
        self.assertEqual(
            parse_when("2026-08-03 15:30", _NOW), datetime(2026, 8, 3, 15, 30, tzinfo=_TZ8)
        )

    def test_invalid_returns_none(self):
        for bad in ("", "随便什么时候", "25:30", "12:70", "2026-13-01 10:00"):
            self.assertIsNone(parse_when(bad, _NOW), bad)


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
            self.assertEqual(store.due(local_now()), [])


class ReminderToolTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_set_reminder_success(self):
        tool = plugin._build_reminder_tool("qq-group-123")
        result = await tool("10分钟后", "吃饭", "栗子")
        self.assertIn("记下啦", result)
        self.assertEqual(self._store.pending_count("qq-group-123"), 1)
        reminder = self._store.due(local_now() + timedelta(minutes=11))[0]
        self.assertEqual(reminder.remind_qq, 456)
        self.assertEqual(reminder.content, "吃饭")
        self.assertEqual(reminder.kind, "group")

    async def test_set_reminder_bad_time(self):
        tool = plugin._build_reminder_tool("qq-group-123")
        self.assertIn("没看懂", await tool("随便", "吃饭"))
        self.assertIn("太近", await tool("5秒后", "吃饭"))
        self.assertEqual(self._store.pending_count("qq-group-123"), 0)

    async def test_set_reminder_without_target(self):
        with patch.object(plugin, "_targets", {}):
            tool = plugin._build_reminder_tool("qq-group-123")
            self.assertIn("还不知道往哪儿说", await tool("10分钟后", "吃饭"))

    async def test_unknown_name_falls_back_to_no_at(self):
        tool = plugin._build_reminder_tool("qq-group-123")
        result = await tool("10分钟后", "开会", "不存在的人")
        self.assertIn("记下啦", result)
        reminder = self._store.due(local_now() + timedelta(minutes=11))[0]
        self.assertIsNone(reminder.remind_qq)
        self.assertEqual(reminder.remind_name, "不存在的人")


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

    async def test_private_fire_without_at(self):
        reminder = _make_reminder(
            local_now() - timedelta(seconds=1),
            kind="user", target_id=3072252442, key="user:3072252442",
            agent_id="qq-user-3072252442", remind_qq=3072252442, remind_name="帕秋莉",
        )
        bot = AsyncMock()
        with (
            patch.object(plugin, "get_bots", return_value={"1": bot}),
            patch.object(
                plugin, "_generate_for_tenant", new=AsyncMock(return_value="该吃饭啦")
            ),
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
