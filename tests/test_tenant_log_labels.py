"""关闭日志租户标签（Agent/ThinkEngine/BackgroundManager/SaveCoordinator 的 label 后缀）定向测试"""

import unittest
from unittest.mock import MagicMock

from GensokyoAI.background.manager import BackgroundManager
from GensokyoAI.core.agent._impl import Agent
from GensokyoAI.core.agent.save_coordinator import SaveCoordinator
from GensokyoAI.core.agent.think_engine import ThinkEngine
from GensokyoAI.core.config import ThinkEngineConfig


class TenantSuffixTests(unittest.TestCase):
    def test_agent_suffix_with_and_without_label(self):
        agent = Agent.__new__(Agent)
        agent._log_label = "qq-group-263402786"
        self.assertEqual(agent._tenant_suffix, " (租户: qq-group-263402786)")
        agent._log_label = ""
        self.assertEqual(agent._tenant_suffix, "")

    def test_think_engine_suffix(self):
        labeled = ThinkEngine(
            MagicMock(), MagicMock(), MagicMock(), "幽幽子", ThinkEngineConfig(),
            log_label="qq-group-1",
        )
        self.assertEqual(labeled._log_suffix, ", 租户: qq-group-1")
        plain = ThinkEngine(MagicMock(), MagicMock(), MagicMock(), "幽幽子", ThinkEngineConfig())
        self.assertEqual(plain._log_suffix, "")

    def test_background_manager_suffix(self):
        labeled = BackgroundManager(label="qq-group-1")
        self.assertEqual(labeled._log_suffix, " (租户: qq-group-1)")
        plain = BackgroundManager()
        self.assertEqual(plain._log_suffix, "")

    def test_save_coordinator_suffix(self):
        labeled = SaveCoordinator(None, None, label="qq-group-1")  # type: ignore[arg-type]
        self.assertEqual(labeled._log_suffix, " (租户: qq-group-1)")
        plain = SaveCoordinator(None, None)  # type: ignore[arg-type]
        self.assertEqual(plain._log_suffix, "")


if __name__ == "__main__":
    unittest.main()
