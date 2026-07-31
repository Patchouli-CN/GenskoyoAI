"""OpenAI 兼容 Provider 实现

支持所有 OpenAI 兼容 API，包括：
- OpenAI 官方
- Deepseek
- SiliconFlow
- vLLM
- Groq
- 本地 llama.cpp server
- 任何 OpenAI 兼容的第三方服务
"""

# GensokyoAI/core/agent/providers/openai_provider.py

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse
from urllib.request import url2pathname

from ....utils.logger import logger
from ....utils.request_utils import (
    endpoint_url,
    has_arbitrary_api_path,
    normalize_openai_api_host_and_path,
    post_json,
    post_sse,
    sdk_base_url_for_endpoint,
)
from ..types import (
    GeneratedImage,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageInput,
    ModelInfo,
    ProviderCapability,
    StreamChunk,
    ToolCall,
    ToolCallFunction,
    UnifiedEmbeddingResponse,
    UnifiedMessage,
    UnifiedResponse,
)
from .base import BaseProvider

if TYPE_CHECKING:
    from ...config import ModelConfig


class OpenAIProvider(BaseProvider):
    """
    OpenAI 兼容 Provider

    使用 openai SDK 调用所有兼容 OpenAI Chat Completions API 的服务。
    通过 base_url 配置可以指向任何兼容端点。
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self._endpoint = normalize_openai_api_host_and_path(config.base_url, config.api_path)
        self._client = self._build_client()
        logger.debug(
            f"OpenAIProvider 初始化完成，base_url: {self._endpoint.api_host}, "
            f"api_path: {self._endpoint.api_path}, model: {config.name}"
        )

    @property
    def capabilities(self) -> set[str]:
        """OpenAI 兼容 Provider 能力声明。

        第三方 OpenAI-compatible endpoint 默认只声明通用文本、工具、embedding 和自定义端点能力；
        官方 OpenAI 端点保留图片输入与图片生成声明，第三方服务可通过模型元数据或配置覆盖补充。
        """
        capabilities = {
            ProviderCapability.CHAT,
            ProviderCapability.STREAM,
            ProviderCapability.TOOLS,
            ProviderCapability.EMBEDDINGS,
            ProviderCapability.CUSTOM_ENDPOINT,
        }
        if self._is_official_openai_endpoint():
            capabilities.update(
                {
                    ProviderCapability.IMAGE,
                    ProviderCapability.IMAGE_GENERATION,
                    ProviderCapability.STRUCTURED_OUTPUT,
                }
            )
        return self.apply_model_capability_overrides(capabilities)

    def _is_official_openai_endpoint(self) -> bool:
        """判断当前配置是否指向 OpenAI 官方 API 端点。"""
        endpoint = getattr(self, "_endpoint", None)
        api_host = (endpoint.api_host if endpoint else self.config.base_url) or ""
        return "api.openai.com" in api_host.lower()

    @property
    def endpoint(self) -> str:
        """当前 Provider 的规范化 API endpoint。"""
        return f"{self._endpoint.api_host}{self._endpoint.api_path}"

    def _uses_custom_http(self) -> bool:
        """当前 api_path 是否需要绕过 SDK 固定 resource path。"""
        endpoint = getattr(self, "_endpoint", None)
        if endpoint is None:
            return False
        return has_arbitrary_api_path(endpoint, "/chat/completions")

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
            "使用 OpenAI Provider 需要安装 openai 包: pip install openai\n"
            "或者: pip install gensokyoai[openai]",
        )
        AsyncOpenAI = openai_module.AsyncOpenAI

        self._endpoint = normalize_openai_api_host_and_path(
            self.config.base_url,
            self.config.api_path,
        )
        kwargs = {}
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        kwargs["base_url"] = sdk_base_url_for_endpoint(self._endpoint, "/chat/completions")
        default_headers = self.merged_headers()
        if default_headers:
            kwargs["default_headers"] = default_headers

        return AsyncOpenAI(**kwargs)

    def _rebuild_auth_client(self) -> None:
        """动态认证 token 更新后重建 OpenAI SDK 客户端。"""
        self._client = self._build_client()

    # ==================== 消息清洗 ====================

    @staticmethod
    def _image_to_url(image: ImageInput | dict | Any) -> str:
        """将统一图片输入转换为 OpenAI image_url 可接受的 URL 或 data URL。"""
        url = image.get("url") if isinstance(image, dict) else getattr(image, "url", None)
        data = image.get("data") if isinstance(image, dict) else getattr(image, "data", None)
        mime_type = (
            image.get("mime_type") if isinstance(image, dict) else getattr(image, "mime_type", None)
        ) or "image/png"
        if url:
            # 本地文件路径 / file:// URL 在发送时读取并转 base64 data URL，避免长期占用内存
            parsed = urlparse(str(url))
            if parsed.scheme == "file":
                local_path = Path(url2pathname(parsed.path))
                if local_path.exists():
                    try:
                        raw = local_path.read_bytes()
                        return f"data:{mime_type};base64,{base64.b64encode(raw).decode('utf-8')}"
                    except Exception as error:
                        logger.warning(f"读取本地图片失败: {local_path}, {error}")
            elif parsed.scheme in ("http", "https"):
                return str(url)
            local_path = Path(str(url))
            if local_path.exists():
                try:
                    raw = local_path.read_bytes()
                    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('utf-8')}"
                except Exception as error:
                    logger.warning(f"读取本地图片失败: {local_path}, {error}")
            return str(url)
        if data:
            return data if str(data).startswith("data:") else f"data:{mime_type};base64,{data}"
        return ""

    @staticmethod
    def _convert_content_parts(content: Any) -> Any:
        """将统一多模态 content parts 转换为 OpenAI Chat Completions 格式。"""
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
                    converted.append({"type": "text", "text": str(text)})
                continue

            image = part.get("image") if isinstance(part, dict) else getattr(part, "image", None)
            if part_type == "image" and image:
                image_url = OpenAIProvider._image_to_url(image)
                if not image_url:
                    continue
                image_payload: dict[str, Any] = {"url": image_url}
                detail = (
                    image.get("detail")
                    if isinstance(image, dict)
                    else getattr(image, "detail", None)
                )
                if detail:
                    image_payload["detail"] = detail
                converted.append({"type": "image_url", "image_url": image_payload})
        return converted

    @staticmethod
    def _clean_messages(messages: list[dict]) -> list[dict]:
        """
        递归清洗消息列表，移除所有 V4/V3 特有的 reasoning_content 字段，
        并将统一多模态 content parts 转换为 OpenAI Chat Completions 格式。
        """

        def _clean(item):
            if isinstance(item, dict):
                cleaned = {
                    key: _clean(value) for key, value in item.items() if key != "reasoning_content"
                }
                if "content" in cleaned:
                    cleaned["content"] = OpenAIProvider._convert_content_parts(cleaned["content"])
                return cleaned
            if isinstance(item, list):
                return [_clean(sub) for sub in item]
            return item

        return cast(list[dict], _clean(messages))

    # ==================== 核心 API ====================

    def _build_call_kwargs(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        options: dict | None,
        *,
        stream: bool = False,
        extra_body: dict | None = None,
    ) -> dict:
        """组装 chat.completions.create 的共用请求参数（chat 与 chat_stream 共用）。"""
        options = options or {}

        call_kwargs: dict = {
            "model": model,
            "messages": self._clean_messages(messages),
            "temperature": options.get("temperature", 0.7),
            "top_p": options.get("top_p", 0.9),
        }
        if stream:
            call_kwargs["stream"] = True

        # 应用 extra_body（如 thinking 模式控制）
        if extra_body:
            call_kwargs["extra_body"] = extra_body

        # max_tokens 映射：优先使用 max_completion_tokens（新版 API 推荐），回退到 max_tokens
        max_tokens = (
            options.get("max_completion_tokens")
            or options.get("num_predict")
            or options.get("max_tokens")
        )
        if max_tokens:
            call_kwargs["max_completion_tokens"] = max_tokens

        if response_format := options.get("response_format"):
            call_kwargs["response_format"] = response_format

        # 工具支持
        if tools:
            call_kwargs["tools"] = self._convert_tools_to_openai(tools)
            if tool_choice := options.get("tool_choice"):
                call_kwargs["tool_choice"] = tool_choice

        return call_kwargs

    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        """非流式调用 OpenAI 兼容 API"""
        call_kwargs = self._build_call_kwargs(model, messages, tools, options)

        if self._uses_custom_http():
            response = await post_json(
                endpoint_url(self._endpoint),
                call_kwargs,
                self._request_headers(),
                self.config.timeout,
            )
            return self._convert_response_dict(response)

        response = await self._client.chat.completions.create(**call_kwargs)

        return self._convert_response(response)

    async def list_models(self) -> list[ModelInfo]:
        """拉取 OpenAI 兼容 `/models` 并转换为统一模型元信息。"""
        try:
            response = await self._client.models.list()
        except Exception as e:
            logger.warning(f"拉取模型列表失败，将返回当前配置模型作为 fallback: {e}")
            return [
                ModelInfo(
                    id=self.config.name,
                    name=self.config.name,
                    capabilities=sorted(self.apply_model_capability_overrides(self.capabilities)),
                    metadata={"fallback": True},
                )
            ]

        models: list[ModelInfo] = []
        for item in getattr(response, "data", []) or []:
            model_id = getattr(item, "id", "") or ""
            if not model_id:
                continue
            metadata = self._model_item_to_metadata(item)
            context_window = self._extract_context_window(item, metadata)
            capabilities = self._infer_model_capabilities(item, metadata)
            models.append(
                ModelInfo(
                    id=model_id,
                    name=model_id,
                    context_window=context_window,
                    capabilities=capabilities,
                    owned_by=getattr(item, "owned_by", None),
                    metadata=metadata,
                )
            )
        return models

    async def chat_stream(  # type: ignore
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        extra_body: dict | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用 OpenAI 兼容 API"""
        call_kwargs = self._build_call_kwargs(
            model, messages, tools, options, stream=True, extra_body=extra_body
        )

        # 流式工具调用累积器（不存 reasoning_content，防止 V4 要求回传）
        tool_calls_acc: dict[int, dict] = {}

        if self._uses_custom_http():
            async for chunk_data in post_sse(
                endpoint_url(self._endpoint),
                call_kwargs,
                self._request_headers(),
                self.config.timeout,
            ):
                async for stream_chunk in self._convert_stream_chunk_dict(
                    chunk_data, tool_calls_acc
                ):
                    yield stream_chunk
            return

        stream = await self._client.chat.completions.create(**call_kwargs)

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            # 处理工具调用
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": "",
                            "name": "",
                            "arguments": "",
                        }
                    if tc.id:
                        tool_calls_acc[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_acc[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc.function.arguments

            # 处理内容（if 不是 elif，V4 可能同时返回 tool_calls 和 content）
            if delta.content:
                yield StreamChunk(content=delta.content)

            # 检查结束
            finish_reason = chunk.choices[0].finish_reason if chunk.choices else None
            usage = self._usage_to_dict(getattr(chunk, "usage", None))
            if finish_reason and finish_reason != "tool_calls":
                yield StreamChunk(type="finish", finish_reason=finish_reason, usage=usage)
            if finish_reason == "tool_calls" and tool_calls_acc:
                import json

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
                            id=tc_data.get("id", ""),
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
                tool_info: dict[str, Any] = {"message": unified_msg}
                if raw_arguments:
                    tool_info["raw_arguments"] = raw_arguments
                yield StreamChunk(
                    type="tool_call",
                    is_tool_call=True,
                    tool_info=tool_info,
                    finish_reason=finish_reason,
                    usage=usage,
                )

    async def image_generation(
        self,
        request: ImageGenerationRequest,
        **kwargs,
    ) -> ImageGenerationResult:
        """调用 OpenAI Images API 并转换为统一图片生成结果。"""
        call_kwargs: dict[str, Any] = {
            "model": request.model or self.config.name,
            "prompt": request.prompt,
            "n": request.n,
        }
        if request.size:
            call_kwargs["size"] = request.size
        if request.quality:
            call_kwargs["quality"] = request.quality
        if request.style:
            call_kwargs["style"] = request.style
        if request.response_format:
            call_kwargs["response_format"] = request.response_format
        call_kwargs.update(kwargs)

        response = await self._client.images.generate(**call_kwargs)
        images: list[GeneratedImage] = []
        for item in getattr(response, "data", []) or []:
            images.append(
                GeneratedImage(
                    url=getattr(item, "url", None),
                    data=getattr(item, "b64_json", None) or getattr(item, "data", None),
                    mime_type="image/png" if getattr(item, "b64_json", None) else None,
                    revised_prompt=getattr(item, "revised_prompt", None),
                    metadata=item.model_dump() if hasattr(item, "model_dump") else {},
                )
            )
        return ImageGenerationResult(
            images=images,
            model=request.model or self.config.name,
            metadata=response.model_dump() if hasattr(response, "model_dump") else {},
        )

    async def embeddings_batch(
        self,
        model: str,
        prompts: list[str],
        **kwargs,
    ) -> list[UnifiedEmbeddingResponse]:
        """批量获取文本向量，使用 OpenAI 原生批量 API。"""
        if not prompts:
            return []

        embed_kwargs: dict = {
            "model": model,
            "input": prompts,
        }

        if dimensions := kwargs.get("dimensions"):
            embed_kwargs["dimensions"] = dimensions

        if encoding_format := kwargs.get("encoding_format"):
            embed_kwargs["encoding_format"] = encoding_format

        response = await self._client.embeddings.create(**embed_kwargs)

        # OpenAI 返回顺序与 input 顺序一致
        return [
            UnifiedEmbeddingResponse(embedding=item.embedding, model=model)
            for item in response.data
        ]

    async def embeddings(
        self,
        model: str,
        prompt: str,
        **kwargs,
    ) -> UnifiedEmbeddingResponse:
        """获取文本向量"""
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

    # ==================== 转换工具方法 ====================

    def _convert_response_dict(self, response: dict[str, Any]) -> UnifiedResponse:
        """将原始 Chat Completions JSON 响应转换为 UnifiedResponse。"""
        choices = response.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        tool_calls = None
        if message.get("tool_calls"):
            import json

            tool_calls = []
            for tc in message.get("tool_calls") or []:
                function = tc.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(
                        id=tc.get("id") or "",
                        function=ToolCallFunction(
                            name=function.get("name") or "",
                            arguments=args,
                        ),
                    )
                )
        message_content = message.get("content") or ""
        thinking = message.get("reasoning_content")
        return UnifiedResponse(
            message=UnifiedMessage(
                role=message.get("role") or "assistant",
                content=message_content,
                tool_calls=tool_calls,
            ),
            model=response.get("model") or "",
            done=True,
            thinking=thinking,
        )

    async def _convert_stream_chunk_dict(
        self,
        chunk: dict[str, Any],
        tool_calls_acc: dict[int, dict],
    ) -> AsyncIterator[StreamChunk]:
        """将原始 Chat Completions SSE JSON chunk 转换为 StreamChunk。"""
        choices = chunk.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            idx = int(tc.get("index", 0))
            if idx not in tool_calls_acc:
                tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
            if tc.get("id"):
                tool_calls_acc[idx]["id"] = tc.get("id")
            function = tc.get("function") or {}
            if function.get("name"):
                tool_calls_acc[idx]["name"] = function.get("name")
            if function.get("arguments"):
                tool_calls_acc[idx]["arguments"] += function.get("arguments")
        if delta.get("content"):
            yield StreamChunk(content=delta.get("content") or "")
        finish_reason = choice.get("finish_reason")
        usage = self._usage_to_dict(chunk.get("usage"))
        if finish_reason and finish_reason != "tool_calls":
            yield StreamChunk(type="finish", finish_reason=finish_reason, usage=usage)
        if finish_reason == "tool_calls" and tool_calls_acc:
            import json

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
                        id=tc_data.get("id", ""),
                        function=ToolCallFunction(name=tc_data["name"], arguments=args),
                    )
                )
            unified_msg = UnifiedMessage(
                role="assistant", content="", tool_calls=unified_tool_calls
            )
            tool_info: dict[str, Any] = {"message": unified_msg}
            if raw_arguments:
                tool_info["raw_arguments"] = raw_arguments
            yield StreamChunk(
                type="tool_call",
                is_tool_call=True,
                tool_info=tool_info,
                finish_reason=finish_reason,
                usage=usage,
            )

    def _convert_response(self, response) -> UnifiedResponse:
        """将 OpenAI ChatCompletion 转换为 UnifiedResponse"""
        choice = response.choices[0] if response.choices else None
        if not choice:
            return UnifiedResponse(model=response.model or "")

        message = choice.message
        tool_calls = None

        if message.tool_calls:
            import json

            tool_calls = []
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(
                        id=tc.id or "",
                        function=ToolCallFunction(
                            name=tc.function.name or "",
                            arguments=args,
                        ),
                    )
                )

        thinking = None
        # 只记录 reasoning_content 用于调试，不存到 UnifiedMessage 里回传
        if hasattr(message, "reasoning_content") and message.reasoning_content:
            thinking = message.reasoning_content
            logger.debug(f"V4 思维链已记录（长度: {len(thinking)}），不回传")

        return UnifiedResponse(
            message=UnifiedMessage(
                role=message.role or "assistant",
                content=message.content or "",
                tool_calls=tool_calls,
            ),
            model=response.model or "",
            done=True,
            thinking=thinking,
        )

    @staticmethod
    def _model_item_to_metadata(item: Any) -> dict[str, Any]:
        """尽量从 SDK 模型对象提取可序列化元数据。"""
        if hasattr(item, "model_dump"):
            try:
                dumped = item.model_dump()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                pass
        if isinstance(item, dict):
            return dict(item)
        metadata: dict[str, Any] = {}
        for key in (
            "created",
            "owned_by",
            "context_length",
            "input_modalities",
            "output_modalities",
            "modalities",
            "supported_parameters",
            "supported_features",
            "capabilities",
            "tools",
            "pricing",
        ):
            if hasattr(item, key):
                metadata[key] = getattr(item, key)
        return metadata

    @staticmethod
    def _extract_context_window(item: Any, metadata: dict[str, Any]) -> int | None:
        """从 OpenAI/OpenRouter 模型元数据推断上下文窗口。"""
        for key in ("context_length", "context_window", "max_context_length"):
            value = metadata.get(key) or getattr(item, key, None)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    def _infer_model_capabilities(self, item: Any, metadata: dict[str, Any]) -> list[str]:
        """基于 Provider 能力和常见模型元数据推断模型能力。"""
        capabilities = set(self.capabilities)
        metadata_tokens = self._metadata_capability_tokens(metadata)
        modalities = metadata.get("input_modalities") or metadata.get("modalities") or []
        if isinstance(modalities, str):
            modalities = [modalities]
        normalized_modalities = {str(modality).lower() for modality in modalities}
        if "image" in normalized_modalities or "vision" in normalized_modalities:
            capabilities.add(ProviderCapability.VISION)
        pricing = metadata.get("pricing") or {}
        if isinstance(pricing, dict) and pricing.get("internal_reasoning") is not None:
            capabilities.add(ProviderCapability.REASONING)
        model_id = (getattr(item, "id", "") or metadata.get("id", "") or "").lower()
        if any(marker in model_id for marker in ("reason", "r1", "o1", "o3", "o4")):
            capabilities.add(ProviderCapability.REASONING)
        if self._metadata_or_model_indicates_web_search(model_id, metadata_tokens):
            capabilities.add(ProviderCapability.WEB_SEARCH)
        return sorted(self.apply_model_capability_overrides(capabilities))

    @staticmethod
    def _metadata_capability_tokens(metadata: dict[str, Any]) -> set[str]:
        """从远端模型元数据中抽取可用于能力推断的扁平化 token。"""
        tokens: set[str] = set()

        def collect(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, str):
                tokens.add(value.lower())
                return
            if isinstance(value, dict):
                for key, nested in value.items():
                    tokens.add(str(key).lower())
                    collect(nested)
                return
            if isinstance(value, (list, tuple, set)):
                for nested in value:
                    collect(nested)
                return
            tokens.add(str(value).lower())

        for key in (
            "capabilities",
            "supported_features",
            "supported_parameters",
            "tools",
            "tool_types",
            "input_modalities",
            "output_modalities",
            "modalities",
        ):
            collect(metadata.get(key))
        return tokens

    @staticmethod
    def _metadata_or_model_indicates_web_search(model_id: str, tokens: set[str]) -> bool:
        """判断模型 id 或 metadata token 是否指向内置联网搜索能力。"""
        web_search_tokens = {
            "web_search",
            "web-search",
            "web search",
            "web_search_preview",
            "browser_search",
            "internet_search",
            "online_search",
            "grounding",
            "google_search",
        }
        if any(token in tokens for token in web_search_tokens):
            return True
        return any(marker in model_id for marker in ("search", "sonar", "perplexity"))

    @staticmethod
    def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
        """将 SDK usage 对象转换为 dict。"""
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
        result: dict[str, Any] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if hasattr(usage, key):
                result[key] = getattr(usage, key)
        return result or None

    @staticmethod
    def _convert_tools_to_openai(tools: list[dict]) -> list[dict]:
        """
        验证并规范化工具定义为 OpenAI Chat Completions 格式

        期望输入格式（由 ToolDefinition.to_openai_schema() 生成）:
          {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}

        如果输入缺少外层包装，会自动适配。
        """
        openai_tools = []
        for tool in tools:
            if "type" in tool and "function" in tool:
                openai_tools.append(tool)
            else:
                openai_tools.append({"type": "function", "function": tool})
        return openai_tools
