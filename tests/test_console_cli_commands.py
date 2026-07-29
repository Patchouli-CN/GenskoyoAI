import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from GensokyoAI.commands.context import CommandContext
from GensokyoAI.commands.executor import CommandExecutor
from GensokyoAI.core.agent.types import UnifiedMessage


class _FakeBackend:
    def __init__(self, agent):
        self.agent = agent
        self._prompt_context = []
        self.panels = []

    def _show_initiative_timer_panel(self, timer):
        self.panels.append(("timer", timer))

    def _show_initiative_trigger_result(self, result):
        self.panels.append(("trigger", result))

    def _show_history_messages_panel(self, messages, *, session=None, limit=20):
        self.panels.append(("history", list(messages), session, limit))

    def _show_history_file_hint(self, path, message):
        self.panels.append(("file", Path(path), message))

    def _show_regenerated_message(self, message):
        self.panels.append(("regen", message))

    def _build_system_contexts(self):
        return list(self._prompt_context[-5:])


class _FakeSessionManager:
    def __init__(self):
        self.session = SimpleNamespace(session_id="session-12345678", total_turns=1)
        self.messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ]
        self.persistence = SimpleNamespace(load_messages=self.load_messages)

    def get_current_session(self):
        return self.session

    def load_messages(self, session_id):
        assert session_id == self.session.session_id
        return [dict(message) for message in self.messages]

    def replace_messages(self, session_id, messages):
        assert session_id == self.session.session_id
        self.messages = [dict(message) for message in messages]
        self.session.total_turns = len(self.messages) // 2
        return True


class _FakeAgent:
    def __init__(self):
        self.session_manager = _FakeSessionManager()
        self.timer = {
            "timer_id": "timer-1",
            "generation": 1,
            "status": "scheduled",
            "pending_summary": "稍后补充一句",
            "editable_fields": ["due_at", "delay_seconds", "pending_summary"],
        }
        self.updated = None
        self.cancelled_reason = None
        self.sent_messages = []
        self.hesitation_enabled = False
        self.hesitation_config_path = "config/default.yaml"

    def current_initiative_timer(self):
        return self.timer

    async def update_initiative_timer(self, **kwargs):
        self.updated = kwargs
        self.timer = {**self.timer, **{k: v for k, v in kwargs.items() if v is not None}}
        return self.timer

    async def cancel_initiative_timer(self, *, reason="cancelled", timer_id=None):
        self.cancelled_reason = reason
        self.timer = {"timer_id": timer_id or "timer-1", "status": "cancelled", "reason": reason}
        return self.timer

    async def trigger_initiative_timer(self, *, timer_id=None):
        return {
            "timer_id": timer_id or "timer-1",
            "sent": True,
            "pending_summary": "稍后补充一句",
            "message": "主动消息",
        }

    def initiative_hesitation_status(self):
        return {"enabled": self.hesitation_enabled, "config_path": self.hesitation_config_path}

    def set_initiative_hesitation_enabled(self, enabled, *, persist=True):
        self.hesitation_enabled = bool(enabled)
        return {
            "enabled": self.hesitation_enabled,
            "persist": persist,
            "config_path": self.hesitation_config_path,
        }

    async def send(self, content, system_contexts=None):
        self.sent_messages.append((content, system_contexts or []))
        self.session_manager.messages.append({"role": "user", "content": content})
        self.session_manager.messages.append({"role": "assistant", "content": "重生成回复"})
        return UnifiedMessage(role="assistant", content="重生成回复")


async def _execute(text: str, agent: _FakeAgent, backend: _FakeBackend):
    executor = CommandExecutor(mode="smart")
    ctx = CommandContext(
        agent=cast(Any, agent),
        backend=cast(Any, backend),
        source="test",
        issuer="pytest",
    )
    return await executor.execute(text, ctx)


def test_timer_command_supports_prefix_and_tag_forms():
    agent = _FakeAgent()
    backend = _FakeBackend(agent)

    results, clean_text = asyncio.run(_execute("/timer", agent, backend))
    assert clean_text == ""
    assert results[0].status.name == "SUCCESS"
    assert backend.panels[-1][0] == "timer"

    results, _ = asyncio.run(_execute("<timer>summary 新摘要</timer>", agent, backend))
    assert results[0].status.name == "SUCCESS"
    assert agent.updated is not None
    assert agent.updated["pending_summary"] == "新摘要"

    asyncio.run(_execute("/timer update delay 30", agent, backend))
    assert agent.updated is not None
    assert agent.updated["delay_seconds"] == 30

    asyncio.run(_execute("/timer cancel 手动取消", agent, backend))
    assert agent.cancelled_reason == "手动取消"

    asyncio.run(_execute("/timer trigger", agent, backend))
    assert backend.panels[-1][0] == "trigger"


def test_history_command_can_show_edit_import_and_regenerate(tmp_path):
    agent = _FakeAgent()
    backend = _FakeBackend(agent)

    results, clean_text = asyncio.run(_execute("/history", agent, backend))
    assert clean_text == ""
    assert results[0].status.name == "SUCCESS"
    assert backend.panels[-1][0] == "history"

    export_path = tmp_path / "history.json"
    asyncio.run(_execute(f"/history export {export_path}", agent, backend))
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["message_count"] == 2
    assert exported["messages"][0]["content"] == "你好"

    imported_path = tmp_path / "edited.json"
    imported_path.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "改写后的用户消息"},
                    {"role": "assistant", "content": "改写后的助手消息"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    asyncio.run(_execute(f"<history>import {imported_path}</history>", agent, backend))
    assert agent.session_manager.messages[0]["content"] == "改写后的用户消息"

    asyncio.run(_execute('/history insert 1 assistant "插入消息"', agent, backend))
    assert agent.session_manager.messages[1] == {"role": "assistant", "content": "插入消息"}

    asyncio.run(_execute("/history delete 1", agent, backend))
    assert agent.session_manager.messages[1]["content"] == "改写后的助手消息"

    asyncio.run(_execute("/history regen 1", agent, backend))
    assert agent.sent_messages[-1][0] == "改写后的用户消息"
    assert ("regen", "重生成回复") in backend.panels
