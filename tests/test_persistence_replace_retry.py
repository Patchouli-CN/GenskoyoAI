"""原子替换瞬态重试（session/persistence._replace_with_retry）定向测试"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from GensokyoAI.session.persistence import _replace_with_retry


def _winerror(code: int) -> OSError:
    error = OSError("模拟锁错误")
    error.winerror = code  # CPython 允许挂自定义属性（winerror 仅在 Windows 原生存在）
    return error


class ReplaceWithRetryTests(unittest.TestCase):
    def test_transient_winerror5_heals_on_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir) / "a.tmp"
            target = Path(tmpdir) / "a.json"
            tmp.write_text("{}", encoding="utf-8")
            attempts = {"count": 0}
            real_replace = Path.replace

            def flaky(self: Path, dst: Path) -> None:
                attempts["count"] += 1
                if attempts["count"] <= 2:
                    raise _winerror(5)
                return real_replace(self, dst)

            with (
                patch.object(Path, "replace", flaky),
                patch("GensokyoAI.session.persistence.time.sleep", lambda _: None),
            ):
                _replace_with_retry(tmp, target)  # 两次 WinError 5 后成功
            self.assertEqual(attempts["count"], 3)
            self.assertTrue(target.exists())
            self.assertFalse(tmp.exists())

    def test_gives_up_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir) / "a.tmp"
            target = Path(tmpdir) / "a.json"
            tmp.write_text("{}", encoding="utf-8")

            def always_fail(self: Path, dst: Path) -> None:
                raise _winerror(32)

            with (
                patch.object(Path, "replace", always_fail),
                patch("GensokyoAI.session.persistence.time.sleep", lambda _: None),
                self.assertRaises(OSError),
            ):
                _replace_with_retry(tmp, target)

    def test_non_transient_error_raises_immediately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir) / "a.tmp"
            target = Path(tmpdir) / "a.json"
            tmp.write_text("{}", encoding="utf-8")
            attempts = {"count": 0}

            def fatal(self: Path, dst: Path) -> None:
                attempts["count"] += 1
                raise _winerror(87)  # 参数错误：非瞬态

            with (
                patch.object(Path, "replace", fatal),
                patch("GensokyoAI.session.persistence.time.sleep", lambda _: None),
                self.assertRaises(OSError),
            ):
                _replace_with_retry(tmp, target)
            self.assertEqual(attempts["count"], 1)  # 不重试


if __name__ == "__main__":
    unittest.main()
