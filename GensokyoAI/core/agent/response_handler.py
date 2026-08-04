"""响应处理器 - 处理模型响应和工具调用"""

# GensokyoAI/core/agent/response_handler.py

import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ...utils.helpers import safe_get
from ...utils.logger import logger
from .types import StreamChunk, UnifiedMessage

if TYPE_CHECKING:
    from ...core.config import AppConfig
    from ...memory.working import WorkingMemoryManager
    from ...tools.executor import ToolExecutor
    from .message_builder import MessageBuilder
    from .model_client import ModelClient

# 模型可能意外输出的 XML 标签残留（如 <get_current_time>, </think> 等）
_XML_TAG_PATTERN = re.compile(r"</?[a-z_]+[^>]*>")

# 流式中断时补给用户的可见标记。只投递给 UI；写入模型上下文前必须用
# strip_interrupt_marker 剥掉（错误文本不提供给模型）。
STREAM_INTERRUPT_MARKER = "响应中断"
_INTERRUPT_MARKER_PATTERN = re.compile(r"\n?\[响应中断:[^\]]*\]\n?")


def strip_interrupt_marker(text: str) -> str:
    """剥离流式中断标记，返回干净的半截正文。"""
    return _INTERRUPT_MARKER_PATTERN.sub("", text).strip()


