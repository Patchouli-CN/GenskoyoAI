"""一次性脱稿生成器（core/agent/oneshot）定向测试"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from GensokyoAI.core.agent.oneshot import OneShotGenerator
from GensokyoAI.core.agent.types import UnifiedMessage, UnifiedResponse

_CHARACTER_YAML = """
name: 测试角色
system_prompt: |
  你是测试角色，语气慵懒。
"""


def _make_root(tmp: Path, *, zh_cn: bool = False) -> Path:
    (tmp / "config").mkdir()
    (tmp / "config" / "local.yaml").write_text(yaml.safe_dump({}), encoding="utf-8")
    char_dir = tmp / "characters" / ("zh_cn" if zh_cn else "")
    char_dir.mkdir(parents=True)
    (char_dir / "TestChar.yaml").write_text(_CHARACTER_YAML, encoding="utf-8")
    return tmp


def _fake_client(text: str) -> MagicMock:
    client = MagicMock()

    async def chat(messages, **kwargs):
        chat.calls = messages
        return UnifiedResponse(message=UnifiedMessage(content=text))

    chat.calls = None
    client.chat = chat
    return client


class OneShotGenerateTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_uses_character_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_root(Path(tmpdir))
            client = _fake_client("  是个安静的人呢  ")
            with patch(
                "GensokyoAI.core.agent.oneshot.ModelClient", return_value=client
            ) as model_cls:
                generator = OneShotGenerator(root)
                text = await generator.generate("TestChar", "写第一印象")
            self.assertEqual(text, "是个安静的人呢")  # 结果去空白
            system, user = client.chat.calls
            self.assertIn("测试角色", system["content"])
            self.assertEqual(user["content"], "写第一印象")
            model_cls.assert_called_once()

    async def test_client_cached_per_character(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_root(Path(tmpdir))
            with patch(
                "GensokyoAI.core.agent.oneshot.ModelClient",
                side_effect=lambda *a, **k: _fake_client("x"),
            ) as model_cls:
                generator = OneShotGenerator(root)
                await generator.generate("TestChar", "一")
                await generator.generate("TestChar", "二")
            self.assertEqual(model_cls.call_count, 1)  # 第二次复用缓存

    async def test_character_resolved_from_zh_cn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_root(Path(tmpdir), zh_cn=True)
            generator = OneShotGenerator(root)
            with patch(
                "GensokyoAI.core.agent.oneshot.ModelClient",
                side_effect=lambda *a, **k: _fake_client("x"),
            ):
                text = await generator.generate("TestChar", "hi")
            self.assertEqual(text, "x")

    async def test_missing_character_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_root(Path(tmpdir))
            generator = OneShotGenerator(root)
            with self.assertRaises(FileNotFoundError):
                await generator.generate("不存在", "hi")

    async def test_get_quota_delegates_to_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _make_root(Path(tmpdir))
            client = _fake_client("x")

            async def get_quota():
                return {"available_balance": 16.6}

            client.get_quota = get_quota
            with patch("GensokyoAI.core.agent.oneshot.ModelClient", return_value=client):
                generator = OneShotGenerator(root)
                quota = await generator.get_quota("TestChar")
            self.assertEqual(quota["available_balance"], 16.6)


if __name__ == "__main__":
    unittest.main()
