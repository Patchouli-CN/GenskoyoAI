import asyncio
import unittest

from GensokyoAI.core.agent.model_client import ModelClient
from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.base import BaseProvider
from GensokyoAI.core.agent.types import UnifiedEmbeddingResponse
from GensokyoAI.core.config import EmbeddingConfig, ModelConfig
from GensokyoAI.core.exceptions import ModelError


class DummyChatProvider(BaseProvider):
    async def chat(self, model: str, messages: list[dict], tools=None, options=None, **kwargs):
        raise NotImplementedError

    async def chat_stream(
        self, model: str, messages: list[dict], tools=None, options=None, **kwargs
    ):
        if False:
            yield None


class DummyEmbeddingProvider(DummyChatProvider):
    calls: list[dict] = []

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self.__class__.calls.append({"event": "init", "config": config})

    async def embeddings(self, model: str, prompt: str, **kwargs) -> UnifiedEmbeddingResponse:
        self.__class__.calls.append(
            {
                "event": "embeddings",
                "model": model,
                "prompt": prompt,
                "kwargs": kwargs,
                "config": self.config,
            }
        )
        return UnifiedEmbeddingResponse(embedding=[0.1, 0.2, 0.3], model=model)


class ModelClientEmbeddingRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ProviderFactory.register("dummy_chat", DummyChatProvider)
        ProviderFactory.register("dummy_embed", DummyEmbeddingProvider)

    def setUp(self):
        DummyEmbeddingProvider.calls.clear()

    def test_embeddings_require_explicit_embedding_model(self):
        config = ModelConfig(provider="dummy_chat", name="chat-model")
        client = ModelClient(config, embedding_config=EmbeddingConfig(provider="dummy_embed"))

        with self.assertRaises(ModelError) as ctx:
            asyncio.run(client.embeddings("hello"))

        self.assertIn("embedding.name", str(ctx.exception))
        self.assertEqual(DummyEmbeddingProvider.calls, [])

    def test_embeddings_use_independent_provider_model_and_options(self):
        config = ModelConfig(
            provider="dummy_chat",
            name="chat-model",
            base_url="https://chat.example/v1",
            api_key="chat-key",
            timeout=60,
        )
        embedding_config = EmbeddingConfig(
            provider="dummy_embed",
            name="embed-model",
            base_url="https://embed.example/v1",
            api_key="embed-key",
            dimensions=256,
            encoding_format="float",
            timeout=7,
        )
        client = ModelClient(config, embedding_config=embedding_config)

        response = asyncio.run(client.embeddings("hello"))

        self.assertEqual(response.embedding, [0.1, 0.2, 0.3])
        init_call = DummyEmbeddingProvider.calls[0]
        self.assertEqual(init_call["event"], "init")
        self.assertEqual(init_call["config"].provider, "dummy_embed")
        self.assertEqual(init_call["config"].name, "embed-model")
        self.assertEqual(init_call["config"].base_url, "https://embed.example/v1")
        self.assertEqual(init_call["config"].api_key, "embed-key")
        self.assertEqual(init_call["config"].timeout, 7)

        embedding_call = DummyEmbeddingProvider.calls[1]
        self.assertEqual(embedding_call["event"], "embeddings")
        self.assertEqual(embedding_call["model"], "embed-model")
        self.assertEqual(embedding_call["prompt"], "hello")
        self.assertEqual(
            embedding_call["kwargs"],
            {"dimensions": 256, "encoding_format": "float"},
        )

    def test_call_kwargs_override_embedding_config_options(self):
        config = ModelConfig(provider="dummy_chat", name="chat-model")
        embedding_config = EmbeddingConfig(
            provider="dummy_embed",
            name="embed-model",
            dimensions=256,
            encoding_format="float",
        )
        client = ModelClient(config, embedding_config=embedding_config)

        asyncio.run(client.embeddings("hello", dimensions=512, encoding_format="base64"))

        embedding_call = DummyEmbeddingProvider.calls[1]
        self.assertEqual(
            embedding_call["kwargs"],
            {"dimensions": 512, "encoding_format": "base64"},
        )

    def test_supports_embeddings_uses_embedding_provider(self):
        config = ModelConfig(provider="dummy_chat", name="chat-model")
        client = ModelClient(
            config,
            embedding_config=EmbeddingConfig(provider="dummy_embed", name="embed-model"),
        )

        self.assertTrue(client.supports_embeddings)

        missing_config = ModelConfig(provider="dummy_chat", name="chat-model")
        missing_client = ModelClient(
            missing_config,
            embedding_config=EmbeddingConfig(provider="dummy_embed"),
        )
        self.assertFalse(missing_client.supports_embeddings)

    def test_embeddings_batch_falls_back_to_single_when_batch_unsupported(self):
        """Provider 未实现原生 batch（抛 NotImplementedError）时回退单条并发，
        而不是被 _call_with_retry 包成 ModelAPIError 吞掉（04#3）。"""
        config = ModelConfig(provider="dummy_chat", name="chat-model")
        client = ModelClient(
            config,
            embedding_config=EmbeddingConfig(provider="dummy_embed", name="embed-model"),
        )

        embeddings = asyncio.run(client.embeddings_batch(["你好", "再见"]))

        self.assertEqual(len(embeddings), 2)
        self.assertEqual([list(e) for e in embeddings], [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]])
        # 回退路径逐条走了 embeddings()（batch 未实现，无原生调用）
        self.assertIn("embeddings", [call["event"] for call in DummyEmbeddingProvider.calls])


if __name__ == "__main__":
    unittest.main()
