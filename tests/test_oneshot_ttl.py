"""OneShotGenerator 缓存 TTL（04#13 回归）：到期重装配，
改 key/模型/人设后无需重启进程即可生效。"""

import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from GensokyoAI.core.agent.oneshot import _CACHE_TTL_SECONDS, OneShotGenerator


class OneShotCacheTtlTests(unittest.TestCase):
    def test_fresh_cache_hit_and_expired_rebuild(self):
        generator = OneShotGenerator(Path("/tmp/nonexistent"))
        builds = []

        def fake_load(self, *args, **kwargs):
            builds.append(len(builds))
            from GensokyoAI.core.config import AppConfig

            return AppConfig()

        fake_character = SimpleNamespace(name="Reimu", system_prompt="你是灵梦")

        with (
            patch("GensokyoAI.core.agent.oneshot.ConfigLoader.load", fake_load),
            patch.object(
                OneShotGenerator,
                "_load_character",
                lambda self, loader, character: fake_character,
            ),
            patch("GensokyoAI.core.agent.oneshot.ModelClient", lambda *a, **k: object()),
            patch(
                "GensokyoAI.core.agent.oneshot.build_roleplay_system_prompt",
                lambda name, prompt: "sp",
            ),
        ):
            generator._resolve("Reimu")
            generator._resolve("Reimu")
            self.assertEqual(len(builds), 1)  # TTL 内命中缓存，不重装配

            # 人为拨老缓存时间：到期后重装配
            client, sp, _ = generator._cache["Reimu"]
            generator._cache["Reimu"] = (client, sp, time.monotonic() - _CACHE_TTL_SECONDS - 1)
            generator._resolve("Reimu")
            self.assertEqual(len(builds), 2)


if __name__ == "__main__":
    unittest.main()
