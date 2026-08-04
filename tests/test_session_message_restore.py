from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from GensokyoAI.core.config import SessionConfig
from GensokyoAI.session.context import SessionContext
from GensokyoAI.session.manager import SessionManager
from GensokyoAI.session.persistence import SessionPersistence


class SessionMessageRestoreTests(unittest.TestCase):
    def test_restore_and_resave_preserves_structured_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            save_path = Path(temp_dir)
            persistence = SessionPersistence(save_path)
            session = SessionContext(character_id="reimu")
            messages = [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "需要先查时间。",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "get_current_time",
                                "arguments": {},
                            },
                        }
                    ],
                    "provider_extension": {"trace": [1, 2, 3]},
                },
                {
                    "role": "tool",
                    "content": "12:00",
                    "tool_call_id": "call-1",
                    "name": "get_current_time",
                },
            ]
            persistence.save_session(session)
            persistence.save_messages(session.session_id, messages)

            manager = SessionManager(SessionConfig(save_path=save_path), "reimu")
            restored = manager.get_working_memory(session.session_id).get_context()
            without_identity = [
                {key: value for key, value in item.items() if key not in {"message_id", "revision"}}
                for item in restored
            ]
            self.assertEqual(without_identity, messages)
            self.assertTrue(all(item["message_id"] for item in restored))
            self.assertTrue(all(item["revision"] == 1 for item in restored))

            manager.save_working_memory(session.session_id)
            self.assertEqual(persistence.load_messages(session.session_id), restored)


if __name__ == "__main__":
    unittest.main()
