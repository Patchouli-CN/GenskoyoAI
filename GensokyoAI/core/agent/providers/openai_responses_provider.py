"""OpenAI Responses API Provider 实现

专用于 OpenAI 官方 Responses API（/v1/responses），提供：
- 更优的推理模型性能（GPT-5 等）
- 内置工具支持（web_search、code_interpreter 等）
- 原生多轮对话状态管理（previous_response_id）
- 更低的成本（更好的缓存利用率）

注意：此 Provider 仅适用于 OpenAI 官方 API，不兼容第三方 OpenAI 兼容服务。
对于 Deepseek/SiliconFlow/vLLM/Groq 等第三方服务，请使用 openai Provider
（Chat Completions API）。
"""

# GensokyoAI/core/agent/providers/openai_responses_provider.py

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ....utils.logger import logger
from ....utils.request_utils import (
    ModelAPIError,
    endpoint_url,
    has_arbitrary_api_path,
    normalize_openai_responses_host_and_path,
    post_json,
    post_sse,
    sdk_base_url_for_endpoint,
)
from ..types import (
    ImageInput,
    ModelInfo,
    ProviderCapability,
    StreamChunk,
    ToolCall,
    ToolCallFunction,
    UnifiedEmbeddingResponse,
    UnifiedMessage,
    UnifiedResponse,
    WebSearchDiagnostics,
    WebSearchReference,
)
from .base import BaseProvider
from .openai_provider import OpenAIProvider

if TYPE_CHECKING:
    from ...config import ModelConfig


