import asyncio
import unittest

from GensokyoAI.core.agent.providers.claude_provider import ClaudeProvider
from GensokyoAI.core.agent.response_handler import ResponseHandler
from GensokyoAI.core.config import ResourceControlConfig, ToolConfig, WebSearchToolConfig
from GensokyoAI.core.events import EventBus, SystemEvent
from GensokyoAI.runtime.resource_control import build_resource_gates
from GensokyoAI.runtime.rpc import RpcError, runtime_error_response
from GensokyoAI.tools.base import tool
from GensokyoAI.tools.errors import ToolError, ToolExecutionError
from GensokyoAI.tools.executor import ToolExecutor
from GensokyoAI.tools.registry import ToolRegistry
from GensokyoAI.tools.tool_builtin import web_search as web_search_module
from GensokyoAI.tools.tool_builtin.web_search import configure_web_search_tool
from GensokyoAI.tools.web_search.types import WebSearchResult


@tool(name="p9_failing_tool", description="P9 failing test tool")
def p9_failing_tool() -> str:
    raise ToolExecutionError(
        ToolError(
            error_code="test.structured_failure",
            technical_message="technical failure",
            user_message="user failure",
            recoverable=False,
            action_hint="fix test",
            details={"scope": "unit"},
        )
    )


@tool(name="p9_generic_failing_tool", description="P9 generic failing test tool")
def p9_generic_failing_tool() -> str:
    raise RuntimeError("boom")


