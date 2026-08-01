import asyncio
import unittest
from unittest.mock import patch

from GensokyoAI.core.agent.message_builder import MessageBuilder
from GensokyoAI.core.config import ConfigLoader, ModelConfig, ToolConfig, WebSearchToolConfig
from GensokyoAI.tools.executor import ToolExecutor
from GensokyoAI.tools.registry import ToolRegistry
from GensokyoAI.tools.tool_builtin.web_search import configure_web_search_tool
from GensokyoAI.tools.web_search.providers.api import GenericAPISearchProvider
from GensokyoAI.tools.web_search.providers.bing import BingSearchProvider
from GensokyoAI.tools.web_search.providers.ddg import DuckDuckGoSearchProvider
from GensokyoAI.tools.web_search.service import WebSearchService
from GensokyoAI.tools.web_search.types import ProviderSearchResult, SearchItem


class _EmptyMemory:
    def get_context(self):
        return []

    def get_relevant_context(self, _query):
        return []


class WebSearchToolTests(unittest.TestCase):
    def test_config_loads_web_search_tool_fields(self):
        data = {
            "tool": {
                "web_search": {
                    "enabled": True,
                    "provider": "api",
                    "max_results": 7,
                    "api": {
                        "endpoint": "https://search.example.test",
                        "results_path": "data.items",
                        "title_path": "name",
                        "url_path": "link",
                    },
                }
            }
        }

        config = ConfigLoader()._dict_to_config(data)

        self.assertTrue(config.tool.web_search.enabled)
        self.assertEqual(config.tool.web_search.provider, "api")
        self.assertEqual(config.tool.web_search.max_results, 7)
        self.assertEqual(config.tool.web_search.api.endpoint, "https://search.example.test")
        self.assertEqual(config.tool.web_search.api.results_path, "data.items")
        self.assertEqual(config.tool.web_search.api.title_path, "name")

    def test_message_builder_adds_web_search_hint_for_freshness_keyword(self):
        registry = ToolRegistry()
        config = WebSearchToolConfig(
            enabled=True,
            trigger_strategy="explicit",
            freshness_keywords=["最新"],
        )
        builder = MessageBuilder(
            system_prompt="system",
            working_memory=_EmptyMemory(),
            semantic_memory=_EmptyMemory(),
            tool_registry=registry,
            tool_enabled=True,
            character_name="Aya",
            web_search_config=config,
            model_config=ModelConfig(web_search_enabled=False),
        )

        messages = builder.build("Python 最新版本是什么？")

        hints = [m["content"] for m in messages if "【联网搜索策略】" in m["content"]]
        self.assertTrue(any("web_search" in hint and "最新" in hint for hint in hints))

    def test_message_builder_skips_own_tool_hint_when_provider_builtin_search_enabled(self):
        registry = ToolRegistry()
        config = WebSearchToolConfig(enabled=True, trigger_strategy="auto")
        builder = MessageBuilder(
            system_prompt="system",
            working_memory=_EmptyMemory(),
            semantic_memory=_EmptyMemory(),
            tool_registry=registry,
            tool_enabled=True,
            web_search_config=config,
            model_config=ModelConfig(web_search_enabled=True, web_search_strategy="auto"),
        )

        messages = builder.build("今天有什么新闻？")

        self.assertEqual(
            sum(1 for m in messages if "请优先调用 web_search 工具" in m["content"]),
            0,
        )
        self.assertIn("Provider 内置联网搜索", messages[0]["content"])

    def test_bing_parser_extracts_web_results_from_generic_links(self):
        provider = BingSearchProvider(WebSearchToolConfig(enabled=True))
        html = """
        <main>
          <section data-result="alpha">
            <a href="https://example.test/a?utm_source=tracker">Result A</a>
            <p>Snippet A describes the first independent search result in plain text.</p>
          </section>
          <section data-result="beta">
            <a href="https://example.test/b">Result B</a>
            <p>Snippet B describes the second independent search result in plain text.</p>
          </section>
          <nav><a href="/settings">Internal navigation</a></nav>
        </main>
        """

        with patch.object(provider, "_fetch", return_value=html):
            result = asyncio.run(provider.search("query", max_results=5))

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.items[0].title, "Result A")
        self.assertEqual(result.items[0].url, "https://example.test/a")
        self.assertIn("Snippet A", result.items[0].snippet)
        self.assertEqual(result.items[0].source, "bing")

    def test_generic_api_provider_maps_json_paths(self):
        config = WebSearchToolConfig(enabled=True, provider="api")
        config.api.endpoint = "https://search.example.test"
        config.api.results_path = "data.items"
        config.api.title_path = "name"
        config.api.url_path = "link"
        config.api.snippet_path = "summary"
        provider = GenericAPISearchProvider(config)

        with patch.object(
            provider,
            "_request_json",
            return_value={
                "data": {
                    "items": [
                        {"name": "Title", "link": "https://example.test", "summary": "Summary"}
                    ]
                }
            },
        ):
            result = asyncio.run(provider.search("query", max_results=3))

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.items[0].title, "Title")
        self.assertEqual(result.items[0].url, "https://example.test")
        self.assertEqual(result.items[0].snippet, "Summary")

    def test_ddg_provider_maps_results(self):
        class _FakeDDGS:
            def __init__(self, *args, **kwargs):
                pass

            def text(self, query, **kwargs):
                return [
                    {
                        "title": "DDG Title",
                        "href": "https://example.test/ddg",
                        "body": "DDG snippet",
                    },
                ]

        with patch("GensokyoAI.tools.web_search.providers.ddg.DDGS", _FakeDDGS):
            provider = DuckDuckGoSearchProvider(WebSearchToolConfig(enabled=True))
            result = asyncio.run(provider.search("query", max_results=3))

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].title, "DDG Title")
        self.assertEqual(result.items[0].url, "https://example.test/ddg")
        self.assertEqual(result.items[0].snippet, "DDG snippet")
        self.assertEqual(result.items[0].source, "ddg")

    def test_service_returns_disabled_when_config_disabled(self):
        service = WebSearchService(WebSearchToolConfig(enabled=False))

        result = asyncio.run(service.search("query"))

        self.assertEqual(result.status, "disabled")
        self.assertIn("config", result.errors)

    def test_service_ranks_deduplicates_and_caches_mixed_results(self):
        config = WebSearchToolConfig(
            enabled=True, provider="mixed", max_results=5, cache_ttl_seconds=60
        )
        service = WebSearchService(config)
        calls = {"count": 0}

        async def fake_search(provider, query, *, max_results=None):
            calls["count"] += 1
            if provider.name == "ddg":
                return ProviderSearchResult(
                    provider="ddg",
                    items=[
                        SearchItem("Brief", "https://example.test/b", "short", "ddg"),
                        SearchItem(
                            "Duplicate", "https://example.test/a?utm_source=x", "duplicate", "ddg"
                        ),
                    ],
                )
            return ProviderSearchResult(
                provider=provider.name,
                items=[
                    SearchItem(
                        "Detailed API Result",
                        "https://example.test/a",
                        "A longer API summary with enough context for ranking.",
                        provider.name,
                    )
                ],
            )

        with (
            patch(
                "GensokyoAI.tools.web_search.providers.ddg.DuckDuckGoSearchProvider.search",
                fake_search,
            ),
            patch(
                "GensokyoAI.tools.web_search.providers.api.GenericAPISearchProvider.search",
                fake_search,
            ),
        ):
            first = asyncio.run(service.search("query", provider="mixed", max_results=5))
            second = asyncio.run(service.search("query", provider="mixed", max_results=5))

        self.assertEqual(first.status, "completed")
        self.assertEqual(len(first.items), 2)
        self.assertEqual(first.items[0].title, "Detailed API Result")
        self.assertEqual(first.items[0].source, "api")
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(calls["count"], 2)

    def test_builtin_tool_executes_with_configured_service(self):
        configure_web_search_tool(ToolConfig(web_search=WebSearchToolConfig(enabled=False)))
        registry = ToolRegistry()
        executor = ToolExecutor(registry)

        result = asyncio.run(
            executor.execute(
                {"id": "call-1", "name": "web_search", "arguments": {"query": "query"}}
            )
        )

        self.assertEqual(result["role"], "tool")
        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "web_search.disabled")
        self.assertEqual(result["error"]["details"]["status"], "disabled")

    def test_injected_service_wins_over_module_global(self):
        # 模块级全局配置为启用，但 executor 注入的服务是禁用的：
        # 执行必须走注入服务（证明多 Actor 各用各的配置，不被全局覆盖）。
        configure_web_search_tool(ToolConfig(web_search=WebSearchToolConfig(enabled=True)))
        registry = ToolRegistry()
        executor = ToolExecutor(
            registry,
            web_search_service=WebSearchService(WebSearchToolConfig(enabled=False)),
        )

        result = asyncio.run(
            executor.execute(
                {"id": "call-1", "name": "web_search", "arguments": {"query": "query"}}
            )
        )

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "web_search.disabled")

    def test_module_global_still_fallback_without_injection(self):
        # 未注入服务时保持旧行为：读模块级兜底配置。
        configure_web_search_tool(ToolConfig(web_search=WebSearchToolConfig(enabled=False)))
        registry = ToolRegistry()
        executor = ToolExecutor(registry)

        result = asyncio.run(
            executor.execute(
                {"id": "call-1", "name": "web_search", "arguments": {"query": "query"}}
            )
        )

        self.assertEqual(result["error"]["error_code"], "web_search.disabled")


if __name__ == "__main__":
    unittest.main()