class OpenAIResponsesProvider(BaseProvider):
    """
    OpenAI Responses API Provider

    使用 openai SDK 调用 OpenAI Responses API（/v1/responses）。
    推荐用于 OpenAI 官方 API，可获得更好的推理性能和更低的成本。
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self._endpoint = normalize_openai_responses_host_and_path(config.base_url, config.api_path)
        self._client = self._build_client()
        logger.debug(
            f"OpenAIResponsesProvider 初始化完成，base_url: {self._endpoint.api_host}, "
            f"api_path: {self._endpoint.api_path}, model: {config.name}"
        )

    @property
    def capabilities(self) -> set[str]:
        """OpenAI Responses Provider 能力声明。"""
        return self.apply_model_capability_overrides(
            {
                ProviderCapability.CHAT,
                ProviderCapability.STREAM,
                ProviderCapability.TOOLS,
                ProviderCapability.EMBEDDINGS,
                ProviderCapability.VISION,
                ProviderCapability.REASONING,
                ProviderCapability.RESPONSES_API,
                ProviderCapability.CUSTOM_ENDPOINT,
                ProviderCapability.WEB_SEARCH,
                ProviderCapability.STRUCTURED_OUTPUT,
            }
        )

    @property
    def endpoint(self) -> str:
        """当前 Provider 的规范化 API endpoint。"""
        return f"{self._endpoint.api_host}{self._endpoint.api_path}"

    def _uses_custom_http(self) -> bool:
        """当前 api_path 是否需要绕过 SDK 固定 resource path。"""
        endpoint = getattr(self, "_endpoint", None)
        if endpoint is None:
            return False
        return has_arbitrary_api_path(endpoint, "/responses")

    def _request_headers(self) -> dict[str, str]:
        """构建自定义 HTTP 调用 headers。"""
        headers = self.merged_headers()
        if self.config.api_key:
            headers.setdefault("Authorization", f"Bearer {self.config.api_key}")
        return headers

    def _build_client(self):
        """构建 OpenAI 异步客户端"""
        openai_module = self.import_optional_dependency(
            "openai",
            "使用 OpenAI Responses Provider 需要安装 openai 包: pip install openai\n"
            "或者: pip install gensokyoai[openai]",
        )
        AsyncOpenAI = openai_module.AsyncOpenAI

        self._endpoint = normalize_openai_responses_host_and_path(
            self.config.base_url,
            self.config.api_path,
        )
        kwargs = {}
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        kwargs["base_url"] = sdk_base_url_for_endpoint(self._endpoint, "/responses")
        default_headers = self.merged_headers()
        if default_headers:
            kwargs["default_headers"] = default_headers

        return AsyncOpenAI(**kwargs)

    # ==================== 核心 API ====================

    @staticmethod
    def _response_format_to_text_format(response_format: dict) -> dict:
        """将 Chat Completions 风格 response_format 转换为 Responses text.format。"""
        if response_format.get("type") != "json_schema":
            return response_format
        json_schema = response_format.get("json_schema")
        if not isinstance(json_schema, dict):
            return response_format
        text_format: dict[str, Any] = {"type": "json_schema"}
        if name := json_schema.get("name"):
            text_format["name"] = name
        if "strict" in json_schema:
            text_format["strict"] = json_schema["strict"]
        if schema := json_schema.get("schema"):
            text_format["schema"] = schema
        return text_format

    def _build_call_kwargs(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        options: dict | None,
        *,
        stream: bool = False,
    ) -> dict:
        """组装 responses.create 的共用请求参数（chat 与 chat_stream 共用）。"""
        options = options or {}

        # 从 messages 中分离 system/developer 指令和对话内容
        instructions, input_items = self._convert_messages_to_input(messages)

        call_kwargs: dict = {
            "model": model,
            "input": input_items,
        }
        if stream:
            call_kwargs["stream"] = True

        # 设置 instructions（系统提示词）
        if instructions:
            call_kwargs["instructions"] = instructions

        # temperature 和 top_p
        temperature = options.get("temperature", 0.7)
        top_p = options.get("top_p", 0.9)
        call_kwargs["temperature"] = temperature
        call_kwargs["top_p"] = top_p

        # max_output_tokens（Responses API 使用此参数名）
        max_tokens = (
            options.get("max_completion_tokens")
            or options.get("num_predict")
            or options.get("max_tokens")
        )
        if max_tokens:
            call_kwargs["max_output_tokens"] = max_tokens

        # text format 当前仅非流式路径使用（保持既有行为）
        if not stream and (response_format := options.get("response_format")):
            call_kwargs["text"] = {"format": self._response_format_to_text_format(response_format)}

        # 工具支持
        converted_tools = self._convert_tools_to_responses(tools) if tools else []
        self._inject_web_search_tool(converted_tools, options)
        if converted_tools:
            call_kwargs["tools"] = converted_tools
            if tool_choice := options.get("tool_choice"):
                call_kwargs["tool_choice"] = tool_choice

        # 推理配置（reasoning effort）
        if reasoning := options.get("reasoning"):
            call_kwargs["reasoning"] = reasoning

        # store 配置（默认不存储，因为项目自己管理对话状态）
        call_kwargs["store"] = options.get("store", False)

        return call_kwargs

    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        """非流式调用 OpenAI Responses API"""
        call_kwargs = self._build_call_kwargs(model, messages, tools, options)

        if self._uses_custom_http():
            response = await post_json(
                endpoint_url(self._endpoint),
                call_kwargs,
                self._request_headers(),
                self.config.timeout,
            )
            return self._convert_response_dict(response)

        response = await self._client.responses.create(**call_kwargs)

        return self._convert_response(response)

    async def list_models(self) -> list[ModelInfo]:
        """Responses Provider 复用 OpenAI `/models` 列表。"""
        try:
            response = await self._client.models.list()
        except Exception as e:
            logger.warning(f"拉取 Responses 模型列表失败，将返回当前配置模型作为 fallback: {e}")
            return [
                ModelInfo(
                    id=self.config.name,
                    name=self.config.name,
                    capabilities=sorted(self._infer_model_capabilities(self.config.name, {})),
                    metadata={"fallback": True},
                )
            ]

        models: list[ModelInfo] = []
        for item in getattr(response, "data", []) or []:
            model_id = getattr(item, "id", "") or ""
            if not model_id:
                continue
            metadata = item.model_dump() if hasattr(item, "model_dump") else {}
            models.append(
                ModelInfo(
                    id=model_id,
                    name=model_id,
                    capabilities=sorted(self._infer_model_capabilities(model_id, metadata)),
                    owned_by=getattr(item, "owned_by", None),
                    metadata=metadata,
                )
            )
        return models

    def _infer_model_capabilities(self, model_id: str, metadata: dict[str, Any]) -> set[str]:
        """Responses API 模型默认支持内置工具；结合模型名与 metadata 应用细粒度能力。"""
        capabilities = set(self.capabilities)
        normalized_id = model_id.lower()
        tokens = OpenAIProvider._metadata_capability_tokens(metadata)
        if OpenAIProvider._metadata_or_model_indicates_web_search(normalized_id, tokens):
            capabilities.add(ProviderCapability.WEB_SEARCH)
        return self.apply_model_capability_overrides(capabilities)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用 OpenAI Responses API"""
        options = options or {}
        call_kwargs = self._build_call_kwargs(model, messages, tools, options, stream=True)

        # 流式工具调用累积器：{output_index: {"call_id": ..., "name": ..., "arguments": ...}}
        tool_calls_acc: dict[int, dict] = {}

        if self._uses_custom_http():
            async for event_data in post_sse(
                endpoint_url(self._endpoint),
                call_kwargs,
                self._request_headers(),
                self.config.timeout,
            ):
                async for stream_chunk in self._convert_stream_event_dict(
                    event_data, tool_calls_acc, options
                ):
                    yield stream_chunk
            return

        stream = await self._client.responses.create(**call_kwargs)

        async for event in stream:
            event_type = event.type if hasattr(event, "type") else ""

            # 文本内容增量
            if event_type == "response.output_text.delta":
                delta_text = event.delta if hasattr(event, "delta") else ""
                if delta_text:
                    yield StreamChunk(content=delta_text)

            # 工具调用参数增量
            elif event_type == "response.function_call_arguments.delta":
                output_index = getattr(event, "output_index", 0)
                if output_index not in tool_calls_acc:
                    tool_calls_acc[output_index] = {
                        "call_id": "",
                        "name": "",
                        "arguments": "",
                    }
                delta = getattr(event, "delta", "")
                tool_calls_acc[output_index]["arguments"] += delta

            # 工具调用：新的输出项添加
            elif event_type == "response.output_item.added":
                item = getattr(event, "item", None)
                if item and getattr(item, "type", "") == "function_call":
                    output_index = getattr(event, "output_index", 0)
                    tool_calls_acc[output_index] = {
                        "call_id": getattr(item, "call_id", "") or getattr(item, "id", ""),
                        "name": getattr(item, "name", ""),
                        "arguments": getattr(item, "arguments", "") or "",
                    }

            # 工具调用参数完成
            elif event_type == "response.function_call_arguments.done":
                output_index = getattr(event, "output_index", 0)
                if output_index in tool_calls_acc:
                    # 更新完整的 arguments
                    tool_calls_acc[output_index]["arguments"] = getattr(event, "arguments", "")

            # 响应完成
            elif event_type == "response.completed":
                # 如果有工具调用，组装并 yield
                if tool_calls_acc:
                    unified_tool_calls = []
                    raw_arguments: dict[int, str] = {}
                    for idx, tc_data in sorted(tool_calls_acc.items()):
                        try:
                            args = json.loads(tc_data["arguments"]) if tc_data["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {}
                            raw_arguments[idx] = tc_data["arguments"]
                        unified_tool_calls.append(
                            ToolCall(
                                id=tc_data.get("call_id", ""),
                                function=ToolCallFunction(
                                    name=tc_data["name"],
                                    arguments=args,
                                ),
                            )
                        )

                    unified_msg = UnifiedMessage(
                        role="assistant",
                        content="",
                        tool_calls=unified_tool_calls,
                    )
                    usage = self._usage_to_dict(
                        getattr(getattr(event, "response", None), "usage", None)
                    )
                    tool_info: dict[str, Any] = {"message": unified_msg}
                    if raw_arguments:
                        tool_info["raw_arguments"] = raw_arguments
                    yield StreamChunk(
                        type="tool_call",
                        is_tool_call=True,
                        tool_info=tool_info,
                        finish_reason="tool_calls",
                        usage=usage,
                    )

                response = getattr(event, "response", None)
                usage = self._usage_to_dict(getattr(response, "usage", None))
                references = self._extract_web_search_references(response)
                diagnostics = self._build_web_search_diagnostics(options, references)
                yield StreamChunk(
                    type="finish",
                    finish_reason="completed",
                    usage=usage,
                    web_search_references=references,
                    web_search_diagnostics=diagnostics,
                )

            elif event_type == "response.failed":
                response = getattr(event, "response", None)
                error = getattr(response, "error", None) or getattr(event, "error", None)
                error_message = (
                    self._response_error_to_message(error) or "Responses API stream failed"
                )
                yield StreamChunk(type="error", error=error_message, finish_reason="failed")
                raise ModelAPIError(
                    error_message,
                    provider=self.config.provider,
                    model=model,
                    endpoint=self.endpoint,
                    retryable=False,
                )

            elif event_type == "response.incomplete":
                response = getattr(event, "response", None)
                reason = getattr(response, "incomplete_details", None) or getattr(
                    event,
                    "incomplete_details",
                    None,
                )
                error_message = (
                    self._response_error_to_message(reason) or "Responses API stream incomplete"
                )
                yield StreamChunk(type="error", error=error_message, finish_reason="incomplete")
                raise ModelAPIError(
                    error_message,
                    provider=self.config.provider,
                    model=model,
                    endpoint=self.endpoint,
                    retryable=False,
                )

    async def embeddings(
        self,
        model: str,
        prompt: str,
        **kwargs,
    ) -> UnifiedEmbeddingResponse:
        """获取文本向量（Embeddings API 在两个 Provider 中是相同的）"""
        embed_kwargs: dict = {
            "model": model,
            "input": prompt,
        }

        if dimensions := kwargs.get("dimensions"):
            embed_kwargs["dimensions"] = dimensions

        if encoding_format := kwargs.get("encoding_format"):
            embed_kwargs["encoding_format"] = encoding_format

        response = await self._client.embeddings.create(**embed_kwargs)

        return UnifiedEmbeddingResponse(
            embedding=response.data[0].embedding,
            model=model,
        )

    # ==================== 转换方法 ====================

    @staticmethod
    def _image_to_url(image: ImageInput | dict | Any) -> str:
        """将统一图片输入转换为 Responses API input_image 可接受的 URL 或 data URL。"""
        url = image.get("url") if isinstance(image, dict) else getattr(image, "url", None)
        data = image.get("data") if isinstance(image, dict) else getattr(image, "data", None)
        mime_type = (
            image.get("mime_type") if isinstance(image, dict) else getattr(image, "mime_type", None)
        ) or "image/png"
        if url:
            return url
        if data:
            return data if str(data).startswith("data:") else f"data:{mime_type};base64,{data}"
        return ""

    @staticmethod
    def _convert_content_parts(content: Any) -> Any:
        """将统一多模态 content parts 转换为 Responses API content blocks。"""
        if not isinstance(content, list):
            return content

        converted: list[dict] = []
        for part in content:
            part_type = (
                part.get("type") if isinstance(part, dict) else getattr(part, "type", "text")
            )
            if part_type == "text":
                text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if text is not None:
                    converted.append({"type": "input_text", "text": str(text)})
                continue

            image = part.get("image") if isinstance(part, dict) else getattr(part, "image", None)
            if part_type == "image" and image:
                image_url = OpenAIResponsesProvider._image_to_url(image)
                if not image_url:
                    continue
                block: dict[str, Any] = {"type": "input_image", "image_url": image_url}
                detail = (
                    image.get("detail")
                    if isinstance(image, dict)
                    else getattr(image, "detail", None)
                )
                if detail:
                    block["detail"] = detail
                converted.append(block)
        return converted

    @staticmethod
    def _convert_messages_to_input(
        messages: list[dict],
    ) -> tuple[str, list[dict]]:
        """
        将 Chat Completions 格式的 messages 转换为 Responses API 的 input + instructions

        Chat Completions 格式:
          [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}, ...]

        Responses API 格式:
          instructions="..." (系统提示词)
          input=[{"role": "user", "content": "..."}, ...] (对话内容)

        转换规则:
          - 第一个 system/developer 消息 → instructions 参数
          - 后续的 system 消息 → 合并到 instructions 或作为 developer 消息插入 input
          - user/assistant/tool 消息 → input 数组
        """
        instructions_parts: list[str] = []
        input_items: list[dict] = []

        for msg in messages:
            role = msg.get("role", "")
            content = OpenAIResponsesProvider._convert_content_parts(msg.get("content", ""))

            if role in ("system", "developer"):
                # 收集所有 system/developer 消息为 instructions
                if content:
                    if isinstance(content, list):
                        instructions_parts.extend(
                            block.get("text", "")
                            for block in content
                            if block.get("type") == "input_text"
                        )
                    else:
                        instructions_parts.append(str(content))
            elif role == "assistant":
                input_items.append({"role": "assistant", "content": content})
            elif role == "user":
                input_items.append({"role": "user", "content": content})
            elif role == "tool":
                # tool 结果消息 → function_call_output Item
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.get("tool_call_id", ""),
                        "output": content if isinstance(content, str) else str(content),
                    }
                )
            else:
                # 未知角色，作为 user 消息处理
                input_items.append({"role": "user", "content": content})

        instructions = "\n\n".join(instructions_parts) if instructions_parts else ""
        return instructions, input_items

    def _convert_response(self, response) -> UnifiedResponse:
        """将 Responses API 响应转换为 UnifiedResponse"""
        output = getattr(response, "output", []) or []

        # 提取文本内容和工具调用
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking: str | None = None

        for item in output:
            item_type = getattr(item, "type", "")

            if item_type == "message":
                # 文本消息
                content_list = getattr(item, "content", []) or []
                for content_item in content_list:
                    content_type = getattr(content_item, "type", "")
                    if content_type == "output_text":
                        text = getattr(content_item, "text", "")
                        if text:
                            text_parts.append(text)

            elif item_type == "function_call":
                # 工具调用
                call_id = getattr(item, "call_id", "") or getattr(item, "id", "")
                name = getattr(item, "name", "")
                arguments_str = getattr(item, "arguments", "")
                try:
                    args = json.loads(arguments_str) if arguments_str else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(
                        id=call_id,
                        function=ToolCallFunction(
                            name=name,
                            arguments=args,
                        ),
                    )
                )

            elif item_type == "reasoning":
                # 推理内容
                reasoning_content = getattr(item, "content", []) or []
                reasoning_texts = []
                for rc in reasoning_content:
                    if hasattr(rc, "text") and rc.text:
                        reasoning_texts.append(rc.text)
                if reasoning_texts:
                    thinking = "\n".join(reasoning_texts)

        # 组装文本
        full_text = "\n".join(text_parts) if text_parts else ""

        # 也尝试从 output_text 便捷属性获取
        if not full_text and hasattr(response, "output_text") and response.output_text:
            full_text = response.output_text

        references = self._extract_web_search_references(response)
        return UnifiedResponse(
            message=UnifiedMessage(
                role="assistant",
                content=full_text,
                tool_calls=tool_calls if tool_calls else None,
            ),
            model=getattr(response, "model", "") or "",
            done=True,
            thinking=thinking,
            web_search_references=references,
            web_search_diagnostics=self._build_web_search_diagnostics({}, references),
        )

    def _convert_response_dict(self, response: dict[str, Any]) -> UnifiedResponse:
        """将原始 Responses JSON 响应转换为 UnifiedResponse。"""
        output = response.get("output") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking: str | None = None
        for item in output:
            item_type = item.get("type", "")
            if item_type == "message":
                for content_item in item.get("content") or []:
                    if content_item.get("type") == "output_text" and content_item.get("text"):
                        text_parts.append(content_item.get("text") or "")
            elif item_type == "function_call":
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(
                        id=item.get("call_id") or item.get("id") or "",
                        function=ToolCallFunction(name=item.get("name") or "", arguments=args),
                    )
                )
            elif item_type == "reasoning":
                reasoning_texts = [
                    rc.get("text") for rc in item.get("content") or [] if rc.get("text")
                ]
                if reasoning_texts:
                    thinking = "\n".join(reasoning_texts)
        full_text = "\n".join(text_parts) if text_parts else (response.get("output_text") or "")
        references = self._extract_web_search_references(response)
        return UnifiedResponse(
            message=UnifiedMessage(
                role="assistant",
                content=full_text,
                tool_calls=tool_calls if tool_calls else None,
            ),
            model=response.get("model") or "",
            done=True,
            thinking=thinking,
            web_search_references=references,
            web_search_diagnostics=self._build_web_search_diagnostics({}, references),
        )

    async def _convert_stream_event_dict(
        self,
        event: dict[str, Any],
        tool_calls_acc: dict[int, dict],
        options: dict,
    ) -> AsyncIterator[StreamChunk]:
        """将原始 Responses SSE JSON event 转换为 StreamChunk。"""
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            if event.get("delta"):
                yield StreamChunk(content=event.get("delta") or "")
        elif event_type == "response.function_call_arguments.delta":
            output_index = int(event.get("output_index", 0))
            tool_calls_acc.setdefault(output_index, {"call_id": "", "name": "", "arguments": ""})
            tool_calls_acc[output_index]["arguments"] += event.get("delta") or ""
        elif event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                output_index = int(event.get("output_index", 0))
                tool_calls_acc[output_index] = {
                    "call_id": item.get("call_id") or item.get("id") or "",
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "",
                }
        elif event_type == "response.function_call_arguments.done":
            output_index = int(event.get("output_index", 0))
            if output_index in tool_calls_acc:
                tool_calls_acc[output_index]["arguments"] = event.get("arguments") or ""
        elif event_type == "response.completed":
            response = event.get("response") or {}
            usage = self._usage_to_dict(response.get("usage"))
            if tool_calls_acc:
                unified_tool_calls = []
                raw_arguments: dict[int, str] = {}
                for idx, tc_data in sorted(tool_calls_acc.items()):
                    try:
                        args = json.loads(tc_data["arguments"]) if tc_data["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
                        raw_arguments[idx] = tc_data["arguments"]
                    unified_tool_calls.append(
                        ToolCall(
                            id=tc_data.get("call_id", ""),
                            function=ToolCallFunction(name=tc_data["name"], arguments=args),
                        )
                    )
                tool_info: dict[str, Any] = {
                    "message": UnifiedMessage(
                        role="assistant", content="", tool_calls=unified_tool_calls
                    )
                }
                if raw_arguments:
                    tool_info["raw_arguments"] = raw_arguments
                yield StreamChunk(
                    type="tool_call",
                    is_tool_call=True,
                    tool_info=tool_info,
                    finish_reason="tool_calls",
                    usage=usage,
                )
            references = self._extract_web_search_references(response)
            yield StreamChunk(
                type="finish",
                finish_reason="completed",
                usage=usage,
                web_search_references=references,
                web_search_diagnostics=self._build_web_search_diagnostics(options, references),
            )
        elif event_type in ("response.failed", "response.incomplete"):
            error_obj = (
                (event.get("response") or {}).get("error")
                or event.get("error")
                or event.get("incomplete_details")
            )
            error_message = (
                self._response_error_to_message(error_obj) or f"Responses API stream {event_type}"
            )
            finish_reason = "failed" if event_type == "response.failed" else "incomplete"
            yield StreamChunk(type="error", error=error_message, finish_reason=finish_reason)
            raise ModelAPIError(
                error_message,
                provider=self.config.provider,
                model=self.config.name,
                endpoint=self.endpoint,
                retryable=False,
            )

    @staticmethod
    def _response_error_to_message(error) -> str:
        """从 Responses 流式错误/不完整事件中提取可读信息。"""
        if error is None:
            return ""
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            for key in ("message", "reason", "code", "type"):
                if error.get(key):
                    return str(error[key])
            return str(error)
        for attr in ("message", "reason", "code", "type"):
            value = getattr(error, attr, None)
            if value:
                return str(value)
        return str(error)

    @staticmethod
    def _usage_to_dict(usage) -> dict | None:
        """将 Responses API usage 对象转换为 dict。"""
        if usage is None:
            return None
        if isinstance(usage, dict):
            return dict(usage)
        if hasattr(usage, "model_dump"):
            try:
                dumped = usage.model_dump()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                pass
        result = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if hasattr(usage, key):
                result[key] = getattr(usage, key)
        return result or None

    @staticmethod
    def _object_to_dict(value: Any) -> dict[str, Any]:
        """将 SDK 对象尽量转为 dict，便于统一解析引用。"""
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "model_dump"):
            try:
                dumped = value.model_dump()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                pass
        result: dict[str, Any] = {}
        for key in (
            "type",
            "title",
            "url",
            "snippet",
            "text",
            "source",
            "published_at",
            "annotations",
            "content",
            "metadata",
        ):
            if hasattr(value, key):
                result[key] = getattr(value, key)
        return result

    @classmethod
    def _extract_web_search_references(cls, response: Any) -> list[WebSearchReference]:
        """从 Responses 输出 annotations / web_search_call 中提取统一搜索引用。"""
        if response is None:
            return []
        references: list[WebSearchReference] = []
        seen: set[str] = set()

        def add_reference(data: Any, source: str = "openai_responses") -> None:
            item = cls._object_to_dict(data)
            title = str(item.get("title") or item.get("text") or item.get("url") or "")
            url = str(item.get("url") or "")
            if not url:
                return
            key = url
            if key in seen:
                return
            seen.add(key)
            references.append(
                WebSearchReference(
                    title=title,
                    url=url,
                    snippet=item.get("snippet"),
                    source=item.get("source") or source,
                    published_at=item.get("published_at"),
                    metadata=item,
                )
            )

        for output_item in getattr(response, "output", []) or []:
            output_data = cls._object_to_dict(output_item)
            if output_data.get("type") == "web_search_call":
                for result in output_data.get("results", []) or []:
                    add_reference(result)
            for content_item in output_data.get("content", []) or []:
                content_data = cls._object_to_dict(content_item)
                for annotation in content_data.get("annotations", []) or []:
                    annotation_data = cls._object_to_dict(annotation)
                    if annotation_data.get("type") in (
                        "url_citation",
                        "citation",
                    ) or annotation_data.get("url"):
                        add_reference(annotation_data)
        return references

    @staticmethod
    def _web_search_options(options: dict) -> dict[str, Any]:
        raw = options.get("web_search") or {}
        return dict(raw) if isinstance(raw, dict) else {"enabled": bool(raw)}

    def _web_search_enabled(self, options: dict) -> bool:
        web_search = self._web_search_options(options)
        strategy = web_search.get("strategy", "off")
        return bool(web_search.get("enabled")) and strategy != "off"

    def _inject_web_search_tool(self, tools: list[dict], options: dict) -> None:
        """按显式配置向 Responses tools 注入内置 web_search_preview。"""
        if not self._web_search_enabled(options):
            return
        if any(str(tool.get("type", "")).startswith("web_search") for tool in tools):
            return
        web_search = self._web_search_options(options)
        tool: dict[str, Any] = {"type": "web_search_preview"}
        if context_size := web_search.get("context_size"):
            tool["search_context_size"] = context_size
        if user_location := web_search.get("user_location"):
            tool["user_location"] = user_location
        tool.update(web_search.get("metadata") or {})
        tools.append(tool)

    def _build_web_search_diagnostics(
        self,
        options: dict,
        references: list[WebSearchReference],
        *,
        fallback_reason: str | None = None,
    ) -> WebSearchDiagnostics | None:
        web_search = self._web_search_options(options)
        enabled = self._web_search_enabled(options)
        if not enabled and not references:
            return None
        return WebSearchDiagnostics(
            enabled=enabled,
            strategy=str(web_search.get("strategy", "off")),
            provider=self.config.provider,
            status="completed"
            if references
            else ("enabled_no_references" if enabled else "references_only"),
            fallback_reason=fallback_reason,
            metadata={"reference_count": len(references)},
        )

    @staticmethod
    def _convert_tools_to_responses(tools: list[dict]) -> list[dict]:
        """
        将 Chat Completions 格式的工具定义转换为 Responses API 格式

        Chat Completions 格式（外部标记多态）:
          {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}

        Responses API 格式（内部标记多态）:
          {"type": "function", "name": "...", "description": "...", "parameters": {...}}
        """
        responses_tools = []
        for tool in tools:
            if "function" in tool:
                # 从 Chat Completions 格式转换
                func_def = tool["function"]
                responses_tool: dict = {
                    "type": "function",
                    "name": func_def.get("name", ""),
                    "description": func_def.get("description", ""),
                    "parameters": func_def.get("parameters", {}),
                }
                # 保留 strict 设置
                if "strict" in func_def:
                    responses_tool["strict"] = func_def["strict"]
                responses_tools.append(responses_tool)
            elif "name" in tool and tool.get("type") == "function":
                # 已经是 Responses 格式
                responses_tools.append(tool)
            else:
                # 未知格式，尝试作为 Responses 格式使用
                responses_tools.append(tool)
        return responses_tools