class ToolErrorContractTests(unittest.TestCase):
    def test_tool_executor_returns_structured_error_for_missing_tool_with_legacy_fields(self):
        executor = ToolExecutor(ToolRegistry())

        result = asyncio.run(
            executor.execute({"id": "call-1", "name": "missing_tool", "arguments": {}})
        )

        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "call-1")
        self.assertTrue(result["is_error"])
        self.assertTrue(result["content"].startswith("调用出错啦:"))
        self.assertEqual(result["error"]["error_code"], "tool.not_found")
        self.assertTrue(result["error"]["recoverable"])
        self.assertIn("action_hint", result["error"])

    def test_tool_executor_preserves_structured_tool_execution_error(self):
        executor = ToolExecutor(ToolRegistry())

        result = executor.execute_sync({"id": "call-2", "name": "p9_failing_tool", "arguments": {}})

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "test.structured_failure")
        self.assertEqual(result["error"]["technical_message"], "technical failure")
        self.assertEqual(result["error"]["user_message"], "user failure")
        self.assertFalse(result["error"]["recoverable"])
        self.assertEqual(result["error"]["details"], {"scope": "unit"})

    def test_tool_executor_wraps_generic_exception_as_structured_error(self):
        executor = ToolExecutor(ToolRegistry())

        result = executor.execute_sync(
            {"id": "call-3", "name": "p9_generic_failing_tool", "arguments": {}}
        )

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "tool.execution_failed")
        self.assertIn("boom", result["error"]["technical_message"])
        self.assertEqual(result["error"]["details"]["exception_type"], "RuntimeError")

    def test_tool_failed_event_contains_structured_error_fields(self):
        event_bus = EventBus(enable_trace=False)
        events = []
        event_bus.subscribe(SystemEvent.TOOL_CALL_FAILED, lambda event: events.append(event))
        executor = ToolExecutor(ToolRegistry(), event_bus=event_bus)

        async def run_and_flush():
            await executor.execute({"id": "call-4", "name": "p9_failing_tool", "arguments": {}})
            while not event_bus._event_queue.empty():
                event = await event_bus._event_queue.get()
                await event_bus._process_event(event)
                event_bus._event_queue.task_done()

        asyncio.run(run_and_flush())

        self.assertEqual(len(events), 1)
        data = events[0].data
        self.assertEqual(data["error"], "technical failure")
        self.assertEqual(data["error_code"], "test.structured_failure")
        self.assertEqual(data["user_message"], "user failure")
        self.assertFalse(data["recoverable"])
        self.assertEqual(data["action_hint"], "fix test")
        self.assertEqual(data["details"], {"scope": "unit"})

    def test_tool_gate_rejects_concurrent_tool_and_releases(self):
        started = asyncio.Event()
        release = asyncio.Event()

        @tool(name="p2_blocking_tool", description="P2 blocking test tool")
        async def p2_blocking_tool() -> str:
            started.set()
            await release.wait()
            return "blocked"

        gates = build_resource_gates(
            ResourceControlConfig(
                tool_max_concurrent=1,
                runtime_queue_size=0,
                acquire_timeout_seconds=0,
            )
        )
        executor = ToolExecutor(ToolRegistry(), resource_gates=gates)

        async def run():
            first = asyncio.create_task(
                executor.execute({"id": "p2-1", "name": "p2_blocking_tool", "arguments": {}})
            )
            await started.wait()
            second = await executor.execute(
                {"id": "p2-2", "name": "p2_blocking_tool", "arguments": {}}
            )
            release.set()
            first_result = await first
            return first_result, second

        first_result, second = asyncio.run(run())

        self.assertEqual(first_result["content"], "blocked")
        self.assertTrue(second["is_error"])
        self.assertEqual(second["error"]["error_code"], "resource.limit_exceeded")
        self.assertEqual(second["error"]["details"]["resource"], "tool")
        self.assertEqual(gates["tool"].active, 0)

    def test_web_search_disabled_maps_to_stable_error_code(self):
        configure_web_search_tool(ToolConfig(web_search=WebSearchToolConfig(enabled=False)))
        executor = ToolExecutor(ToolRegistry())

        result = asyncio.run(
            executor.execute(
                {"id": "call-5", "name": "web_search", "arguments": {"query": "query"}}
            )
        )

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "web_search.disabled")
        self.assertEqual(result["error"]["details"]["status"], "disabled")
        self.assertIn("tool.web_search.enabled", result["error"]["action_hint"])

    def test_web_search_unsupported_provider_maps_to_stable_error_code(self):
        configure_web_search_tool(
            ToolConfig(web_search=WebSearchToolConfig(enabled=True, provider="unknown"))
        )
        executor = ToolExecutor(ToolRegistry())

        result = asyncio.run(
            executor.execute(
                {"id": "call-6", "name": "web_search", "arguments": {"query": "query"}}
            )
        )

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "web_search.unsupported_provider")
        self.assertEqual(result["error"]["details"]["status"], "failed")

    def test_web_search_provider_failure_maps_to_stable_error_code(self):
        class FailingSearchService:
            async def search(self, query, *, max_results=None, provider=None):
                return WebSearchResult(
                    query=query,
                    provider=provider or "api",
                    status="failed",
                    provider_status={"api": "failed"},
                    fallback_reason="api: missing API key",
                    errors={"api": "missing API key"},
                )

        original_service = web_search_module._service
        web_search_module._service = FailingSearchService()
        try:
            executor = ToolExecutor(ToolRegistry())
            result = asyncio.run(
                executor.execute(
                    {"id": "call-7", "name": "web_search", "arguments": {"query": "query"}}
                )
            )
        finally:
            web_search_module._service = original_service

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "web_search.provider_failed")
        self.assertEqual(result["error"]["details"]["status"], "failed")
        self.assertEqual(result["error"]["details"]["errors"], {"api": "missing API key"})
        self.assertIn("API key", result["error"]["action_hint"])

    def test_web_search_no_results_maps_to_stable_error_code(self):
        class NoResultsSearchService:
            async def search(self, query, *, max_results=None, provider=None):
                return WebSearchResult(
                    query=query,
                    provider=provider or "api",
                    status="failed",
                    provider_status={"api": "completed"},
                    errors={},
                )

        original_service = web_search_module._service
        web_search_module._service = NoResultsSearchService()
        try:
            executor = ToolExecutor(ToolRegistry())
            result = asyncio.run(
                executor.execute(
                    {"id": "call-8", "name": "web_search", "arguments": {"query": "query"}}
                )
            )
        finally:
            web_search_module._service = original_service

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "web_search.no_results")
        self.assertEqual(result["error"]["details"]["result_count"], 0)
        self.assertIn("查询词", result["error"]["action_hint"])

    def test_web_search_unexpected_exception_maps_to_stable_error_code(self):
        class BrokenSearchService:
            async def search(self, query, *, max_results=None, provider=None):
                raise ValueError("invalid provider payload")

        original_service = web_search_module._service
        web_search_module._service = BrokenSearchService()
        try:
            executor = ToolExecutor(ToolRegistry())
            result = asyncio.run(
                executor.execute(
                    {"id": "call-9", "name": "web_search", "arguments": {"query": "query"}}
                )
            )
        finally:
            web_search_module._service = original_service

        self.assertTrue(result["is_error"])
        self.assertEqual(result["error"]["error_code"], "web_search.unexpected_error")
        self.assertIn("invalid provider payload", result["error"]["technical_message"])
        self.assertEqual(result["error"]["details"]["exception_type"], "ValueError")

    def test_claude_tool_result_conversion_keeps_is_error_compatibility(self):
        result = ClaudeProvider._convert_tool_result_block(
            {
                "role": "tool",
                "tool_call_id": "toolu_1",
                "content": "错误: technical failure",
                "is_error": True,
                "error": {"error_code": "test.structured_failure"},
            }
        )

        self.assertTrue(result["is_error"])
        self.assertEqual(result["tool_use_id"], "toolu_1")
        self.assertIn("technical failure", result["content"])

    def test_tool_error_result_can_be_converted_to_stream_error_chunk(self):
        chunk = ResponseHandler._tool_error_chunk(
            {
                "role": "tool",
                "tool_call_id": "call-10",
                "name": "p9_failing_tool",
                "content": "错误: technical failure",
                "is_error": True,
                "error": {
                    "error_code": "test.structured_failure",
                    "technical_message": "technical failure",
                    "user_message": "user failure",
                    "recoverable": False,
                    "action_hint": "fix test",
                    "details": {"scope": "stream"},
                },
            }
        )

        self.assertIsNotNone(chunk)
        assert chunk is not None
        self.assertEqual(chunk.type, "tool_error")
        self.assertEqual(chunk.status, "failed")
        self.assertEqual(chunk.error_code, "test.structured_failure")
        self.assertEqual(chunk.error, "technical failure")
        self.assertIsNotNone(chunk.error_details)
        assert chunk.error_details is not None
        self.assertEqual(chunk.error_details["tool_call_id"], "call-10")
        self.assertEqual(chunk.error_details["details"], {"scope": "stream"})
        self.assertFalse(chunk.error_details["recoverable"])

    def test_runtime_error_response_contract_keeps_legacy_and_structured_fields(self):
        response = runtime_error_response(
            RpcError(
                "technical runtime failure",
                code="runtime.test_failure",
                user_message="用户可读错误",
                recoverable=False,
                action_hint="检查测试配置",
                details={"scope": "runtime"},
            )
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "technical runtime failure")
        self.assertEqual(response["error_code"], "runtime.test_failure")
        self.assertEqual(response["error_object"]["code"], "runtime.test_failure")
        self.assertEqual(response["error_object"]["message"], "用户可读错误")
        self.assertEqual(response["error_object"]["technical_message"], "technical runtime failure")
        self.assertEqual(response["error_object"]["user_message"], "用户可读错误")
        self.assertFalse(response["error_object"]["recoverable"])
        self.assertEqual(response["error_object"]["action_hint"], "检查测试配置")
        self.assertEqual(response["error_object"]["details"], {"scope": "runtime"})


