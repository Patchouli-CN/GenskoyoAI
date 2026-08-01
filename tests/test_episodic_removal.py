"""情景记忆子系统删除的回归测试：

- MemoryConfig 不再携带 episodic 字段，合并链不受影响
- 旧配置中的 memory.episodic_* 键不报未知字段错误，只给迁移警告
"""

import unittest

from GensokyoAI.core.config_schema import MemoryConfig
from GensokyoAI.core.config_validator import ConfigValidator


class EpisodicRemovalTests(unittest.TestCase):
    def test_memory_config_has_no_episodic_fields(self):
        config = MemoryConfig()
        self.assertFalse(hasattr(config, "episodic_threshold"))
        self.assertFalse(hasattr(config, "episodic_summary_model"))
        self.assertFalse(hasattr(config, "episodic_keep_recent"))

    def test_legacy_episodic_keys_warn_not_error(self):
        validator = ConfigValidator()
        diagnostics = validator.validate_config_dict(
            {
                "memory": {
                    "working_max_turns": 20,
                    "episodic_threshold": 50,
                    "episodic_summary_model": "qwen3.5:9b",
                    "episodic_keep_recent": 10,
                }
            }
        )
        errors = [d for d in diagnostics if d.severity == "error"]
        self.assertEqual(errors, [])
        deprecated = [d for d in diagnostics if d.code == "config.field.deprecated"]
        self.assertEqual(len(deprecated), 3)
        paths = {d.path for d in deprecated}
        self.assertIn("memory.episodic_threshold", paths)
        self.assertIn("memory.episodic_summary_model", paths)
        self.assertIn("memory.episodic_keep_recent", paths)


if __name__ == "__main__":
    unittest.main()
