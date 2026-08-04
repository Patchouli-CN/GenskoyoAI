import tempfile
import unittest
from pathlib import Path

from GensokyoAI.core.config import ConfigLoader
from GensokyoAI.core.release_resources import (
    bundled_resource_path,
    find_character_resource,
    logical_character_path,
    resolve_resource_path,
)
from GensokyoAI.runtime.service import RuntimeService


class ReleaseResourceTests(unittest.TestCase):
    def test_shipped_resources_are_available(self):
        self.assertTrue(bundled_resource_path("tmp", "template-conf.yaml").is_file())
        self.assertTrue(
            bundled_resource_path("characters", "zh_cn", "KirisameMarisa.yaml").is_file()
        )
        self.assertTrue(bundled_resource_path("scenes", "zh_cn").is_dir())

    def test_operator_resource_overrides_bundled_resource(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            template = root / "tmp" / "template-conf.yaml"
            template.parent.mkdir()
            template.write_text("operator: true\n", encoding="utf-8")
            self.assertEqual(resolve_resource_path(root, "tmp", template.name), template)

    def test_character_falls_back_to_bundled_resource_with_stable_logical_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            character = find_character_resource("KirisameMarisa", root)
            self.assertTrue(character.is_file())
            self.assertEqual(
                logical_character_path(character, root),
                str(Path("characters") / "zh_cn" / "KirisameMarisa.yaml"),
            )

    def test_character_lookup_prefers_operator_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            character = root / "characters" / "zh_cn" / "KirisameMarisa.yaml"
            character.parent.mkdir(parents=True)
            character.write_text("name: override\n", encoding="utf-8")
            self.assertEqual(
                find_character_resource("KirisameMarisa", root).resolve(),
                character.resolve(),
            )

    def test_character_identifier_rejects_absolute_and_parent_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = root.parent / "outside-character.yaml"
            outside.write_text("name: outside\n", encoding="utf-8")
            try:
                with self.assertRaises(FileNotFoundError):
                    find_character_resource(str(outside), root)
                with self.assertRaises(FileNotFoundError):
                    find_character_resource("../outside-character.yaml", root)
            finally:
                outside.unlink(missing_ok=True)

    def test_config_loader_resolves_scenes_and_world_characters_for_runtime_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config" / "local.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "world:\n"
                "  actors:\n"
                "    - id: marisa\n"
                "      character_file: characters/zh_cn/KirisameMarisa.yaml\n",
                encoding="utf-8",
            )
            config = ConfigLoader().load(config_path, resource_root=root)
            self.assertTrue(config.scene.library_path.is_dir())
            self.assertTrue(config.world.actors[0].character_file.is_file())


class RuntimeReleaseResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_lists_bundled_characters_with_reusable_logical_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = RuntimeService(Path(tmpdir))
            characters = await service.list_characters("zh_cn")
            marisa = next(item for item in characters if item["id"] == "KirisameMarisa")
            self.assertEqual(
                marisa["path"], str(Path("characters") / "zh_cn" / "KirisameMarisa.yaml")
            )
            self.assertTrue(service._resolve_character(marisa["path"], None).is_file())

    def test_runtime_character_name_cannot_escape_runtime_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = RuntimeService(Path(tmpdir))
            with self.assertRaises(FileNotFoundError):
                service._resolve_character(None, str(Path(tmpdir).parent / "outside.yaml"))


if __name__ == "__main__":
    unittest.main()
