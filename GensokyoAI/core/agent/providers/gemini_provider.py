"""Google Gemini Provider 实现

支持 Google Gemini 系列模型 API。
"""

# GensokyoAI/core/agent/providers/gemini_provider.py

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import TYPE_CHECKING, Any, cast

from ....utils.logger import logger
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


class GeminiProvider(BaseProvider):
    """
    Google Gemini Provider

    使用 google-genai SDK 调用 Gemini 系列模型。
    注意：Gemini 的消息角色和格式与 OpenAI 有所不同。
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self._client = self._build_client()
        logger.debug(f"GeminiProvider 初始化完成，model: {config.name}")

    @property
    def capabilities(self) -> set[str]:
        """Gemini Provider 能力声明。"""
        return self.apply_model_capability_overrides(
            {
                ProviderCapability.CHAT,
                ProviderCapability.STREAM,
                ProviderCapability.TOOLS,
                ProviderCapability.EMBEDDINGS,
                ProviderCapability.VISION,
                ProviderCapability.REASONING,
                ProviderCapability.WEB_SEARCH,
                ProviderCapability.STRUCTURED_OUTPUT,
            }
        )

    @classmethod
    def _load_genai_module(cls) -> Any:
        """动态加载 Gemini SDK，避免可选依赖缺失时触发静态导入告警。"""
        return cls.import_optional_dependency(
            "google.genai",
            "使用 Gemini Provider 需要安装 google-genai 包: pip install google-genai\n"
            "或者: pip install gensokyoai[gemini]",
        )

    @classmethod
    def _load_genai_types(cls) -> Any:
        """动态加载 google.genai.types，统一可选依赖边界。"""
        return cls._load_genai_module().types

    def _build_client(self):
        """构建 Gemini 客户端"""
        genai = self._load_genai_module()
        return genai.Client(api_key=self.config.api_key)

    @staticmethod
    def _response_format_to_gemini(response_format: dict) -> dict:
        """将统一 response_format 转换为 google-genai response_format。"""
        if response_format.get("type") == "json_schema":
            json_schema = response_format.get("json_schema")
            if isinstance(json_schema, dict) and isinstance(json_schema.get("schema"), dict):
                return {
                    "text": {
                        "mime_type": "application/json",
                        "schema": json_schema["schema"],
                    }
                }
        if response_format.get("type") == "json_object":
            return {"text": {"mime_type": "application/json"}}
        return response_format

    def _build_content_config(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        options: dict | None,
        genai_types,
    ):
        """组装 GenerateContentConfig 与消息体（chat 与 chat_stream 共用）。"""
        options = options or {}
        system_instruction, gemini_contents = self._convert_messages(messages)

        config_kwargs: dict = {
            "temperature": options.get("temperature", 0.7),
            "top_p": options.get("top_p", 0.9),
        }

        max_tokens = options.get("num_predict") or options.get("max_tokens")
        if max_tokens:
            config_kwargs["max_output_tokens"] = max_tokens

        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        if response_format := options.get("response_format"):
            config_kwargs["response_format"] = self._response_format_to_gemini(response_format)

        gemini_tools = self._convert_tools_to_gemini(tools) if tools else []
        self._inject_google_search_tool(gemini_tools, options, genai_types)
        if gemini_tools:
            config_kwargs["tools"] = gemini_tools

        return genai_types.GenerateContentConfig(**config_kwargs), gemini_contents

    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        """非流式调用 Gemini API"""
        genai_types = self._load_genai_types()
        config, gemini_contents = self._build_content_config(messages, tools, options, genai_types)

        response = await self._client.aio.models.generate_content(
            model=model,
            contents=gemini_contents,
            config=config,
        )

        return self._convert_response(response, model)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用 Gemini API"""
        options = options or {}
        genai_types = self._load_genai_types()
        config, gemini_contents = self._build_content_config(messages, tools, options, genai_types)

        last_usage: dict[str, Any] | None = None
        async for chunk in self._client.aio.models.generate_content_stream(
            model=model,
            contents=gemini_contents,
            config=config,
        ):
            # usage_metadata 随流式块携带（常在末块），先取再过 candidates 守卫
            usage_meta = getattr(chunk, "usage_metadata", None)
            if usage_meta is not None:
                prompt_tokens = getattr(usage_meta, "prompt_token_count", None)
                completion_tokens = getattr(usage_meta, "candidates_token_count", None)
                if prompt_tokens is not None or completion_tokens is not None:
                    last_usage = {
                        "prompt_tokens": prompt_tokens or 0,
                        "completion_tokens": completion_tokens or 0,
                        "total_tokens": getattr(usage_meta, "total_token_count", None)
                        or (prompt_tokens or 0) + (completion_tokens or 0),
                    }
            if not chunk.candidates:
                continue

            candidate = chunk.candidates[0]
            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if hasattr(part, "text") and part.text:
                        yield StreamChunk(content=part.text)
                    elif hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        unified_msg = UnifiedMessage(
                            role="assistant",
                            content="",
                            tool_calls=[
                                ToolCall(
                                    function=ToolCallFunction(
                                        name=fc.name,
                                        arguments=dict(fc.args) if fc.args else {},
                                    )
                                )
                            ],
                        )
                        yield StreamChunk(
                            type="tool_call",
                            is_tool_call=True,
                            tool_info={"message": unified_msg},
                            finish_reason="tool_calls",
                        )

            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason:
                references = self._extract_web_search_references(chunk)
                yield StreamChunk(
                    type="finish",
                    finish_reason=str(finish_reason),
                    usage=last_usage,
                    web_search_references=references,
                    web_search_diagnostics=self._build_web_search_diagnostics(options, references),
                )

    async def list_models(self) -> list[ModelInfo]:
        """列出 Gemini 模型。"""
        try:
            response = await self._client.aio.models.list()
        except Exception as e:
            logger.warning(f"拉取 Gemini 模型列表失败，将返回当前配置模型作为 fallback: {e}")
            return [
                ModelInfo(
                    id=self.config.name,
                    name=self.config.name,
                    capabilities=sorted(self._infer_model_capabilities(self.config.name, {})),
                    metadata={"fallback": True},
                )
            ]

        items: Any = getattr(response, "models", response) or []
        models: list[ModelInfo] = []
        if isinstance(items, AsyncIterable):
            async for item in items:
                model_id = getattr(item, "name", "") or getattr(item, "id", "")
                if model_id:
                    metadata = item.model_dump() if hasattr(item, "model_dump") else {}
                    models.append(
                        ModelInfo(
                            id=model_id,
                            name=model_id,
                            capabilities=sorted(self._infer_model_capabilities(model_id, metadata)),
                            metadata=metadata,
                        )
                    )
        else:
            for item in cast(Iterable[Any], items):
                model_id = getattr(item, "name", "") or getattr(item, "id", "")
                if model_id:
                    metadata = item.model_dump() if hasattr(item, "model_dump") else {}
                    models.append(
                        ModelInfo(
                            id=model_id,
                            name=model_id,
                            capabilities=sorted(self._infer_model_capabilities(model_id, metadata)),
                            metadata=metadata,
                        )
                    )
        return models

    def _infer_model_capabilities(self, model_id: str, metadata: dict[str, Any]) -> set[str]:
        """基于 Gemini 模型名和元数据推断模型能力。"""
        capabilities = set(self.capabilities)
        normalized_id = model_id.lower()
        tokens = OpenAIProvider._metadata_capability_tokens(metadata)
        if OpenAIProvider._metadata_or_model_indicates_web_search(normalized_id, tokens):
            capabilities.add(ProviderCapability.WEB_SEARCH)
        return self.apply_model_capability_overrides(capabilities)

    async def embeddings(
        self,
        model: str,
        prompt: str,
        **kwargs,
    ) -> UnifiedEmbeddingResponse:
        """获取文本向量"""
        response = await self._client.aio.models.embed_content(
            model=model,
            contents=prompt,
        )

        return UnifiedEmbeddingResponse(
            embedding=list(response.embeddings[0].values) if response.embeddings else [],
            model=model,
        )

    # ==================== 转换工具方法 ====================

    @staticmethod
    def _image_to_part(image: ImageInput | dict | Any) -> dict:
        """将统一图片输入转换为 Gemini inline_data/file_data part。"""
        url = image.get("url") if isinstance(image, dict) else getattr(image, "url", None)
        data = image.get("data") if isinstance(image, dict) else getattr(image, "data", None)
        mime_type = (
            image.get("mime_type") if isinstance(image, dict) else getattr(image, "mime_type", None)
        ) or "image/png"
        if data:
            return {"inline_data": {"mime_type": mime_type, "data": data}}
        if url:
            return {"file_data": {"mime_type": mime_type, "file_uri": url}}
        return {"text": ""}

    @staticmethod
    def _convert_content_parts(content: Any) -> list[dict]:
        """将统一多模态 content parts 转换为 Gemini parts。"""
        if not isinstance(content, list):
            return [{"text": str(content)}]

        parts: list[dict] = []
        for part in content:
            part_type = (
                part.get("type") if isinstance(part, dict) else getattr(part, "type", "text")
            )
            if part_type == "text":
                text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if text is not None:
                    parts.append({"text": str(text)})
                continue

            image = part.get("image") if isinstance(part, dict) else getattr(part, "image", None)
            if part_type == "image" and image:
                parts.append(GeminiProvider._image_to_part(image))
        return parts or [{"text": ""}]

    @staticmethod
    def _convert_messages(messages: list[dict]) -> tuple[str, list]:
        """
        将 OpenAI 格式的消息转换为 Gemini 格式

        Gemini 使用 "user" 和 "model" 角色（不是 "assistant"）
        system 消息需要作为 system_instruction 单独传入
        """
        system_parts = []
        gemini_contents = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            parts = GeminiProvider._convert_content_parts(content)

            if role == "system":
                system_parts.append(
                    "".join(part.get("text", "") for part in parts if "text" in part)
                )
            elif role == "assistant":
                gemini_contents.append({"role": "model", "parts": parts})
            elif role == "tool":
                # Gemini 的工具结果格式
                tool_text = content if isinstance(content, str) else str(content)
                gemini_contents.append(
                    {"role": "user", "parts": [{"text": f"[工具结果] {tool_text}"}]}
                )
            else:
                gemini_contents.append({"role": "user", "parts": parts})

        system_instruction = "\n\n".join(system_parts) if system_parts else ""

        # Gemini 要求交替的 user/model 消息，合并连续的同角色消息
        gemini_contents = GeminiProvider._merge_consecutive_roles(gemini_contents)

        return system_instruction, gemini_contents

    @staticmethod
    def _merge_consecutive_roles(contents: list) -> list:
        """合并连续相同角色的消息"""
        if not contents:
            return contents

        merged = [contents[0]]
        for msg in contents[1:]:
            if msg["role"] == merged[-1]["role"]:
                # 合并 parts
                merged[-1]["parts"].extend(msg["parts"])
            else:
                merged.append(msg)
        return merged

    def _convert_response(self, response, model: str) -> UnifiedResponse:
        """将 Gemini GenerateContentResponse 转换为 UnifiedResponse"""
        if not response.candidates:
            return UnifiedResponse(model=model)

        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            return UnifiedResponse(model=model)

        content_parts = []
        tool_calls = []

        for part in candidate.content.parts:
            if hasattr(part, "text") and part.text:
                content_parts.append(part.text)
            elif hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                tool_calls.append(
                    ToolCall(
                        function=ToolCallFunction(
                            name=fc.name,
                            arguments=dict(fc.args) if fc.args else {},
                        )
                    )
                )

        references = self._extract_web_search_references(response)
        # Gemini 的 token 计数在 response.usage_metadata（snake_case 字段）
        usage = None
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            usage = {
                "prompt_tokens": getattr(meta, "prompt_token_count", None),
                "completion_tokens": getattr(meta, "candidates_token_count", None),
                "total_tokens": getattr(meta, "total_token_count", None),
            }
            if not any(usage.values()):
                usage = None
        return UnifiedResponse(
            message=UnifiedMessage(
                role="assistant",
                content="".join(content_parts),
                tool_calls=tool_calls if tool_calls else None,
            ),
            model=model,
            done=True,
            web_search_references=references,
            web_search_diagnostics=self._build_web_search_diagnostics({}, references),
            usage=usage,
        )

    @staticmethod
    def _object_to_dict(value: Any) -> dict[str, Any]:
        """将 Gemini SDK 对象尽量转为 dict。"""
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
            "grounding_metadata",
            "grounding_chunks",
            "grounding_supports",
            "web",
            "uri",
            "title",
            "text",
            "segment",
            "metadata",
        ):
            if hasattr(value, key):
                result[key] = getattr(value, key)
        return result

    @classmethod
    def _extract_web_search_references(cls, response: Any) -> list[WebSearchReference]:
        """从 Gemini grounding_metadata 中提取统一搜索引用。"""
        references: list[WebSearchReference] = []
        seen: set[str] = set()
        candidates = getattr(response, "candidates", []) or []
        for candidate in candidates:
            grounding = getattr(candidate, "grounding_metadata", None)
            grounding_data = cls._object_to_dict(grounding)
            for chunk in grounding_data.get("grounding_chunks", []) or []:
                chunk_data = cls._object_to_dict(chunk)
                web_data = cls._object_to_dict(chunk_data.get("web"))
                url = str(web_data.get("uri") or web_data.get("url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                references.append(
                    WebSearchReference(
                        title=str(web_data.get("title") or url),
                        url=url,
                        source="gemini_grounding",
                        metadata={"chunk": chunk_data},
                    )
                )
        return references

    @staticmethod
    def _web_search_options(options: dict) -> dict[str, Any]:
        raw = options.get("web_search") or {}
        return dict(raw) if isinstance(raw, dict) else {"enabled": bool(raw)}

    def _web_search_enabled(self, options: dict) -> bool:
        web_search = self._web_search_options(options)
        strategy = web_search.get("strategy", "off")
        return bool(web_search.get("enabled")) and strategy != "off"

    def _inject_google_search_tool(self, tools: list, options: dict, genai_types: Any) -> None:
        """按显式配置向 Gemini tools 注入 Google Search grounding。"""
        if not self._web_search_enabled(options):
            return
        for tool in tools:
            if (
                getattr(tool, "google_search", None) is not None
                or getattr(tool, "google_search_retrieval", None) is not None
            ):
                return
        try:
            tools.append(genai_types.Tool(google_search=genai_types.GoogleSearch()))
        except Exception:
            try:
                tools.append(genai_types.Tool(google_search_retrieval={}))
            except Exception:
                tools.append({"google_search": {}})

    def _build_web_search_diagnostics(
        self,
        options: dict,
        references: list[WebSearchReference],
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
            metadata={"reference_count": len(references)},
        )

    @staticmethod
    def _convert_tools_to_gemini(tools: list[dict]) -> list:
        """
        将 Ollama/OpenAI 格式的工具定义转换为 Gemini 格式

        Gemini 使用 function_declarations
        """
        try:
            genai_types = GeminiProvider._load_genai_types()
        except ImportError:
            return []

        declarations = []
        for tool in tools:
            if "function" in tool:
                func = tool["function"]
                declarations.append(
                    genai_types.FunctionDeclaration(
                        name=func.get("name", ""),
                        description=func.get("description", ""),
                        parameters=func.get("parameters"),
                    )
                )

        if declarations:
            return [genai_types.Tool(function_declarations=declarations)]
        return []
