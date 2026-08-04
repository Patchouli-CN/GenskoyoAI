import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, cast

from GensokyoAI.core.agent.model_client import ModelClient
from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import ProviderCapability, UnifiedEmbeddingResponse
from GensokyoAI.core.config import AuthConfig, EmbeddingConfig, ModelConfig, ResourceControlConfig
from GensokyoAI.core.events import SystemEvent
from GensokyoAI.core.exceptions import ModelError
from GensokyoAI.runtime.resource_control import build_resource_gates


class _HTTPError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, response_body: str):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class RetryableProvider(BaseProvider):
    calls = 0

    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        self.__class__.calls += 1
        if self.__class__.calls == 1:
            raise _HTTPError(
                "server failed",
                status_code=502,
                response_body="<!doctype html><html>bad gateway</html>",
            )
        return SimpleNamespace(
            message=SimpleNamespace(content="ok"),
            model=model,
            done=True,
        )

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        if False:
            yield None


class NonRetryableProvider(BaseProvider):
    calls = 0

    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        self.__class__.calls += 1
        raise _HTTPError("bad request", status_code=400, response_body="bad params")

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        if False:
            yield None


class Retryable429Provider(BaseProvider):
    calls = 0

    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        self.__class__.calls += 1
        if self.__class__.calls == 1:
            raise _HTTPError("rate limited", status_code=429, response_body="retry later")
        return SimpleNamespace(
            message=SimpleNamespace(content="ok429"),
            model=model,
            done=True,
        )

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        if False:
            yield None


class RetryableEmbeddingProvider(BaseProvider):
    calls = 0

    @property
    def capabilities(self) -> set[str]:
        return super().capabilities | {ProviderCapability.EMBEDDINGS}

    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        raise NotImplementedError

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        if False:
            yield None

    async def embeddings(self, model: str, prompt: str, **kwargs):
        self.__class__.calls += 1
        if self.__class__.calls == 1:
            raise _HTTPError(
                "embedding server failed",
                status_code=502,
                response_body="<!doctype html><html>bad gateway</html>",
            )
        return UnifiedEmbeddingResponse(embedding=[1.0, 2.0], model=model)


class FailingEmbeddingProvider(BaseProvider):
    @property
    def capabilities(self) -> set[str]:
        return super().capabilities | {ProviderCapability.EMBEDDINGS}

    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        raise NotImplementedError

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        if False:
            yield None

    async def embeddings(self, model: str, prompt: str, **kwargs):
        raise _HTTPError(
            "bad embedding request",
            status_code=400,
            response_body="bad embedding params",
        )


class OAuth401Provider(BaseProvider):
    calls = 0
    refreshed = False

    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        self.__class__.calls += 1
        if not self.__class__.refreshed:
            raise _HTTPError("unauthorized", status_code=401, response_body="expired")
        return SimpleNamespace(message=SimpleNamespace(content="oauth ok"), model=model, done=True)

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        if False:
            yield None

    async def prepare_auth(self, *, force_refresh: bool = False) -> None:
        if force_refresh:
            self.__class__.refreshed = True
            return
        await super().prepare_auth(force_refresh=force_refresh)


