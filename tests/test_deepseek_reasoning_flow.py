import asyncio
import unittest
from types import SimpleNamespace

from GensokyoAI.core.agent.response_handler import ResponseHandler
from GensokyoAI.core.agent.types import StreamChunk, ToolCall, ToolCallFunction, UnifiedMessage
from GensokyoAI.core.event_listeners import CoreListeners
from GensokyoAI.core.events import Event, SystemEvent
from GensokyoAI.memory.working import WorkingMemoryManager


class _DummyEventBus:
    def subscribe(self, *args, **kwargs):
        pass

    def publish(self, *args, **kwargs):
        pass


class _FakeModelClient:
    def __init__(self, streams):
        self._streams = list(streams)
        self.calls = []

    async def chat_stream(self, messages, tools, extra_body=None):
        self.calls.append(messages)
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
        return (
            [{"role": "system", "content": "sys"}]
            + self._working_memory.get_context()
            + [{"role": "system", "content": "工具调用已完成"}]
        )


class DeepSeekReasoningFlowTests(unittest.TestCase):
    def _record_message_sent(self, working_memory, content, reasoning_content):
        agent = SimpleNamespace(working_memory=working_memory)
        listener = CoreListeners(agent, _DummyEventBus())
        asyncio.run(
            listener.on_message_sent(
                Event(
                    type=SystemEvent.MESSAGE_SENT,
                    source="agent",
                    data={
                        "content": content,
                        "reasoning_content": reasoning_content,
                    },
                )
            )
        )

    def test_plain_assistant_reasoning_is_exposed_for_message_sent_persistence(self):
        working_memory = WorkingMemoryManager()
        model_client = _FakeModelClient(
            [
                [
                    StreamChunk(reasoning_content="第一轮思考。"),
                    StreamChunk(content="你好。"),
                ]
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
            reasoning = ""
            async for chunk in handler.process_stream(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "你好"}],
                None,
            ):
                output += chunk.content
                reasoning += chunk.reasoning_content or ""
            return output, reasoning

        content, streamed_reasoning = asyncio.run(collect())
        self.assertEqual(content, "你好。")
        self.assertEqual(streamed_reasoning, "第一轮思考。")
        self.assertEqual(handler.last_assistant_reasoning, "第一轮思考。")

        self._record_message_sent(working_memory, content, handler.last_assistant_reasoning)
        self.assertEqual(working_memory.get_context()[-1]["reasoning_content"], "第一轮思考。")

    def test_tool_continuation_final_assistant_reasoning_is_preserved_for_next_turn(self):
        working_memory = WorkingMemoryManager()
        tool_call = ToolCall(
            id="call_1",
            provider="deepseek",
            function=ToolCallFunction(
                name="get_current_time",
                arguments={},
                provider="deepseek",
            ),
        )
        model_client = _FakeModelClient(
            [
                [
                    StreamChunk(reasoning_content="准备调用工具。"),
                    StreamChunk(
                        is_tool_call=True,
                        tool_info={
                            "message": UnifiedMessage(
                                role="assistant",
                                content="",
                                tool_calls=[tool_call],
                                reasoning_content="准备调用工具。",
                            )
                        },
                    ),
                ],
                [
                    StreamChunk(reasoning_content="工具结果整合思考。"),
                    StreamChunk(content="现在是15:54:02。"),
                ],
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
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "几点"}],
                [{"type": "function", "function": {"name": "get_current_time"}}],
            ):
                output += chunk.content
            return output

        content = asyncio.run(collect())
        self.assertEqual(content, "现在是15:54:02。")
        self.assertEqual(handler.last_assistant_reasoning, "工具结果整合思考。")

        self._record_message_sent(working_memory, content, handler.last_assistant_reasoning)
        context = working_memory.get_context()
        self.assertEqual(context[0]["role"], "assistant")
        self.assertEqual(context[0]["reasoning_content"], "准备调用工具。")
        self.assertIn("tool_calls", context[0])
        self.assertEqual(context[1]["role"], "tool")
        self.assertEqual(context[2]["role"], "assistant")
        self.assertEqual(context[2]["reasoning_content"], "工具结果整合思考。")

        next_turn_messages = (
            [{"role": "system", "content": "sys"}]
            + context
            + [{"role": "user", "content": "可以可以！"}]
        )
        assistant_messages = [m for m in next_turn_messages if m["role"] == "assistant"]
        self.assertTrue(assistant_messages)
        self.assertTrue(all("reasoning_content" in m for m in assistant_messages))


if __name__ == "__main__":
    unittest.main()
