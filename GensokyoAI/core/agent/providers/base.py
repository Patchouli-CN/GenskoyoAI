"""LLM Provider 抽象基类"""

# GensokyoAI/core/agent/providers/base.py

import importlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ....utils.request_utils import merge_headers
from ..types import ProviderCapability
from .auth_utils import TokenRefreshManager

if TYPE_CHECKING:
    from ...config import ModelConfig
    from ..types import (
        ImageGenerationRequest,
        ImageGenerationResult,
        ModelInfo,
        StreamChunk,
        UnifiedEmbeddingResponse,
        UnifiedResponse,
    )


class BaseProvider(ABC):
    """
    LLM Provider 抽象基类

    所有 Provider 必须实现此接口，将各自 API 的响应转换为统一类型。

    紫：「边界是幻想乡的秩序，Provider 是 LLM 的边界。」
    """

    def __init__(self, config: ModelConfig):
        self.config = config
        self._token_manager = TokenRefreshManager(config.auth) if config.auth else None

    @property
    def capabilities(self) -> set[str]:
        """Provider 级能力声明。"""
        return {ProviderCapability.CHAT, ProviderCapability.STREAM}

    def supports(self, capability: str) -> bool:
        """检查 Provider 是否声明支持指定能力，兼容常见能力别名。"""
        normalized = ProviderCapability.normalize_name(capability)
        return normalized in self.apply_model_capability_overrides(self.capabilities)

    def apply_model_capability_overrides(self, capabilities: set[str]) -> set[str]:
        """应用配置中的模型能力增删覆盖，修正远端元数据或启发式推断误差。"""
        result = ProviderCapability.normalize(capabilities)
        result.update(ProviderCapability.normalize(self.config.model_capabilities_add or []))
        result.difference_update(
            ProviderCapability.normalize(self.config.model_capabilities_remove or [])
        )
        return result

    @staticmethod
    def import_optional_dependency(module_name: str, install_hint: str) -> Any:
        """动态导入可选 SDK，避免未安装 extra 时触发静态导入告警。"""
        try:
            return importlib.import_module(module_name)
        except ImportError as exc:
            raise ImportError(install_hint) from exc

    async def list_models(self) -> list[ModelInfo]:
        """列出 Provider 可用模型；默认无远程模型列表。"""
        return []

    async def get_quota(self) -> dict[str, Any] | None:
        """查询账户额度（可选能力）。

        不支持的 Provider 返回 None；返回结构由具体 Provider 定义
        （如 Moonshot 的 available_balance / voucher_balance / cash_balance）。
        """
        return None

    async def prepare_auth(self, *, force_refresh: bool = False) -> None:
        """准备认证信息；默认支持 OAuth/Bearer token refresh。"""
        if self._token_manager:
            before = self.config.auth.access_token if self.config.auth else None
            await self._token_manager.ensure_token(force=force_refresh)
            after = self.config.auth.access_token if self.config.auth else None
            if after and after != before:
                self._rebuild_auth_client()

    def _rebuild_auth_client(self) -> None:
        """认证 token 刷新后重建底层客户端；需要的 Provider 可覆写。"""
        return None

    def auth_headers(self) -> dict[str, str]:
        """返回当前 Provider 可用的认证 headers。"""
        if not self._token_manager:
            return {}
        return self._token_manager.auth_headers()

    def merged_headers(self, *headers: dict | None) -> dict[str, str]:
        """合并配置 headers 与动态认证 headers。"""
        return merge_headers(self.config.extra_headers, self.auth_headers(), *headers)

    @abstractmethod
    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        """
        非流式对话

        Args:
            model: 模型名称
            messages: 消息列表 [{"role": "user", "content": "..."}]
            tools: 工具定义列表（可选）
            options: 模型选项（temperature, top_p 等）
            **kwargs: 额外参数（如 think 等）

        Returns:
            UnifiedResponse: 统一响应
        """
        ...

    @abstractmethod
    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """
        流式对话

        Args:
            model: 模型名称
            messages: 消息列表
            tools: 工具定义列表（可选）
            options: 模型选项
            **kwargs: 额外参数

        Yields:
            StreamChunk: 流式响应块
        """
        ...

    async def image_generation(
        self,
        request: ImageGenerationRequest,
        **kwargs,
    ) -> ImageGenerationResult:
        """图片生成；默认不支持。"""
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 image_generation")

    async def embeddings(
        self,
        model: str,
        prompt: str,
        **kwargs,
    ) -> UnifiedEmbeddingResponse:
        """
        文本向量化

        Args:
            model: 模型名称
            prompt: 要向量化的文本
            **kwargs: 额外参数

        Returns:
            UnifiedEmbeddingResponse: 统一 embedding 响应

        Raises:
            NotImplementedError: 如果 Provider 不支持 embeddings
        """
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 embeddings")

    async def embeddings_batch(
        self,
        model: str,
        prompts: list[str],
        **kwargs,
    ) -> list[UnifiedEmbeddingResponse]:
        """
        批量文本向量化。

        默认实现为串行/并行单条调用；支持原生批量 API 的 Provider 可覆写此方法
        以获得更高吞吐。

        Args:
            model: 模型名称
            prompts: 要向量化的文本列表
            **kwargs: 额外参数

        Returns:
            list[UnifiedEmbeddingResponse]: 统一 embedding 响应列表

        Raises:
            NotImplementedError: 如果 Provider 不支持 embeddings
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 embeddings_batch，请使用 embeddings 单条调用"
        )

    def update_config(self, config: ModelConfig) -> None:
        """更新配置"""
        self.config = config
        self._token_manager = TokenRefreshManager(config.auth) if config.auth else None