class _CollectingEventBus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class ModelClientRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("retryable_test", RetryableProvider)
        ProviderFactory.register("non_retryable_test", NonRetryableProvider)
        ProviderFactory.register("retryable_429_test", Retryable429Provider)
        ProviderFactory.register("retryable_embedding_test", RetryableEmbeddingProvider)
        ProviderFactory.register("failing_embedding_test", FailingEmbeddingProvider)
        ProviderFactory.register("oauth_401_test", OAuth401Provider)

    def test_retries_5xx_and_succeeds(self):
        RetryableProvider.calls = 0
        client = ModelClient(
            ModelConfig(
                provider="retryable_test",
                name="test-model",
                retry_max_attempts=2,
                retry_initial_delay=0,
            )
        )

        response = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

        self.assertEqual(response.message.content, "ok")
        self.assertEqual(RetryableProvider.calls, 2)
        # 延迟统计：一次成功调用产生一个样本
        stats = client.latency_stats()
        self.assertEqual(stats["count"], 1)
        self.assertGreaterEqual(stats["avg_ms"], 0)

    def test_latency_stats_empty_and_rolling(self):
        client = ModelClient(
            ModelConfig(provider="retryable_test", name="test-model", retry_max_attempts=1)
        )
        self.assertEqual(client.latency_stats(), {"count": 0})
        for context, ms in (
            ("think_engine", 100.0),
            ("chat", 900.0),
            ("think_engine", 300.0),
            ("think_engine", 200.0),
        ):
            client._latency_samples.append((context, ms))
        # 全量统计
        stats = client.latency_stats()
        self.assertEqual(stats["count"], 4)
        self.assertEqual(stats["max_ms"], 900.0)
        # 按调用方过滤：think_engine 中位数 = 200.0
        think_stats = client.latency_stats(context="think_engine")
        self.assertEqual(think_stats["count"], 3)
        self.assertEqual(think_stats["median_ms"], 200.0)
        self.assertEqual(think_stats["max_ms"], 300.0)
        # 偶数样本中位数取中间两值均值
        client._latency_samples.append(("think_engine", 400.0))
        even_stats = client.latency_stats(context="think_engine")
        self.assertEqual(even_stats["median_ms"], 250.0)

    def test_does_not_retry_400_and_sanitizes_error(self):
        NonRetryableProvider.calls = 0
        client = ModelClient(
            ModelConfig(
                provider="non_retryable_test",
                name="test-model",
                retry_max_attempts=3,
                retry_initial_delay=0,
            )
        )

        with self.assertRaises(ModelError) as ctx:
            asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

        self.assertEqual(NonRetryableProvider.calls, 1)
        self.assertIn("API 状态码 400", str(ctx.exception))
        self.assertIn("bad params", str(ctx.exception))

    def test_custom_retry_status_codes_can_retry_429(self):
        Retryable429Provider.calls = 0
        client = ModelClient(
            ModelConfig(
                provider="retryable_429_test",
                name="test-model",
                retry_max_attempts=2,
                retry_initial_delay=0,
                retry_status_codes=[429],
            )
        )

        response = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

        self.assertEqual(response.message.content, "ok429")
        self.assertEqual(Retryable429Provider.calls, 2)

    def test_embeddings_retries_5xx_and_succeeds(self):
        RetryableEmbeddingProvider.calls = 0
        client = ModelClient(
            ModelConfig(
                provider="retryable_embedding_test",
                name="chat-model",
                retry_max_attempts=2,
                retry_initial_delay=0,
            ),
            embedding_config=EmbeddingConfig(
                provider="retryable_embedding_test", name="embed-model"
            ),
        )

        response = asyncio.run(client.embeddings("hello"))

        self.assertEqual(response.embedding, [1.0, 2.0])
        self.assertEqual(RetryableEmbeddingProvider.calls, 2)

    def test_embeddings_failure_publishes_structured_error_event(self):
        event_bus = _CollectingEventBus()
        client = ModelClient(
            ModelConfig(
                provider="failing_embedding_test",
                name="chat-model",
                retry_max_attempts=2,
                retry_initial_delay=0,
            ),
            event_bus=cast("Any", event_bus),
            embedding_config=EmbeddingConfig(provider="failing_embedding_test", name="embed-model"),
        )

        with self.assertRaises(ModelError):
            asyncio.run(client.embeddings("hello"))

        self.assertTrue(event_bus.events)
        data = event_bus.events[-1].data
        self.assertEqual(data["context"], "embeddings")
        self.assertEqual(data["status_code"], 400)
        self.assertEqual(data["prompt_length"], 5)
        self.assertEqual(data["provider"], "failing_embedding_test")
        self.assertEqual(data["model"], "embed-model")

    def test_oauth_401_refreshes_once_and_retries(self):
        OAuth401Provider.calls = 0
        OAuth401Provider.refreshed = False
        event_bus = _CollectingEventBus()
        client = ModelClient(
            ModelConfig(
                provider="oauth_401_test",
                name="test-model",
                retry_max_attempts=1,
                retry_initial_delay=0,
                auth=AuthConfig(auth_type="bearer", access_token="old-token"),
            ),
            event_bus=cast("Any", event_bus),
        )

        response = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

        self.assertEqual(response.message.content, "oauth ok")
        self.assertEqual(OAuth401Provider.calls, 2)
        auth_events = [e for e in event_bus.events if e.type == SystemEvent.MODEL_AUTH]
        self.assertTrue(auth_events)
        self.assertIn("token_refresh_completed", [e.data["status"] for e in auth_events])

    def test_provider_gate_rejects_concurrent_chat_and_releases(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingProvider(BaseProvider):
            async def chat(
                self, model: str, messages: list[dict], tools=None, options=None, **kwargs
            ):
                started.set()
                await release.wait()
                return SimpleNamespace(
                    message=SimpleNamespace(content="blocked"), model=model, done=True
                )

            async def chat_stream(
                self, model: str, messages: list[dict], tools=None, options=None, **kwargs
            ):
                if False:
                    yield None

        if "blocking_resource_test" not in ProviderFactory.available_providers():
            ProviderFactory.register("blocking_resource_test", BlockingProvider)
        gates = build_resource_gates(
            ResourceControlConfig(
                provider_max_concurrent=1,
                model_max_concurrent=1,
                runtime_queue_size=0,
                acquire_timeout_seconds=0,
            )
        )
        client = ModelClient(
            ModelConfig(provider="blocking_resource_test", name="test-model"),
            resource_gates=gates,
        )

        async def run():
            first = asyncio.create_task(client.chat([{"role": "user", "content": "one"}]))
            await started.wait()
            with self.assertRaises(ModelError) as ctx:
                await client.chat([{"role": "user", "content": "two"}])
            release.set()
            await first
            return ctx.exception

        error = asyncio.run(run())

        self.assertEqual(getattr(error, "error_code", None), "resource.limit_exceeded")
        self.assertEqual(getattr(error, "details", {}).get("resource"), "provider")
        self.assertEqual(gates["provider"].active, 0)
        self.assertEqual(gates["model"].active, 0)


if __name__ == "__main__":
    unittest.main()
