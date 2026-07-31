"""Ollama Provider 实现"""

# GensokyoAI/core/agent/providers/ollama_provider.py

import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ....utils.logger import logger
from ..types import (
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


class OllamaProvider(BaseProvider):
    """
    Ollama Provider - 封装 Ollama 异步调用

    保持与原始 ModelClient 完全一致的行为
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self._client = self._build_client()
        logger.debug(f"OllamaProvider 初始化完成，base_url: {config.base_url}")

    @property
    def capabilities(self) -> set[str]:
        """Ollama Provider 能力声明。"""
        return self.apply_model_capability_overrides(
            {
                ProviderCapability.CHAT,
                ProviderCapability.STREAM,
                ProviderCapability.TOOLS,
                ProviderCapability.EMBEDDINGS,
                ProviderCapability.CUSTOM_ENDPOINT,
            }
        )

    def _build_client(self):
        """构建 Ollama 异步客户端"""
        ollama_module = self.import_optional_dependency(
            "ollama",
            "使用 Ollama Provider 需要安装 ollama 包: pip install ollama\n"
            "或者: pip install gensokyoai[ollama]",
        )
        OllamaAsyncClient = ollama_module.AsyncClient

        # 根据配置决定是否使用代理
        if not self.config.use_proxy:
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("HTTPS_PROXY", None)
            os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
            logger.debug("已禁用代理，localhost 将直连")
        else:
            logger.debug(f"使用代理模式，base_url: {self.config.base_url}")

        return OllamaAsyncClient(host=self.config.base_url)

    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        """非流式调用 Ollama"""
        call_kwargs = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "options": options or {},
            "stream": False,
        }

        # 处理 think 参数
        if kwargs.get("think"):
            model_lower = model.lower()
            if "deepseek" in model_lower or "r1" in model_lower:
                call_kwargs["think"] = True

        response = await self._client.chat(**call_kwargs)

        # 转换为统一类型
        return self._convert_response(response)

    async def chat_stream(  # type: ignore
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用 Ollama"""
        call_kwargs = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "options": options or {},
            "stream": True,
        }

        # 处理 think 参数
        if kwargs.get("think"):
            model_lower = model.lower()
            if "deepseek" in model_lower or "r1" in model_lower:
                call_kwargs["think"] = True

        stream = await self._client.chat(**call_kwargs)

        async for chunk in stream:
            message = chunk.message

            if message.tool_calls:
                # 转换工具调用信息
                tool_calls = self._convert_tool_calls(message.tool_calls)
                unified_msg = UnifiedMessage(
                    role="assistant",
                    content=message.content or "",
                    tool_calls=tool_calls,
                )
                yield StreamChunk(
                    type="tool_call",
                    is_tool_call=True,
                    tool_info={"message": unified_msg},
                    finish_reason="tool_calls",
                )
            elif message.content:
                yield StreamChunk(content=message.content)

            done = bool(getattr(chunk, "done", False))
            if done:
                yield StreamChunk(
                    type="finish",
                    finish_reason=getattr(chunk, "done_reason", None) or "stop",
                )

    async def list_models(self) -> list[ModelInfo]:
        """列出本地 Ollama 模型。"""
        try:
            response = await self._client.list()
        except Exception as e:
            logger.warning(f"拉取 Ollama 模型列表失败，将返回当前配置模型作为 fallback: {e}")
            return [
                ModelInfo(
                    id=self.config.name,
                    name=self.config.name,
                    capabilities=sorted(self.capabilities),
                    metadata={"fallback": True},
                )
            ]

        items = (
            getattr(response, "models", None) or response.get("models", [])
            if isinstance(response, dict)
            else []
        )
        models: list[ModelInfo] = []
        for item in items:
            name = getattr(item, "model", None) or getattr(item, "name", None)
            if isinstance(item, dict):
                name = item.get("model") or item.get("name")
            if not name:
                continue
            metadata = item if isinstance(item, dict) else getattr(item, "__dict__", {})
            models.append(
                ModelInfo(
                    id=name,
                    name=name,
                    capabilities=sorted(self.capabilities),
                    metadata=dict(metadata) if isinstance(metadata, dict) else {},
                )
            )
        return models

    async def embeddings(
        self,
        model: str,
        prompt: str,
        **kwargs,
    ) -> UnifiedEmbeddingResponse:
        """获取文本向量"""
        response = await self._client.embeddings(
            model=model,
            prompt=prompt,
            **kwargs,
        )

        return UnifiedEmbeddingResponse(
            embedding=response.embedding,
            model=model,
        )

    # ==================== 转换工具方法 ====================

    def _convert_response(self, response) -> UnifiedResponse:
        """将 ollama.ChatResponse 转换为 UnifiedResponse"""
        message = response.message
        tool_calls = None

        if message.tool_calls:
            tool_calls = self._convert_tool_calls(message.tool_calls)

        return UnifiedResponse(
            message=UnifiedMessage(
                role=message.role or "assistant",
                content=message.content or "",
                tool_calls=tool_calls,
            ),
            model=response.model if hasattr(response, "model") else "",
            done=True,
        )

    @staticmethod
    def _convert_tool_calls(ollama_tool_calls) -> list[ToolCall]:
        """将 ollama 的 tool_calls 转换为统一格式"""
        result = []
        for tc in ollama_tool_calls:
            result.append(
                ToolCall(
                    function=ToolCallFunction(
                        name=tc.function.name,
                        arguments=tc.function.arguments or {},
                        provider="ollama",
                    )
                )
            )
        return result
