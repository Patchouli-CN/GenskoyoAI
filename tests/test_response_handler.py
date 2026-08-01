"""ResponseHandler 工具路径记录测试：前言不重复入工作记忆"""

import asyncio
import unittest
from types import SimpleNamespace

from GensokyoAI.core.agent.response_handler import ResponseHandler
from GensokyoAI.core.agent.types import StreamChunk, ToolCall, ToolCallFunction, UnifiedMessage
from GensokyoAI.memory.working import WorkingMemoryManager


class _FakeModelClient:
    def __init__(self, streams):
        self._streams = list(streams)

    async def chat_stream(self, messages, tools, extra_body=None):
        stream = self._streams.pop(0)
        for chunk in stream:
            yield chunk


class _FakeToolExecutor:
    def parse_tool_calls(self, message):
        return [
            {
                "id": tc.id,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            }
            for tc in message.tool_calls or []
        ]

    async def execute_batch(self, parsed):
        return [
            {
                "role": "tool",
                "tool_call_id": item["id"],
                "name": item["name"],
                "content": "15:54:02",
            }
            for item in parsed
        ]


class _FakeMessageBuilder:
    def __init__(self, working_memory):
        self._working_memory = working_memory

    def build_continuation(self, system_contexts=None):
        return self._working_memory.get_context()


class ToolPrefaceRecordTests(unittest.TestCase):
    def test_preface_is_recorded_once_not_duplicated(self):
        working_memory = WorkingMemoryManager()
        tool_call = ToolCall(
            id="call_1",
            provider="openai",
            function=ToolCallFunction(name="get_current_time", arguments={}, provider="openai"),
        )
        model_client = _FakeModelClient(
            [
                [
                    StreamChunk(content="我查一下。"),
                    StreamChunk(
                        is_tool_call=True,
                        tool_info={
                            "message": UnifiedMessage(
                                role="assistant",
                                content="",
                                tool_calls=[tool_call],
                            )
                        },
                    ),
                ],
                [StreamChunk(content="现在是15:54。")],
            ]
        )
        handler = ResponseHandler(
            SimpleNamespace(character=SimpleNamespace(name="test")),
            working_memory,
            _FakeToolExecutor(),
            model_client,
            _FakeMessageBuilder(working_memory),
        )

        async def collect():
            output = ""
            async for chunk in handler.process_stream(
                [{"role": "user", "content": "几点"}],
                [{"type": "function", "function": {"name": "get_current_time"}}],
            ):
                output += chunk.content or ""
            return output

        full_response = asyncio.run(collect())
        # 完整回复 = 前言 + 工具后续写，MESSAGE_SENT 按全文记录一份
        self.assertEqual(full_response, "我查一下。现在是15:54。")
        working_memory.add_message("assistant", full_response)

        context = working_memory.get_context()
        self.assertEqual(context[0]["role"], "assistant")
        self.assertIn("tool_calls", context[0])
        # 工具调用消息不再携带前言：前言只在最终回复里出现一次
        self.assertEqual(context[0]["content"], "")
        self.assertEqual(context[1]["role"], "tool")
        self.assertEqual(context[2]["content"], full_response)
        preface_occurrences = sum(
            "我查一下。" in str(message.get("content", "")) for message in context
        )
        self.assertEqual(preface_occurrences, 1)


if __name__ == "__main__":
    unittest.main()
