"""World 配置链测试：schema 解析、校验诊断、合并、示例文件加载。"""

import unittest
from pathlib import Path

from GensokyoAI.core.config import ConfigLoader, WorldConfig
from GensokyoAI.core.config_validator import ConfigValidator


class WorldConfigParsingTests(unittest.TestCase):
    def setUp(self):
        self.loader = ConfigLoader()

    def test_default_world_is_disabled(self):
        # 不配置 world 时，AppConfig 有默认 WorldConfig 且关闭
        config = self.loader._dict_to_config({})
        self.assertIsInstance(config.world, WorldConfig)
        self.assertFalse(config.world.enabled)
        self.assertEqual(config.world.protagonist, "__user__")

    def test_world_nested_structs_and_path_conversion(self):
        data = {
            "world": {
                "enabled": True,
                "id": "gensokyo",
                "protagonist": "marisa",
                "actors": [
                    {
                        "id": "marisa",
                        "character_file": "characters/zh_cn/KirisameMarisa.yaml",
                        "initial_scene": "magic_forest",
                    },
                    {"id": "remilia", "character_file": "x.yaml", "enabled": False},
                ],
                "director": {"temperature": 0.3, "fallback_action": "continue"},
                "transcript": {"context_entries": 12},
                "persistence": {"save_path": "./sessions/worlds"},
            }
        }
        config = self.loader._dict_to_config(data)

        self.assertTrue(config.world.enabled)
        self.assertEqual([a.id for a in config.world.actors], ["marisa", "remilia"])
        # 嵌套 struct 正确构造
        self.assertEqual(config.world.director.temperature, 0.3)
        self.assertEqual(config.world.director.fallback_action, "continue")
        self.assertEqual(config.world.transcript.context_entries, 12)
        # Path 字段转换
        self.assertIsInstance(config.world.actors[0].character_file, Path)
        self.assertIsInstance(config.world.persistence.save_path, Path)
        self.assertFalse(config.world.actors[1].enabled)


class WorldConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.validator = ConfigValidator()

    def _world_codes(self, data: dict) -> set[str]:
        diags = self.validator.validate_config_dict(data)
        return {d.code for d in diags if d.path.startswith("world")}

    def test_duplicate_actor_id_flagged(self):
        codes = self._world_codes(
            {"world": {"enabled": True, "actors": [{"id": "a"}, {"id": "a"}]}}
        )
        self.assertIn("config.world.actor_id_duplicate", codes)

    def test_unknown_protagonist_flagged(self):
        codes = self._world_codes(
            {"world": {"enabled": True, "protagonist": "ghost", "actors": [{"id": "a"}]}}
        )
        self.assertIn("config.world.protagonist_unknown", codes)

    def test_protagonist_user_sentinel_is_valid(self):
        codes = self._world_codes(
            {"world": {"enabled": True, "protagonist": "__user__", "actors": [{"id": "a"}]}}
        )
        self.assertNotIn("config.world.protagonist_unknown", codes)

    def test_enabled_world_requires_enabled_actor(self):
        codes = self._world_codes(
            {"world": {"enabled": True, "actors": [{"id": "a", "enabled": False}]}}
        )
        self.assertIn("config.world.no_enabled_actor", codes)

    def test_disabled_world_skips_roster_requirement(self):
        # 关闭态不因空 roster 报错，避免误报
        codes = self._world_codes({"world": {"enabled": False, "actors": []}})
        self.assertNotIn("config.world.no_enabled_actor", codes)

    def test_director_enum_and_range_flagged(self):
        codes = self._world_codes(
            {
                "world": {
                    "enabled": True,
                    "actors": [{"id": "a"}],
                    "director": {"fallback_action": "nope", "max_auto_turns": 0},
                }
            }
        )
        self.assertIn("config.field.enum", codes)
        self.assertIn("config.field.range", codes)

    def test_unknown_world_field_flagged(self):
        codes = self._world_codes({"world": {"enabled": True, "actors": [{"id": "a"}], "bogus": 1}})
        self.assertTrue(any(c.startswith("config.") for c in codes))


class WorldConfigHardeningTests(unittest.TestCase):
    """审查后硬化：标量类型、persistence 类型、protagonist 指向 disabled、
    temperature 上限、transcript 交叉校验。"""

    def setUp(self):
        self.validator = ConfigValidator()

    def _world_diags(self, data: dict):
        diags = self.validator.validate_config_dict(data)
        return [d for d in diags if d.path.startswith("world")]

    def _world_codes(self, data: dict) -> set[str]:
        return {d.code for d in self._world_diags(data)}

    @staticmethod
    def _valid_world() -> dict:
        return {
            "world": {
                "enabled": True,
                "protagonist": "a",
                "actors": [{"id": "a", "character_file": "x.yaml"}],
            }
        }

    def test_valid_world_has_no_hardening_codes(self):
        self.assertEqual(self._world_codes(self._valid_world()), set())

    def test_persistence_non_dict_flagged(self):
        data = self._valid_world()
        data["world"]["persistence"] = "junk"
        self.assertIn("config.world.persistence_type", self._world_codes(data))

    def test_protagonist_pointing_to_disabled_actor_flagged(self):
        data = self._valid_world()
        data["world"]["actors"] = [
            {"id": "a", "enabled": False},
            {"id": "b", "character_file": "y.yaml"},
        ]
        self.assertIn("config.world.protagonist_disabled", self._world_codes(data))

    def test_director_temperature_max_flagged(self):
        data = self._valid_world()
        data["world"]["director"] = {"temperature": 99}
        self.assertIn("config.field.range", self._world_codes(data))

    def test_scalar_type_errors_flagged(self):
        data = self._valid_world()
        data["world"]["id"] = 123
        data["world"]["actors"][0]["enabled"] = "no"
        data["world"]["actors"][0]["initial_scene"] = 123
        codes = self._world_codes(data)
        self.assertIn("config.field.type", codes)

    def test_transcript_cross_check_warns(self):
        data = self._valid_world()
        data["world"]["transcript"] = {"context_entries": 1000, "max_entries_per_scene": 10}
        diags = self._world_diags(data)
        warnings = {d.code for d in diags if d.severity == "warning"}
        self.assertIn("config.world.transcript_entries_exceed_max", warnings)


class WorldExampleFileTests(unittest.TestCase):
    def test_world_example_yaml_loads_and_is_enabled(self):
        loader = ConfigLoader()
        config = loader.load(Path("tmp/temp-world.yaml"))

        self.assertTrue(config.world.enabled)
        self.assertEqual(config.world.protagonist, "marisa")
        actor_ids = [a.id for a in config.world.actors]
        self.assertEqual(actor_ids, ["marisa", "remilia"])
        # 引用的角色卡真实存在，示例可运行
        for actor in config.world.actors:
            self.assertTrue(
                actor.character_file is not None and actor.character_file.exists(),
                f"角色卡不存在: {actor.character_file}",
            )

    def test_template_conf_world_section_disabled(self):
        loader = ConfigLoader()
        config = loader.load(Path("tmp/template-conf.yaml"))
        self.assertFalse(config.world.enabled)


if __name__ == "__main__":
    unittest.main()
