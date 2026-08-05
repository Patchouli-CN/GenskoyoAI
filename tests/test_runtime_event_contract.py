import asyncio
import unittest
from types import SimpleNamespace

from GensokyoAI.background.manager import BackgroundManager
from GensokyoAI.background.types import BackgroundTask, TaskResult, TaskType
from GensokyoAI.background.workers.base import BaseWorker
from GensokyoAI.core.agent.model_client import ModelClient
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import StreamChunk, UnifiedMessage, UnifiedResponse
from GensokyoAI.core.config import ModelConfig
from GensokyoAI.core.events import SystemEvent
from GensokyoAI.runtime.event_contract import (
    REDACTED_VALUE,
    RUNTIME_EVENT_CONTRACT,
    runtime_event_contract_payload,
    sanitize_event_payload,
)
from GensokyoAI.tools.executor import ToolExecutor
from GensokyoAI.tools.registry import ToolRegistry


class _CollectingEventBus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class _RuntimeContractProvider(BaseProvider):
    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        if kwargs.get("api_key") == "fail":
            error = RuntimeError("bad secret")
            error.status_code = 502
            raise error
        return UnifiedResponse(message=UnifiedMessage(content="answer"), model=model, done=True)

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        yield StreamChunk(content="hello")
        yield StreamChunk(type="finish", finish_reason="stop")


class _IdleWorker(BaseWorker):
    async def process(self, task: BackgroundTask) -> TaskResult:
        return TaskResult(task_id=task.id, success=True, result="ok")