class ToolErrorModelMessageTests(unittest.TestCase):
    """给模型的错误 content 用干净 model_message，不泄漏原始诊断（ddg 修复）。"""

    def test_error_result_uses_model_message_for_content(self):
        error = ToolError(
            error_code="web_search.provider_failed",
            technical_message="ddg: error sending request for url (https://www.startpage.com/)",
            user_message="联网搜索 Provider 执行失败。",
            model_message="搜索服务暂时不可用，稍后再试。",
        )
        result = ToolExecutor._error_result(
            {"id": "call-x"}, "web_search", error, legacy_prefix="错误"
        )
        self.assertEqual(result["content"], "错误: 搜索服务暂时不可用，稍后再试。")
        self.assertNotIn("startpage", result["content"])  # 原始 URL 不泄漏给模型
        # 原始诊断仍保留在 error.technical_message 供日志/事件观测
        self.assertIn("startpage", result["error"]["technical_message"])
        self.assertEqual(result["error"]["model_message"], "搜索服务暂时不可用，稍后再试。")

    def test_error_result_falls_back_to_user_message(self):
        error = ToolError(
            error_code="tool.generic",
            technical_message="raw technical detail",
            user_message="给用户看的干净信息",
        )
        result = ToolExecutor._error_result(
            {"id": "call-y"}, "generic_tool", error, legacy_prefix="错误"
        )
        self.assertEqual(result["content"], "错误: 给用户看的干净信息")
        self.assertNotIn("raw technical", result["content"])
        self.assertNotIn("model_message", result["error"])  # None 时 to_dict 省略

    def test_web_search_provider_failed_sets_clean_model_message(self):
        from GensokyoAI.tools.tool_builtin.web_search import _result_to_tool_error

        result = WebSearchResult(
            query="test",
            provider="mixed",
            status="failed",
            fallback_reason="ddg: ddg: error sending request for url "
            "(https://www.startpage.com/) > operation timed out",
            errors={"ddg": "ddg: error sending request for url "
            "(https://www.startpage.com/) > operation timed out"},
        )
        error = _result_to_tool_error(result)
        self.assertEqual(error.model_message, "搜索服务暂时不可用，稍后再试。")
        self.assertIn("startpage", error.technical_message)  # 诊断保留
        self.assertNotIn("startpage", error.model_message)  # 模型面干净


if __name__ == "__main__":
    unittest.main()