class ResponseHandler:
    """
    响应处理器 - 纯响应生成，不操作工作记忆

    魔理沙：只管说话，记东西交给别人DA☆ZE！
    """

    def __init__(
        self,
        config: AppConfig,
        working_memory: WorkingMemoryManager,
        tool_executor: ToolExecutor,
        model_client: ModelClient,
        message_builder: MessageBuilder,
    ):
        self._config = config
        self._working_memory = working_memory
        self._tool_executor = tool_executor
        self._model_client = model_client
        self._message_builder = message_builder
        self._shutting_down = False
        self._last_assistant_reasoning: str | None = None

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = value

    @property
    def last_assistant_reasoning(self) -> str | None:
        """最近一次最终 assistant 回复对应的 reasoning_content。"""
        return self._last_assistant_reasoning

    @property
    def character_name(self) -> str:
        return safe_get(self._config, "character.name", "default")

    # ==================== 工具处理 ====================

    async def _handle_tool_calls(self, message: UnifiedMessage) -> list[dict] | None:
        if not message.tool_calls:
            return None
        parsed = self._tool_executor.parse_tool_calls(message)
        names = "、".join(item.get("name", "?") for item in (parsed or []))
        logger.info(f"检测到 {len(message.tool_calls)} 个工具调用: {names}")
        if parsed:
            for item in parsed:
                logger.debug(
                    f"调用工具: {item.get('name')}({str(item.get('arguments', ''))[:150]})"
                )
            return await self._tool_executor.execute_batch(parsed)
        return None

    @staticmethod
    def _tool_error_chunk(result: dict) -> StreamChunk | None:
        """将结构化工具错误结果转换为流式错误 chunk，供 Runtime/客户端直接消费。"""
        error = result.get("error")
        if not isinstance(error, dict):
            return None
        return StreamChunk(
            type="tool_error",
            status="failed",
            content=result.get("content", ""),
            error=error.get("technical_message") or result.get("content", ""),
            error_code=error.get("error_code"),
            error_details={
                "tool_call_id": result.get("tool_call_id", ""),
                "name": result.get("name", ""),
                "user_message": error.get("user_message"),
                "recoverable": error.get("recoverable"),
                "action_hint": error.get("action_hint"),
                "details": dict(error.get("details") or {}),
            },
        )

    def _record_tool_results(self, tool_calls_message: UnifiedMessage, results: list[dict]) -> None:
        """将工具调用和结果写入工作记忆"""

        # 写入 assistant 的 tool_call 消息。content 置空：前言文本已包含在
        # MESSAGE_SENT 记录的完整回复（前言 + 续写）中，这里再存一份会让
        # 前言在工作记忆里出现两次，白占上下文 token。
        if tool_calls_message.tool_calls:
            self._working_memory.add_message(
                role="assistant",
                content="",
                tool_calls=tool_calls_message.tool_calls,
                reasoning_content=tool_calls_message.reasoning_content,
            )

        # 写入 tool 结果
        for index, r in enumerate(results):
            fallback_id = ""
            if tool_calls_message.tool_calls and index < len(tool_calls_message.tool_calls):
                fallback_id = tool_calls_message.tool_calls[index].id
            self._working_memory.add_message(
                role="tool",
                content=r["content"],
                tool_call_id=r.get("tool_call_id") or fallback_id,
            )

    # ==================== 响应处理 ====================

    async def process_stream(
        self,
        messages: list[dict[str, str]],
        tools: list[dict] | None,
        *,
        continuation_contexts: list[str] | None = None,
        continuation_input: str = "",
    ) -> AsyncIterator[StreamChunk]:
        self._last_assistant_reasoning = None
        tool_calls_message: UnifiedMessage | None = None
        assistant_content = ""
        assistant_reasoning = ""

        # 第一次流式调用
        async for chunk in self._safe_stream(messages, tools, "第一次流式调用"):
            if self._shutting_down:
                break
            if chunk.reasoning_content:
                assistant_reasoning += chunk.reasoning_content
                yield chunk
                continue
            if chunk.is_tool_call and chunk.tool_info:
                message = chunk.tool_info.get("message")
                if not isinstance(message, UnifiedMessage):
                    continue
                tool_calls_message = message
                if assistant_reasoning and not tool_calls_message.reasoning_content:
                    tool_calls_message.reasoning_content = assistant_reasoning
            else:
                cleaned = self._clean_chunk(chunk)
                if cleaned.content:
                    assistant_content += cleaned.content
                yield cleaned

        if self._shutting_down or not tool_calls_message:
            self._last_assistant_reasoning = assistant_reasoning or None
            return

        if assistant_content and not tool_calls_message.content:
            tool_calls_message.content = assistant_content
        if assistant_reasoning and not tool_calls_message.reasoning_content:
            tool_calls_message.reasoning_content = assistant_reasoning

        # 工具调用
        tool_results = await self._safe_tool_calls(tool_calls_message)
        if not tool_results:
            return

        for result in tool_results:
            if error_chunk := self._tool_error_chunk(result):
                yield error_chunk

        self._safe_record_results(tool_calls_message, tool_results)
        # 仅 world 回合注入临时触发；单角色保持旧调用形态（行为零变化，
        # 也避免依赖旧签名的 duck 类型在单角色路径上被新参数破坏）。
        if continuation_input:
            cont_messages = self._message_builder.build_continuation(
                continuation_contexts, ephemeral_input=continuation_input
            )
        else:
            cont_messages = self._message_builder.build_continuation(continuation_contexts)

        continuation_reasoning = ""

        # 第二次流式调用
        async for chunk in self._safe_stream(cont_messages, tools, "第二次流式调用"):
            if self._shutting_down:
                break
            if chunk.reasoning_content:
                continuation_reasoning += chunk.reasoning_content
                yield chunk
                continue
            yield self._clean_chunk(chunk)

        self._last_assistant_reasoning = continuation_reasoning or None

    # ==================== 私有容错方法 ====================

    async def _safe_stream(
        self,
        messages: list,
        tools: list | None,
        context: str,
        extra_body: dict | None = None,  # 新增参数
    ) -> AsyncIterator[StreamChunk]:
        """带容错的流式调用"""
        try:
            async for chunk in self._model_client.chat_stream(
                messages,
                tools,
                extra_body=extra_body,  # 传进去
            ):
                yield chunk
        except Exception as e:
            logger.error(f"{context}失败: {e}")
            yield StreamChunk(content=f"\n[{STREAM_INTERRUPT_MARKER}: {e}]\n")

    async def _safe_tool_calls(self, message: UnifiedMessage) -> list[dict] | None:
        """带容错的工具调用"""
        try:
            return await self._handle_tool_calls(message)
        except Exception as e:
            logger.error(f"工具调用处理失败: {e}")
            return None

    def _safe_record_results(self, tool_calls_message: UnifiedMessage, results: list[dict]) -> None:
        try:
            self._record_tool_results(tool_calls_message, results)
        except Exception as e:
            logger.warning(f"记录工具结果失败: {e}")

    # ==================== 内容清洗 ====================

    @staticmethod
    def _clean_chunk(chunk: StreamChunk) -> StreamChunk:
        """清洗模型意外输出的 XML 标签残留，防止脏数据进入工作记忆"""
        if chunk.content and _XML_TAG_PATTERN.search(chunk.content):
            cleaned = _XML_TAG_PATTERN.sub("", chunk.content)
            if cleaned.strip():
                chunk.content = cleaned
        return chunk
