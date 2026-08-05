"""fetch_url 工具 + 知识站点指令渲染定向测试

护栏：SSRF 校验（私网/元数据拒绝）、HTML 剥标签、输出截断、
失败时给模型干净人话（技术细节不进台词——ddg 错误串泄漏的教训）。
"""

import unittest
from unittest.mock import patch

from GensokyoAI.core.config import ModelConfig, ToolConfig, WebSearchToolConfig
from GensokyoAI.core.config_schema import KnowledgeSiteConfig
from GensokyoAI.tools.build_service import ToolBuildContext, ToolBuildService
from GensokyoAI.tools.registry import ToolRegistry
from GensokyoAI.tools.tool_builtin.fetch_url import _strip_html, fetch_url


class _FakeContent:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, limit: int) -> bytes:
        return self._data[:limit]


class _FakeResponse:
    def __init__(self, status: int, content_type: str, data: bytes):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = _FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, response=None, error: Exception | None = None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        if self._error is not None:
            raise self._error
        return self._response


def _patch_session(response=None, error=None):
    return patch(
        "GensokyoAI.tools.tool_builtin.fetch_url.aiohttp.ClientSession",
        lambda *args, **kwargs: _FakeSession(response, error),
    )


class FetchUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_ssrf_rejected(self):
        result = await fetch_url("http://169.254.169.254/latest/meta-data")
        self.assertFalse(result["ok"])
        self.assertIn("不允许访问", result["error"])

    async def test_html_stripped(self):
        response = _FakeResponse(
            200,
            "text/html; charset=utf-8",
            "<html><head><style>x{}</style></head><body><p>幽幽子</p><script>bad()</script><b>西行寺</b>&amp;妖梦</body></html>".encode(),
        )
        with _patch_session(response):
            result = await fetch_url("https://thbwiki.cc/西行寺幽幽子")
        self.assertTrue(result["ok"])
        self.assertIn("幽幽子", result["content"])
        self.assertIn("西行寺", result["content"])
        self.assertNotIn("script", result["content"])
        self.assertNotIn("<p>", result["content"])
        self.assertIn("&妖梦", result["content"])  # &amp; 已反转义

    async def test_json_not_stripped(self):
        response = _FakeResponse(200, "application/json", b'{"name": "yuyuko"}')
        with _patch_session(response):
            result = await fetch_url("https://thbwiki.cc/api.php?action=query")
        self.assertTrue(result["ok"])
        self.assertIn('"name"', result["content"])

    async def test_truncation(self):
        response = _FakeResponse(200, "text/plain", ("很长" * 5000).encode())
        with _patch_session(response):
            result = await fetch_url("https://example.com/long")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["content"]), 4000)

    async def test_failure_gives_clean_message(self):
        with _patch_session(error=TimeoutError("operation timed out")):
            result = await fetch_url("https://www.startpage.com/")
        self.assertFalse(result["ok"])
        self.assertIn("稍后再试", result["error"])
        self.assertNotIn("startpage", result["error"])  # 技术细节不进模型台词
        self.assertNotIn("timed out", result["error"])


class StripHtmlTests(unittest.TestCase):
    def test_entities_unescaped(self):
        self.assertEqual(_strip_html("<p>a &amp; b</p>").strip(), "a & b")

    def test_script_style_removed(self):
        self.assertNotIn("alert", _strip_html("<script>alert(1)</script>正文"))


class KnowledgeSiteInstructionTests(unittest.TestCase):
    def _build(self, sites, builtin_tools):
        service = ToolBuildService(ToolRegistry())
        return service.build(
            ToolBuildContext(
                tool_config=ToolConfig(
                    enabled=True,
                    builtin_tools=builtin_tools,
                    web_search=WebSearchToolConfig(knowledge_sites=sites),
                ),
                model_config=ModelConfig(provider="openai", name="gpt-4o"),
            )
        )

    def test_sites_rendered_when_fetch_url_enabled(self):
        result = self._build(
            [KnowledgeSiteConfig(site="thbwiki.cc", desc="东方 Project 中文维基")],
            ["fetch_url"],
        )
        self.assertIn("知识站点", result.instructions)
        self.assertIn("thbwiki.cc", result.instructions)
        self.assertIn("东方 Project 中文维基", result.instructions)
        self.assertIn("fetch_url", result.enabled_tool_names)

    def test_not_rendered_without_fetch_url(self):
        result = self._build([KnowledgeSiteConfig(site="thbwiki.cc", desc="x")], ["time"])
        self.assertNotIn("【知识站点】", result.instructions)

    def test_not_rendered_without_sites(self):
        result = self._build([], ["fetch_url"])
        self.assertNotIn("【知识站点】", result.instructions)

    def test_yaml_dict_items_become_knowledge_site_config(self):
        """回归：yaml 加载的 knowledge_sites 项必须转成 KnowledgeSiteConfig——
        msgspec 构造不深度转换 list 项，否则指令渲染取 .site 属性时炸（实机事故）。"""
        from GensokyoAI.core.config_loader import ConfigLoader

        config = ConfigLoader()._dict_to_config(
            {
                "tool": {
                    "web_search": {"knowledge_sites": [{"site": "thbwiki.cc", "desc": "东方维基"}]}
                }
            }
        )
        item = config.tool.web_search.knowledge_sites[0]
        self.assertIsInstance(item, KnowledgeSiteConfig)
        self.assertEqual(item.site, "thbwiki.cc")
        self.assertEqual(item.desc, "东方维基")


if __name__ == "__main__":
    unittest.main()