class RuntimeEventContractTests(unittest.TestCase):
    def test_contract_declares_stable_event_names_and_required_fields(self):
        payload = runtime_event_contract_payload()

        self.assertEqual(SystemEvent.MODEL_REQUEST_STARTED.value, "model.request_started")
        self.assertEqual(SystemEvent.MODEL_RETRY_SCHEDULED.value, "model.retry_scheduled")
        self.assertEqual(SystemEvent.MODEL_FIRST_TOKEN.value, "model.first_token")
        self.assertEqual(SystemEvent.MODEL_COMPLETED.value, "model.completed")
        self.assertEqual(SystemEvent.MODEL_FAILED.value, "model.failed")
        self.assertEqual(SystemEvent.TOOL_CALL_PROGRESS.value, "tool.call.progress")
        self.assertEqual(SystemEvent.BACKGROUND_WORKER_STARTED.value, "background.worker.started")
        self.assertEqual(SystemEvent.BACKGROUND_WORKER_IDLE.value, "background.worker.idle")
        self.assertEqual(SystemEvent.BACKGROUND_WORKER_FAILED.value, "background.worker.failed")

        self.assertIn("context", payload["model.request_started"]["required_fields"])
        self.assertIn("provider", payload["model.completed"]["required_fields"])
        self.assertIn("error", payload["model.failed"]["required_fields"])
        self.assertIn("arguments", payload["tool.call.started"]["required_fields"])
        self.assertIn("worker_id", payload["background.worker.started"]["required_fields"])
        self.assertEqual(RUNTIME_EVENT_CONTRACT["model.auth"].event, "model.auth")

    def test_sensitive_event_payload_sanitizer_redacts_nested_secrets(self):
        cleaned = sanitize_event_payload(
            {
                "api_key": "sk-test",
                "nested": {"refresh_token": "rt", "safe": "ok"},
                "items": [{"client_secret": "secret"}],
            }
        )

        self.assertEqual(cleaned["api_key"], REDACTED_VALUE)
        self.assertEqual(cleaned["nested"]["refresh_token"], REDACTED_VALUE)
        self.assertEqual(cleaned["nested"]["safe"], "ok")
        self.assertEqual(cleaned["items"][0]["client_secret"], REDACTED_VALUE)

    def test_sanitizer_keeps_token_metric_fields(self):
        """03#1 回归：契约承诺的计量字段（含 token 字样）不得被脱敏。"""
        cleaned = sanitize_event_payload(
            {
                "first_token_ms": 320,
                "total_tokens": 1500,
                "prompt_tokens": 900,
                "completion_tokens": 600,
                "cost_yuan": 0.012,
                "access_token": "secret-token",  # 真敏感仍要抹
            }
        )
        self.assertEqual(cleaned["first_token_ms"], 320)
        self.assertEqual(cleaned["total_tokens"], 1500)
        self.assertEqual(cleaned["prompt_tokens"], 900)
        self.assertEqual(cleaned["cost_yuan"], 0.012)
        self.assertEqual(cleaned["access_token"], REDACTED_VALUE)

    def test_model_client_publishes_runtime_model_events_and_redacts_retry_payload(self):
        event_bus = _CollectingEventBus()
        client = ModelClient.__new__(ModelClient)
        client.config = ModelConfig(
            provider="test",
            name="contract-model",
            retry_max_attempts=1,
            retry_initial_delay=0,
        )
        client._provider = _RuntimeContractProvider(client.config)
        client._event_bus = event_bus
        client._embedding_config = SimpleNamespace(provider="", name="")
        client._embedding_provider = None

        async def run():
            return await client.chat([{"role": "user", "content": "hi"}], api_key="sk-secret")

        response = asyncio.run(run())
        self.assertEqual(response.message.content, "answer")

        event_types = [event.type for event in event_bus.events]
        self.assertIn(SystemEvent.MODEL_REQUEST_STARTED, event_types)
        self.assertIn(SystemEvent.MODEL_FIRST_TOKEN, event_types)
        self.assertIn(SystemEvent.MODEL_COMPLETED, event_types)
        self.assertNotIn(SystemEvent.MODEL_FAILED, event_types)

        started = next(
            event for event in event_bus.events if event.type == SystemEvent.MODEL_REQUEST_STARTED
        )
        self.assertEqual(started.data["context"], "chat")
        self.assertEqual(started.data["provider"], "test")

    def test_tool_executor_progress_and_arguments_are_sanitized(self):
        event_bus = _CollectingEventBus()
        executor = ToolExecutor(ToolRegistry(), event_bus=event_bus)

        executor.publish_progress(
            "web_search",
            "fetching",
            message="fetching data",
            details={"api_key": "secret"},
        )
        executor._publish_tool_event("started", "web_search", {"api_key": "secret", "query": "q"})

        progress = event_bus.events[0]
        started = event_bus.events[1]
        self.assertEqual(progress.type, SystemEvent.TOOL_CALL_PROGRESS)
        self.assertEqual(progress.data["arguments"]["details"]["api_key"], REDACTED_VALUE)
        self.assertEqual(started.type, SystemEvent.TOOL_CALL_STARTED)
        self.assertEqual(started.data["arguments"]["api_key"], REDACTED_VALUE)
        self.assertEqual(started.data["arguments"]["query"], "q")

    def test_background_manager_publishes_worker_events(self):
        event_bus = _CollectingEventBus()
        manager = BackgroundManager(max_workers=1, event_bus=event_bus)
        manager.register_worker(TaskType.CUSTOM, _IdleWorker())

        async def run():
            await manager.start()
            manager.submit(BackgroundTask(type=TaskType.CUSTOM, name="contract_task"))
            await asyncio.sleep(0.05)
            await manager.stop(wait=True)

        asyncio.run(run())

        event_types = [event.type for event in event_bus.events]
        self.assertIn(SystemEvent.BACKGROUND_WORKER_STARTED, event_types)
        self.assertIn(SystemEvent.BACKGROUND_WORKER_IDLE, event_types)
        idle = next(
            event for event in event_bus.events if event.type == SystemEvent.BACKGROUND_WORKER_IDLE
        )
        self.assertEqual(idle.data["worker_id"], 0)


if __name__ == "__main__":
    unittest.main()
