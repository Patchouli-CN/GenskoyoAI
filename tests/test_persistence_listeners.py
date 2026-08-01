"""PersistenceListeners 回归测试：session_id 上报现查现报，不缓存注册时的会话"""

import asyncio
import unittest
from types import SimpleNamespace

from GensokyoAI.core.event_listeners import PersistenceListeners
from GensokyoAI.core.events import Event, SystemEvent


class _FakeEventBus:
    def __init__(self):
        self.published: list[Event] = []

    def subscribe(self, *args, **kwargs):
        pass

    def publish(self, event):
        self.published.append(event)


class _FakeSessionManager:
    def __init__(self):
        self._session = None

    def get_current_session(self):
        return self._session


class _FakeSaveCoordinator:
    async def save_async(self, working_memory, force=False):
        return True


class PersistenceListenersTests(unittest.TestCase):
    def test_session_id_is_reported_from_live_session(self):
        session_manager = _FakeSessionManager()
        agent = SimpleNamespace(
            session_manager=session_manager,
            save_coordinator=_FakeSaveCoordinator(),
            working_memory=None,
        )
        bus = _FakeEventBus()
        # 监听器注册时还没有会话（此前实现会把这个 None 缓存到永远）
        listener = PersistenceListeners(agent, bus)
        # 会话在注册之后才创建
        session_manager._session = SimpleNamespace(session_id="session-late-1")

        asyncio.run(
            listener._on_message_sent_for_persistence(
                Event(type=SystemEvent.MESSAGE_SENT, source="agent", data={"content": "hi"})
            )
        )

        completed = [
            event for event in bus.published if event.type == SystemEvent.PERSISTENCE_SAVE_COMPLETED
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].data["session_id"], "session-late-1")
        self.assertTrue(completed[0].data["success"])


if __name__ == "__main__":
    unittest.main()
