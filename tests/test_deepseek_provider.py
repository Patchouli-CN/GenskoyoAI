import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from GensokyoAI.core.agent.providers import ProviderFactory
from GensokyoAI.core.agent.providers.deepseek_provider import DeepSeekProvider
from GensokyoAI.core.config import ModelConfig


class _AsyncStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as error:
            raise StopAsyncIteration from error


def _chunk(delta, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=delta,
                finish_reason=finish_reason,
            )
        ]
    )


class DeepSeekProviderTests(unittest.TestCase):
    def test_provider_is_registered(self):
        self.assertIn("deepseek", ProviderFactory.available_providers())

    def test_chat_stream_yields_reasoning_and_tool_call_message(self):
        with patch.dict(
            sys.modules,
            {"openai": SimpleNamespace(AsyncOpenAI=lambda **kwargs: SimpleNamespace())},
        ):
            provider = DeepSeekProvider(
                ModelConfig(
                    provider="deepseek",
                    name="deepseek-v4-pro",
                    api_key="test-key",
                )
            )
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock()))
        )
        provider._client.chat.completions.create = AsyncMock(
            return_value=_AsyncStream(
                [
                    _chunk(
                        SimpleNamespace(
                            reasoning_content="先思考。",
                            content=None,
                            tool_calls=None,
                        )
                    ),
                    _chunk(
                        SimpleNamespace(
                            reasoning_content=None,
                            content="我查一下。",
                            tool_calls=None,
                        )
                    ),
                    _chunk(
                        SimpleNamespace(
                            reasoning_content=None,
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_1",
                                    function=SimpleNamespace(
                                        name="get_current_time",
                                        arguments="{}",
                                    ),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    ),
                ]
            )
        )

        async def collect():
            chunks = []
            async for chunk in provider.chat_stream(
                model="deepseek-v4-pro",
                messages=[{"role": "user", "content": "几点了"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_current_time",
                            "description": "Get current time",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            ):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(collect())

        self.assertEqual(chunks[0].reasoning_content, "先思考。")
        self.assertEqual(chunks[1].content, "我查一下。")
        self.assertTrue(chunks[2].is_tool_call)
        message = chunks[2].tool_info["message"]
        self.assertEqual(message.content, "我查一下。")
        self.assertEqual(message.reasoning_content, "先思考。")
        self.assertEqual(message.tool_calls[0].id, "call_1")
        self.assertEqual(message.tool_calls[0].function.name, "get_current_time")
        self.assertEqual(message.tool_calls[0].function.arguments, {})

    def test_chat_stream_merges_usage_tail_chunk_into_finish(self):
        """OpenAI 兼容流式：usage 挂在空 choices 的独立末块——必须被捕获并
        合并进 finish chunk，不能被 delta 守卫跳过（成本采样数据源）。"""
        with patch.dict(
            sys.modules,
            {"openai": SimpleNamespace(AsyncOpenAI=lambda **kwargs: SimpleNamespace())},
        ):
            provider = DeepSeekProvider(
                ModelConfig(
                    provider="deepseek",
                    name="deepseek-v4-pro",
                    api_key="test-key",
                )
            )
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock()))
        )
        provider._client.chat.completions.create = AsyncMock(
            return_value=_AsyncStream(
                [
                    _chunk(
                        SimpleNamespace(reasoning_content=None, content="你好", tool_calls=None)
                    ),
                    _chunk(
                        SimpleNamespace(reasoning_content=None, content=None, tool_calls=None),
                        finish_reason="stop",
                    ),
                    SimpleNamespace(
                        choices=[],
                        usage=SimpleNamespace(
                            prompt_tokens=10, completion_tokens=5, total_tokens=15
                        ),
                    ),
                ]
            )
        )

        async def collect():
            chunks = []
            async for chunk in provider.chat_stream(
                model="deepseek-v4-pro",
                messages=[{"role": "user", "content": "hi"}],
            ):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(collect())

        finish_chunks = [chunk for chunk in chunks if chunk.type == "finish"]
        self.assertEqual(len(finish_chunks), 1)  # 合并成一个，不双发
        self.assertEqual(finish_chunks[0].finish_reason, "stop")
        self.assertEqual(
            finish_chunks[0].usage,
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def test_chat_uses_deepseek_thinking_defaults(self):
        with patch.dict(
            sys.modules,
            {"openai": SimpleNamespace(AsyncOpenAI=lambda **kwargs: SimpleNamespace())},
        ):
            provider = DeepSeekProvider(
                ModelConfig(
                    provider="deepseek",
                    name="deepseek-v4-pro",
                    api_key="test-key",
                )
            )
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock()))
        )
        provider._client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content="回答",
                            reasoning_content="思考",
                            tool_calls=None,
                        )
                    )
                ],
                model="deepseek-v4-pro",
            )
        )

        response = asyncio.run(
            provider.chat(
                model="deepseek-v4-pro",
                messages=[{"role": "user", "content": "你好"}],
            )
        )

        kwargs = provider._client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertNotIn("temperature", kwargs)
        self.assertEqual(response.message.reasoning_content, "思考")
        self.assertEqual(response.thinking, "思考")

    def test_chat_can_disable_thinking(self):
        with patch.dict(
            sys.modules,
            {"openai": SimpleNamespace(AsyncOpenAI=lambda **kwargs: SimpleNamespace())},
        ):
            provider = DeepSeekProvider(
                ModelConfig(
                    provider="deepseek",
                    name="deepseek-v4-flash",
                    api_key="test-key",
                    thinking_enabled=False,
                )
            )
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock()))
        )
        provider._client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content="回答",
                            reasoning_content=None,
                            tool_calls=None,
                        )
                    )
                ],
                model="deepseek-v4-flash",
            )
        )

        asyncio.run(
            provider.chat(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": "你好"}],
                options={"temperature": 0.3, "top_p": 0.8},
            )
        )

        kwargs = provider._client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(kwargs["temperature"], 0.3)
        self.assertEqual(kwargs["top_p"], 0.8)

    def test_chat_converts_structured_output_to_json_object(self):
        with patch.dict(
            sys.modules,
            {"openai": SimpleNamespace(AsyncOpenAI=lambda **kwargs: SimpleNamespace())},
        ):
            provider = DeepSeekProvider(
                ModelConfig(
                    provider="deepseek",
                    name="deepseek-v4-pro",
                    api_key="test-key",
                )
            )
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock()))
        )
        provider._client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content='{"ok": true}',
                            reasoning_content=None,
                            tool_calls=None,
                        )
                    )
                ],
                model="deepseek-v4-pro",
            )
        )

        asyncio.run(
            provider.chat(
                model="deepseek-v4-pro",
                messages=[{"role": "user", "content": "请输出 JSON"}],
                options={
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "test_schema",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                                "additionalProperties": False,
                            },
                        },
                    }
                },
            )
        )

        kwargs = provider._client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    def test_provider_declares_structured_output_capability(self):
        with patch.dict(
            sys.modules,
            {"openai": SimpleNamespace(AsyncOpenAI=lambda **kwargs: SimpleNamespace())},
        ):
            provider = DeepSeekProvider(
                ModelConfig(
                    provider="deepseek",
                    name="deepseek-v4-pro",
                    api_key="test-key",
                )
            )

        self.assertTrue(provider.supports("structured_output"))


if __name__ == "__main__":
    unittest.main()
