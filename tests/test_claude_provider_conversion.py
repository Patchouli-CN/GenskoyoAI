import asyncio
import unittest
from types import SimpleNamespace

from GensokyoAI.core.agent.providers.claude_provider import ClaudeProvider
from GensokyoAI.core.agent.types import ToolCall, ToolCallFunction


class ClaudeProviderConversionTests(unittest.TestCase):
    def test_system_messages_are_extracted_and_tool_results_are_grouped(self):
        messages = [
            {"role": "system", "content": "system A"},
            {"role": "user", "content": "需要查时间和月相"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {
                            "name": "get_current_time",
                            "arguments": {"timezone": "Asia/Shanghai"},
                        },
                    },
                    {
                        "id": "toolu_2",
                        "type": "function",
                        "function": {
                            "name": "get_moon_phase",
                            "arguments": {},
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "10:30"},
            {"role": "tool", "tool_call_id": "toolu_2", "content": "满月"},
        ]

        system, claude_messages = ClaudeProvider._convert_messages_to_claude(messages)

        self.assertEqual(system, "system A")
        self.assertEqual(claude_messages[0], {"role": "user", "content": "需要查时间和月相"})
        self.assertEqual(claude_messages[1]["role"], "assistant")
        self.assertEqual(claude_messages[1]["content"][0]["type"], "tool_use")
        self.assertEqual(claude_messages[1]["content"][0]["id"], "toolu_1")
        self.assertEqual(claude_messages[1]["content"][1]["id"], "toolu_2")
        self.assertEqual(claude_messages[2]["role"], "user")
        self.assertEqual(
            [block["type"] for block in claude_messages[2]["content"]],
            ["tool_result", "tool_result"],
        )
        self.assertEqual(claude_messages[2]["content"][0]["tool_use_id"], "toolu_1")
        self.assertEqual(claude_messages[2]["content"][1]["tool_use_id"], "toolu_2")

    def test_toolcall_object_converts_to_claude_tool_use(self):
        messages = [
            {
                "role": "assistant",
                "content": "我来查一下。",
                "tool_calls": [
                    ToolCall(
                        id="toolu_obj",
                        provider="claude",
                        function=ToolCallFunction(
                            name="get_current_time",
                            arguments={"timezone": "UTC"},
                            provider="claude",
                        ),
                    )
                ],
            }
        ]

        _system, claude_messages = ClaudeProvider._convert_messages_to_claude(messages)

        self.assertEqual(claude_messages[0]["role"], "assistant")
        self.assertEqual(claude_messages[0]["content"][0], {"type": "text", "text": "我来查一下。"})
        self.assertEqual(
            claude_messages[0]["content"][1],
            {
                "type": "tool_use",
                "id": "toolu_obj",
                "name": "get_current_time",
                "input": {"timezone": "UTC"},
            },
        )

    def test_empty_assistant_message_without_tool_calls_is_skipped(self):
        """04#12：空 content 且无 tool_calls 的 assistant 消息跳过，不发 content='' 给 Anthropic。"""
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "还在吗"},
        ]

        _system, claude_messages = ClaudeProvider._convert_messages_to_claude(messages)

        self.assertNotIn(
            {"role": "assistant", "content": ""}, claude_messages
        )  # 退化 assistant 已被过滤
        self.assertEqual(
            [m["role"] for m in claude_messages], ["user", "user"]
        )  # 剩余 user 消息保留

    def test_convert_response_preserves_tool_use_id_and_arguments(self):
        response = SimpleNamespace(
            model="claude-test",
            content=[
                SimpleNamespace(type="text", text="我需要调用工具。"),
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_resp",
                    name="get_current_time",
                    input={"timezone": "Asia/Shanghai"},
                ),
            ],
        )

        converted = ClaudeProvider._convert_response(
            ClaudeProvider.__new__(ClaudeProvider), response
        )

        self.assertEqual(converted.message.content, "我需要调用工具。")
        self.assertEqual(converted.message.tool_calls[0].id, "toolu_resp")
        self.assertEqual(converted.message.tool_calls[0].provider, "claude")
        self.assertEqual(
            converted.message.tool_calls[0].function.arguments, {"timezone": "Asia/Shanghai"}
        )

    def test_convert_response_maps_usage(self):
        response = SimpleNamespace(
            model="claude-test",
            content=[SimpleNamespace(type="text", text="你好。")],
            usage=SimpleNamespace(
                input_tokens=120,
                output_tokens=42,
                cache_read_input_tokens=3000,
                cache_creation_input_tokens=800,
            ),
        )

        converted = ClaudeProvider._convert_response(
            ClaudeProvider.__new__(ClaudeProvider), response
        )

        self.assertEqual(
            converted.usage,
            {
                "input_tokens": 120,
                "output_tokens": 42,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 800,
            },
        )

    def test_convert_response_without_usage_keeps_none(self):
        response = SimpleNamespace(
            model="claude-test",
            content=[SimpleNamespace(type="text", text="你好。")],
        )

        converted = ClaudeProvider._convert_response(
            ClaudeProvider.__new__(ClaudeProvider), response
        )

        self.assertIsNone(converted.usage)

    def test_stream_finish_chunk_carries_usage(self):
        events = [
            SimpleNamespace(
                type="message_start",
                message=SimpleNamespace(
                    usage=SimpleNamespace(
                        input_tokens=120,
                        output_tokens=1,
                        cache_read_input_tokens=3000,
                        cache_creation_input_tokens=None,
                    )
                ),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="你好"),
                index=0,
            ),
            SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=42)),
            SimpleNamespace(type="message_stop"),
        ]

        class _FakeStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def __aiter__(self):
                async def _gen():
                    for event in events:
                        yield event

                return _gen()

        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider._client = SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **kwargs: _FakeStream())
        )
        provider._build_call_kwargs = lambda *args, **kwargs: {}

        async def collect():
            return [chunk async for chunk in provider.chat_stream("model", [])]

        chunks = asyncio.run(collect())
        finish = [chunk for chunk in chunks if chunk.type == "finish"]
        self.assertEqual(len(finish), 1)
        self.assertEqual(
            finish[0].usage,
            {"input_tokens": 120, "output_tokens": 42, "cache_read_input_tokens": 3000},
        )

    def test_thinking_budget_is_less_than_max_tokens(self):
        self.assertIsNone(ClaudeProvider._get_thinking_budget({}, 1024))
        self.assertEqual(ClaudeProvider._get_thinking_budget({}, 2048), 1024)
        self.assertEqual(
            ClaudeProvider._get_thinking_budget({"thinking_budget_tokens": 1500}, 2048), 1500
        )
        self.assertIsNone(
            ClaudeProvider._get_thinking_budget({"thinking_budget_tokens": 2048}, 2048)
        )


if __name__ == "__main__":
    unittest.main()
