"""阶段 2.3：World 存档与世界级长期记忆命名空间。"""

import json
import tempfile
import unittest
from pathlib import Path

from GensokyoAI.core.schema_versions import (
    WORLD_SESSION_FILE_FORMAT,
    WORLD_SESSION_SCHEMA_VERSION,
)
from GensokyoAI.utils.helpers import build_world_memory_root
from GensokyoAI.world import (
    SpeakerKind,
    TranscriptEntry,
    WorldPersistence,
    WorldPersistenceError,
)


class WorldPersistenceTests(unittest.TestCase):
    def _full_record(self, persistence: WorldPersistence):
        record = persistence.create("gensokyo", session_id="play-1", metadata={"title": "红魔馆"})
        record.roster = {"marisa": "雾雨魔理沙", "remilia": "蕾米莉亚"}
        record.actor_sessions = {"marisa": "private-a", "remilia": "private-b"}
        record.stage = {"marisa": "sdm", "remilia": "sdm", "__user__": "sdm"}
        record.current_actor_id = "marisa"
        record.waiting_for_user = False
        record.transcript = {
            "sdm": [
                TranscriptEntry(
                    scene_id="sdm",
                    speaker_kind=SpeakerKind.CHARACTER,
                    speaker_id="marisa",
                    speaker_name="魔理沙",
                    content="到红魔馆了！",
                )
            ]
        }
        record.director_state = {"auto_turns": 2, "same_actor_turns": 1}
        record.initiative_state = {"pending_summary": "稍后继续探索"}
        persistence.save(record)
        return record

    def test_round_trip_list_export_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = WorldPersistence(Path(tmp))
            original = self._full_record(persistence)

            loaded = persistence.resume("gensokyo", "play-1")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.record.metadata["title"], "红魔馆")
            self.assertEqual(loaded.record.stage["marisa"], "sdm")
            self.assertEqual(loaded.record.transcript["sdm"][0].content, "到红魔馆了！")
            self.assertEqual(loaded.record.director_state["auto_turns"], 2)
            self.assertEqual(loaded.record.initiative_state["pending_summary"], "稍后继续探索")
            self.assertEqual([item.session_id for item in persistence.list("gensokyo")], ["play-1"])

            exported = persistence.export("gensokyo", "play-1")
            self.assertEqual(exported["schema_version"], WORLD_SESSION_SCHEMA_VERSION)
            self.assertEqual(exported["world_session"]["session_id"], original.session_id)
            self.assertTrue(persistence.delete("gensokyo", "play-1"))
            self.assertIsNone(persistence.resume("gensokyo", "play-1"))
            self.assertFalse(persistence.delete("gensokyo", "play-1"))

    def test_paths_are_sanitized_and_confined(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = WorldPersistence(Path(tmp))
            path = persistence.session_path(
                "../world evil", "../../session evil", create_parent=True
            )
            self.assertEqual(path.parent.parent, Path(tmp))
            self.assertNotIn("..", path.parts)
            self.assertEqual(path.parent.name, "world_evil")
            self.assertEqual(path.name, "session_evil.json")

    def test_save_creates_backup_and_corrupt_primary_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = WorldPersistence(Path(tmp))
            record = persistence.create("gensokyo", session_id="play")
            record.metadata["generation"] = 1
            persistence.save(record)
            record.metadata["generation"] = 2
            persistence.save(record)
            path = persistence.session_path("gensokyo", "play")
            backup = path.with_name(f"{path.name}.bak")
            self.assertTrue(backup.exists())

            path.write_bytes(b"{broken")
            result = persistence.resume("gensokyo", "play")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.record.metadata["generation"], 1)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["format"], WORLD_SESSION_FILE_FORMAT
            )

    def test_corrupt_primary_and_backup_are_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = WorldPersistence(Path(tmp))
            record = persistence.create("gensokyo", session_id="play")
            persistence.save(record)
            path = persistence.session_path("gensokyo", "play")
            backup = path.with_name(f"{path.name}.bak")
            path.write_bytes(b"bad")
            backup.write_bytes(b"bad backup")

            with self.assertRaises(WorldPersistenceError):
                persistence.resume("gensokyo", "play")
            self.assertFalse(path.exists())
            self.assertEqual(len(list((path.parent / "quarantine").glob("*.bad"))), 1)

    def test_rejects_future_version_and_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = WorldPersistence(Path(tmp))
            path = persistence.session_path("gensokyo", "play", create_parent=True)
            payload = {
                "format": WORLD_SESSION_FILE_FORMAT,
                "schema_version": WORLD_SESSION_SCHEMA_VERSION + 1,
                "world_session": {"world_id": "gensokyo", "session_id": "play"},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(WorldPersistenceError, "schema version"):
                persistence.resume("gensokyo", "play")

            payload["schema_version"] = WORLD_SESSION_SCHEMA_VERSION
            payload["world_session"]["world_id"] = "other-world"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(WorldPersistenceError, "World id 不匹配"):
                persistence.resume("gensokyo", "play")

    def test_roster_diagnostics_report_missing_and_added_actors(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = WorldPersistence(Path(tmp))
            record = persistence.create("gensokyo", session_id="play")
            record.roster = {"marisa": "魔理沙", "remilia": "蕾米莉亚"}
            persistence.save(record)

            result = persistence.resume("gensokyo", "play", available_actor_ids={"marisa", "reimu"})
            self.assertIsNotNone(result)
            assert result is not None
            codes = {(item.code, item.actor_id) for item in result.diagnostics}
            self.assertIn(("world.persistence.actor_missing", "remilia"), codes)
            self.assertIn(("world.persistence.actor_added", "reimu"), codes)


class WorldMemoryPathTests(unittest.TestCase):
    def test_world_memory_root_is_stable_isolated_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first = build_world_memory_root(base, "world/one", "Marisa/霧雨")
            same_world_next_session = build_world_memory_root(base, "world/one", "Marisa/霧雨")
            other_world = build_world_memory_root(base, "world/two", "Marisa/霧雨")
            other_actor = build_world_memory_root(base, "world/one", "Reimu")

            self.assertEqual(first, same_world_next_session)
            self.assertNotEqual(first, other_world)
            self.assertNotEqual(first, other_actor)
            self.assertEqual(first, base / "world" / "world_one" / "memory" / "Marisa_霧雨")


if __name__ == "__main__":
    unittest.main()
