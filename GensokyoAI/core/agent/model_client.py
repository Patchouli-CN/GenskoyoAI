"""模型客户端 - Facade 模式，委托给可插拔的 Provider"""

# GensokyoAI/core/agent/model_client.py

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar, cast

from ...runtime.event_contract import sanitize_event_payload
from ...runtime.resource_control import (
    ResourceGate,
    ResourceLimitError,
    resource_limit_payload,
    resource_scope,
)
from ...utils.logger import logger
from ...utils.request_utils import ModelAPIError, is_retryable_error, normalize_model_error
from ..config import EmbeddingConfig, ModelConfig
from ..events import Event, EventBus, SystemEvent
from ..exceptions import ModelError
from .providers import BaseProvider, ProviderFactory
from .providers.auth_utils import AuthRefreshError, sanitize_auth_data
from .types import (
    ImageGenerationRequest,
    ImageGenerationResult,
    ModelCallTiming,
    ProviderCapability,
    StreamChunk,
    UnifiedEmbeddingResponse,
    UnifiedResponse,
)

_T = TypeVar("_T")


class ModelClient:
    """
    模型客户端 - Facade 层

    对外暴露统一接口，内部委托给具体的 Provider 实现。
    消费方（ThinkEngine、EpisodicMemory 等）只需要和 ModelClient 交互，
    不需要关心底层使用的是 Ollama、OpenAI 还是其他 API。
    """

    def __init__(
        self,
        config: ModelConfig,
        event_bus: EventBus | None = None,
        embedding_config: EmbeddingConfig | None = None,
        resource_gates: dict[str, ResourceGate] | None = None,
    ):
        self.config = config
        self._event_bus = event_bus
        self._resource_gates = resource_gates or {}
        self._provider: BaseProvider = ProviderFactory.create(config)
        self._embedding_provider: BaseProvider | None = None
        self._embedding_config = embedding_config or EmbeddingConfig()
        # 最近模型调用耗时滚动窗口（(context, duration_ms)，供 /status 等观测入口）
        self._latency_samples: deque[tuple[str, float]] = deque(maxlen=50)
        logger.debug(f"ModelClient 初始化完成，Provider: {config.provider}, 模型: {config.name}")

    def _build_options(self) -> dict[str, Any]:
        """构建模型选项"""
        options: dict[str, Any] = {
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
        }
        if self.config.max_tokens > 0:
            options["num_predict"] = self.config.max_tokens
        if self.config.web_search_enabled or self.config.web_search_strategy != "off":
            options["web_search"] = {
                "enabled": self.config.web_search_enabled,
                "strategy": self.config.web_search_strategy,
                "context_size": self.config.web_search_context_size,
                "user_location": dict(self.config.web_search_user_location),
                "allow_fallback": self.config.web_search_allow_fallback,
                "metadata": dict(self.config.web_search_metadata),
            }
        return options

    def _build_embedding_config(self) -> ModelConfig:
        """构建独立的 Embedding Provider 配置。"""
        embedding_config = self._embedding_config
        if not embedding_config.name:
            raise ModelError(
                "未配置 embedding.name，不能使用聊天模型作为 embedding 模型；"
                "请在配置文件的 embedding.name 中指定 embedding 模型"
            )

        return ModelConfig(
            provider=embedding_config.provider or self.config.provider,
            name=embedding_config.name,
            base_url=embedding_config.base_url or self.config.base_url,
            api_key=embedding_config.api_key or self.config.api_key,
            extra_headers=self.config.extra_headers,
            auth=self.config.auth,
            timeout=embedding_config.timeout or self.config.timeout,
            thinking_enabled=False,
            retry_max_attempts=self.config.retry_max_attempts,
            retry_initial_delay=self.config.retry_initial_delay,
            retry_backoff_factor=self.config.retry_backoff_factor,
            retry_status_codes=list(self.config.retry_status_codes),
            use_proxy=(
                embedding_config.use_proxy
                if embedding_config.use_proxy is not None
                else self.config.use_proxy
            ),
        )

    async def get_quota(self) -> dict[str, Any] | None:
        """查询 Provider 账户额度（可选能力，不支持返回 None）。"""
        return await self._provider.get_quota()

    def _get_embedding_provider(self) -> tuple[BaseProvider, ModelConfig]:
        """获取或创建独立 Embedding Provider。"""
        embedding_model_config = self._build_embedding_config()
        if self._embedding_provider is None:
            self._embedding_provider = ProviderFactory.create(embedding_model_config)
            logger.info(
                f"创建 Embedding Provider: {embedding_model_config.provider}, "
                f"模型: {embedding_model_config.name}"
            )
        return self._embedding_provider, embedding_model_config

    def _build_embedding_kwargs(self, kwargs: dict) -> dict:
        """合并配置文件和调用方传入的 embedding 参数。"""
        merged = dict(kwargs)
        if self._embedding_config.dimensions is not None:
            merged.setdefault("dimensions", self._embedding_config.dimensions)
        if self._embedding_config.encoding_format:
            merged.setdefault("encoding_format", self._embedding_config.encoding_format)
        return merged

    def _publish_runtime_model_event(self, event_type: SystemEvent, data: dict) -> None:
        """发布 Runtime 模型状态事件，并统一清洗敏感字段。"""
        if not self._event_bus:
            return
        self._event_bus.publish(
            Event(
                type=event_type,
                source="model_client",
                data=sanitize_event_payload(data),
            )
        )

    def _publish_model_started(self, *, context: str, provider: str, model: str, **data) -> None:
        self._publish_runtime_model_event(
            SystemEvent.MODEL_REQUEST_STARTED,
            {"context": context, "provider": provider, "model": model, **data},
        )

    def _publish_model_completed(self, timing: ModelCallTiming) -> None:
        self._publish_runtime_model_event(
            SystemEvent.MODEL_COMPLETED,
            {
                "context": timing.context,
                "provider": timing.provider,
                "model": timing.model,
                "duration_ms": timing.duration_ms,
                "finish_reason": timing.finish_reason,
                "usage": timing.usage,
            },
        )

    def _publish_model_failed(self, error: Exception, context: dict) -> None:
        data = {
            "model": self.config.name,
            "provider": self.config.provider,
            "error": str(error),
            "error_type": type(error).__name__,
            **context,
        }
        if isinstance(error, ModelAPIError):
            data.update(
                {
                    "status_code": error.status_code,
                    "endpoint": error.endpoint,
                    "retryable": error.retryable,
                }
            )
        self._publish_runtime_model_event(SystemEvent.MODEL_FAILED, data)

    def _publish_error(self, error: Exception, context: dict) -> None:
        """发布错误事件 - 立即通知监听器"""
        if self._event_bus:
            error_str = str(error)
            data = {
                "model": self.config.name,
                "provider": self.config.provider,
                "error": error_str,
                "error_type": type(error).__name__,
                **context,
            }
            if isinstance(error, ModelAPIError):
                data.update(
                    {
                        "status_code": error.status_code,
                        "response_body": error.response_body,
                        "endpoint": error.endpoint,
                        "retryable": error.retryable,
                    }
                )

            cleaned = sanitize_event_payload(data)
            self._event_bus.publish(
                Event(
                    type=SystemEvent.MODEL_ERROR,
                    source="model_client",
                    data=cleaned,
                )
            )
            self._publish_model_failed(error, context)

    def _retry_status_codes(self) -> set[int]:
        """获取当前配置的可重试 HTTP 状态码。"""
        return set(self.config.retry_status_codes or [])

    def update_resource_gates(self, resource_gates: dict[str, ResourceGate] | None) -> None:
        """更新 Runtime 深层资源闸门引用。"""

        self._resource_gates = resource_gates or {}

    def supports(self, capability: str) -> bool:
        """检查当前 Provider/模型配置是否声明支持指定能力。"""
        return self._provider.supports(capability)

    @staticmethod
    def _resource_limit_model_error(error: ResourceLimitError) -> ModelError:
        payload = resource_limit_payload(error)
        model_error = ModelError(payload["technical_message"])
        model_error.error_code = payload["code"]  # type: ignore[attr-defined]
        model_error.user_message = payload["user_message"]  # type: ignore[attr-defined]
        model_error.recoverable = payload["recoverable"]  # type: ignore[attr-defined]
        model_error.action_hint = payload["action_hint"]  # type: ignore[attr-defined]
        model_error.details = payload["details"]  # type: ignore[attr-defined]
        return model_error

    async def _call_with_resource_gates(self, call_factory, *, action: str):
        try:
            async with (
                resource_scope(getattr(self, "_resource_gates", {}).get("provider"), action),
                resource_scope(getattr(self, "_resource_gates", {}).get("model"), action),
            ):
                return await call_factory()
        except ResourceLimitError as error:
            raise self._resource_limit_model_error(error) from error

    @staticmethod
    def _now() -> float:
        """获取单调时钟时间，用于稳定计算调用耗时。"""
        return time.perf_counter()

    @staticmethod
    def _elapsed_ms(start_time: float, now: float | None = None) -> float:
        """计算从 start_time 到 now 的毫秒耗时。"""
        current = now if now is not None else time.perf_counter()
        return round((current - start_time) * 1000, 3)

    def _finish_timing(self, timing: ModelCallTiming) -> ModelCallTiming:
        """补齐 timing 的结束时间和总耗时。"""
        now = self._now()
        timing.end_time = now
        timing.duration_ms = self._elapsed_ms(timing.start_time, now)
        self._latency_samples.append((timing.context, timing.duration_ms))
        return timing

    def latency_stats(self) -> dict[str, Any]:
        """最近模型调用耗时统计（滚动窗口，供 /status 等观测入口）。"""
        samples = [duration for _, duration in self._latency_samples]
        if not samples:
            return {"count": 0}
        return {
            "count": len(samples),
            "avg_ms": round(sum(samples) / len(samples), 1),
            "last_ms": round(samples[-1], 1),
            "max_ms": round(max(samples), 1),
        }

    def _publish_timing(self, timing: ModelCallTiming) -> None:
        """发布模型调用耗时观测事件。"""
        if not self._event_bus:
            return
        self._event_bus.publish(
            Event(
                type=SystemEvent.MODEL_CALL_TIMING,
                source="model_client",
                data={
                    "context": timing.context,
                    "provider": timing.provider,
                    "model": timing.model,
                    "timing": timing,
                    "duration_ms": timing.duration_ms,
                    "first_chunk_ms": timing.first_chunk_ms,
                    "first_token_ms": timing.first_token_ms,
                    "first_reasoning_ms": timing.first_reasoning_ms,
                    "reasoning_chunk_count": timing.reasoning_chunk_count,
                    "reasoning_char_count": timing.reasoning_char_count,
                    "content_chunk_count": timing.content_chunk_count,
                    "content_char_count": timing.content_char_count,
                    "finish_reason": timing.finish_reason,
                    "usage": timing.usage,
                    "metadata": timing.metadata,
                },
            )
        )

    def _publish_auth_event(self, status: str, context: dict) -> None:
        """发布认证刷新事件，自动清洗敏感字段。"""
        if not self._event_bus:
            return
        self._event_bus.publish(
            Event(
                type=SystemEvent.MODEL_AUTH,
                source="model_client",
                data=sanitize_auth_data({"status": status, **context}),
            )
        )

    async def _prepare_provider_auth(
        self,
        provider: BaseProvider,
        *,
        context: str,
        model: str,
        force_refresh: bool = False,
    ) -> None:
        """在调用 Provider 前准备动态认证。"""
        if not getattr(provider, "_token_manager", None):
            return
        self._publish_auth_event(
            "token_refresh_started" if force_refresh else "auth_prepare_started",
            {"context": context, "provider": provider.config.provider, "model": model},
        )
        try:
            await provider.prepare_auth(force_refresh=force_refresh)
        except Exception as e:
            self._publish_auth_event(
                "token_refresh_failed",
                {
                    "context": context,
                    "provider": provider.config.provider,
                    "model": model,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            raise AuthRefreshError(str(e)) from e
        self._publish_auth_event(
            "token_refresh_completed" if force_refresh else "auth_prepare_completed",
            {"context": context, "provider": provider.config.provider, "model": model},
        )

    async def _call_with_retry(
        self,
        call_factory: Callable[[], Awaitable[_T]],
        *,
        context: str,
        model: str,
        provider: BaseProvider | None = None,
        provider_name: str | None = None,
        timeout: float | None = None,
        endpoint: str | None = None,
    ) -> _T:
        """按配置对可重试 API 错误进行指数退避重试。"""
        max_attempts = max(1, self.config.retry_max_attempts)
        delay = max(0.0, self.config.retry_initial_delay)
        backoff = max(1.0, self.config.retry_backoff_factor)
        call_provider = provider or self._provider
        call_provider_name = provider_name or self.config.provider
        retry_status_codes = self._retry_status_codes()
        call_timeout = timeout or self.config.timeout
        call_endpoint = (
            endpoint if endpoint is not None else getattr(call_provider, "endpoint", None)
        )

        auth_refreshed_after_401 = False

        for attempt in range(1, max_attempts + 1):
            try:
                await self._prepare_provider_auth(
                    call_provider,
                    context=context,
                    model=model,
                )
                return await self._call_with_resource_gates(
                    lambda: asyncio.wait_for(call_factory(), timeout=call_timeout),
                    action=context,
                )
            except TimeoutError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if (
                    isinstance(e, ModelError)
                    and getattr(e, "error_code", None) == "resource.limit_exceeded"
                ):
                    raise
                normalized = normalize_model_error(
                    e,
                    provider=call_provider_name,
                    model=model,
                    endpoint=call_endpoint,
                    retry_status_codes=retry_status_codes,
                )
                auth_config = call_provider.config.auth
                if (
                    normalized.status_code == 401
                    and not auth_refreshed_after_401
                    and auth_config is not None
                    and auth_config.allow_401_refresh
                ):
                    auth_refreshed_after_401 = True
                    await self._prepare_provider_auth(
                        call_provider,
                        context=context,
                        model=model,
                        force_refresh=True,
                    )
                    return await self._call_with_resource_gates(
                        lambda: asyncio.wait_for(call_factory(), timeout=call_timeout),
                        action=context,
                    )

                if attempt >= max_attempts or not is_retryable_error(
                    normalized,
                    retry_status_codes,
                ):
                    raise normalized from e

                logger.warning(
                    f"模型 API 调用失败，准备重试 ({attempt + 1}/{max_attempts})，"
                    f"context={context}, status={normalized.status_code}, error={normalized}"
                )
                retry_data = {
                    "model": model,
                    "provider": call_provider_name,
                    "context": context,
                    "status": "retrying",
                    "attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "error": str(normalized),
                    "status_code": normalized.status_code,
                    "endpoint": normalized.endpoint,
                }
                if self._event_bus:
                    cleaned_retry_data = sanitize_event_payload(retry_data)
                    self._event_bus.publish(
                        Event(
                            type=SystemEvent.MODEL_ERROR,
                            source="model_client",
                            data=cleaned_retry_data,
                        )
                    )
                    self._publish_runtime_model_event(
                        SystemEvent.MODEL_RETRY_SCHEDULED,
                        cleaned_retry_data,
                    )
                if delay:
                    await asyncio.sleep(delay)
                    delay *= backoff

        raise ModelError(f"模型 API 调用失败: 已达到最大重试次数 ({max_attempts})")

    async def chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
        model: str | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        """
        非流式调用模型

        Args:
            messages: 消息列表
            tools: 工具定义列表（可选）
            model: 模型名称（可选，默认使用配置中的模型）
            options: 模型选项（可选，默认使用配置构建）
            **kwargs: 额外参数（如 think 等）

        Returns:
            UnifiedResponse: 统一响应

        Raises:
            ModelError: 调用失败或超时
        """
        call_model = model or self.config.name
        call_options = options or self._build_options()

        # 传递 think 参数：True 请求开启思考，False 显式禁用
        # （kimi-k2.5 等模型服务端默认思考，不打 disabled 关不掉）
        if hasattr(self.config, "think"):
            kwargs.setdefault("think", bool(self.config.think))

        timing = ModelCallTiming(
            context="chat",
            provider=self.config.provider,
            model=call_model,
            start_time=self._now(),
            message_count=len(messages),
        )

        try:
            self._publish_model_started(
                context="chat",
                provider=self.config.provider,
                model=call_model,
                message_count=len(messages),
            )
            logger.debug(f"非流式调用模型，消息数: {len(messages)}")
            response = cast(
                UnifiedResponse,
                await self._call_with_retry(
                    lambda: self._provider.chat(
                        model=call_model,
                        messages=messages,
                        tools=tools,
                        options=call_options,
                        **kwargs,
                    ),
                    context="chat",
                    model=call_model,
                ),
            )
            content = response.message.content or ""
            content_text = content if isinstance(content, str) else ""
            reasoning = (
                getattr(response.message, "reasoning_content", None)
                or getattr(response, "thinking", None)
                or ""
            )
            self._finish_timing(timing)
            if content_text:
                timing.content_chunk_count = 1
                timing.content_char_count = len(content_text)
                timing.first_token_ms = timing.duration_ms
            if reasoning:
                timing.reasoning_chunk_count = 1
                timing.reasoning_char_count = len(reasoning)
                timing.first_reasoning_ms = timing.duration_ms
            self._publish_timing(timing)
            if timing.first_token_ms is not None:
                self._publish_runtime_model_event(
                    SystemEvent.MODEL_FIRST_TOKEN,
                    {
                        "context": "chat",
                        "provider": self.config.provider,
                        "model": call_model,
                        "first_token_ms": timing.first_token_ms,
                    },
                )
            self._publish_model_completed(timing)
            logger.debug(f"模型响应完成，长度: {len(content_text)}，耗时: {timing.duration_ms}ms")
            return response

        except TimeoutError as error:
            error_msg = f"模型调用超时 ({self.config.timeout}s)"
            logger.error(error_msg)

            self._publish_error(
                ModelError(error_msg),
                {"context": "chat", "timeout": self.config.timeout, "message_count": len(messages)},
            )
            raise ModelError(error_msg) from error

        except Exception as e:
            if (
                isinstance(e, ModelError)
                and getattr(e, "error_code", None) == "resource.limit_exceeded"
            ):
                raise
            normalized = (
                e
                if isinstance(e, ModelAPIError)
                else normalize_model_error(
                    e,
                    provider=self.config.provider,
                    model=call_model,
                    retry_status_codes=self._retry_status_codes(),
                )
            )
            error_msg = f"模型调用失败: {normalized}"
            logger.opt(colors=False).error(error_msg)

            self._publish_error(
                normalized,
                {"context": "chat", "message_count": len(messages)},
            )
            raise ModelError(error_msg) from e

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
        model: str | None = None,
        options: dict | None = None,
        extra_body: dict | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """
        流式调用模型

        Args:
            messages: 消息列表
            tools: 工具定义列表（可选）
            model: 模型名称（可选，默认使用配置中的模型）
            options: 模型选项（可选，默认使用配置构建）
            **kwargs: 额外参数

        Yields:
            StreamChunk: 流式响应块

        Raises:
            ModelError: 调用失败或超时
        """
        call_model = model or self.config.name
        call_options = options or self._build_options()

        # 传递 think 参数：True 请求开启思考，False 显式禁用
        # （kimi-k2.5 等模型服务端默认思考，不打 disabled 关不掉）
        if hasattr(self.config, "think"):
            kwargs.setdefault("think", bool(self.config.think))

        timing = ModelCallTiming(
            context="chat_stream",
            provider=self.config.provider,
            model=call_model,
            start_time=self._now(),
            message_count=len(messages),
        )
        timing_published = False

        try:
            self._publish_model_started(
                context="chat_stream",
                provider=self.config.provider,
                model=call_model,
                message_count=len(messages),
            )
            logger.debug(f"流式调用模型，消息数: {len(messages)}")
            max_attempts = max(1, self.config.retry_max_attempts)
            delay = max(0.0, self.config.retry_initial_delay)
            backoff = max(1.0, self.config.retry_backoff_factor)

            auth_refreshed_after_401 = False

            for attempt in range(1, max_attempts + 1):
                try:
                    await self._prepare_provider_auth(
                        self._provider,
                        context="chat_stream",
                        model=call_model,
                    )
                    async with (
                        resource_scope(
                            getattr(self, "_resource_gates", {}).get("provider"), "chat_stream"
                        ),
                        resource_scope(
                            getattr(self, "_resource_gates", {}).get("model"), "chat_stream"
                        ),
                    ):
                        stream = self._provider.chat_stream(
                            model=call_model,
                            messages=messages,
                            tools=tools,
                            options=call_options,
                            extra_body=extra_body,
                            **kwargs,
                        ).__aiter__()  # type: ignore
                        while True:
                            try:
                                chunk = await asyncio.wait_for(
                                    anext(stream),
                                    timeout=self.config.timeout,
                                )
                            except StopAsyncIteration:
                                break

                            elapsed_ms = self._elapsed_ms(timing.start_time)
                            if timing.first_chunk_ms is None:
                                timing.first_chunk_ms = elapsed_ms
                            if chunk.content:
                                timing.content_chunk_count += 1
                                timing.content_char_count += len(chunk.content)
                                if timing.first_token_ms is None:
                                    timing.first_token_ms = elapsed_ms
                                    self._publish_runtime_model_event(
                                        SystemEvent.MODEL_FIRST_TOKEN,
                                        {
                                            "context": "chat_stream",
                                            "provider": self.config.provider,
                                            "model": call_model,
                                            "first_token_ms": timing.first_token_ms,
                                            "first_chunk_ms": timing.first_chunk_ms,
                                        },
                                    )
                            reasoning_content = getattr(chunk, "reasoning_content", None)
                            if reasoning_content:
                                timing.reasoning_chunk_count += 1
                                timing.reasoning_char_count += len(reasoning_content)
                                if timing.first_reasoning_ms is None:
                                    timing.first_reasoning_ms = elapsed_ms
                            chunk_usage = getattr(chunk, "usage", None)
                            if chunk_usage:
                                timing.usage = chunk_usage
                            chunk_finish_reason = getattr(chunk, "finish_reason", None)
                            if chunk_finish_reason:
                                timing.finish_reason = chunk_finish_reason
                            if chunk.type == "finish":
                                self._finish_timing(timing)
                                chunk.timing = timing
                                self._publish_timing(timing)
                                timing_published = True
                                self._publish_model_completed(timing)
                            yield chunk
                    if not timing_published:
                        self._finish_timing(timing)
                        self._publish_timing(timing)
                        timing_published = True
                        self._publish_model_completed(timing)
                    break
                except ResourceLimitError as error:
                    raise self._resource_limit_model_error(error) from error
                except TimeoutError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    normalized = normalize_model_error(
                        e,
                        provider=self.config.provider,
                        model=call_model,
                        endpoint=getattr(self._provider, "endpoint", None),
                        retry_status_codes=self._retry_status_codes(),
                    )
                    auth_config = self._provider.config.auth
                    if (
                        normalized.status_code == 401
                        and not auth_refreshed_after_401
                        and auth_config is not None
                        and auth_config.allow_401_refresh
                    ):
                        auth_refreshed_after_401 = True
                        await self._prepare_provider_auth(
                            self._provider,
                            context="chat_stream",
                            model=call_model,
                            force_refresh=True,
                        )
                        continue

                    if attempt >= max_attempts or not is_retryable_error(
                        normalized,
                        self._retry_status_codes(),
                    ):
                        raise normalized from e
                    logger.warning(
                        f"流式模型 API 调用失败，准备重试 ({attempt + 1}/{max_attempts})，"
                        f"status={normalized.status_code}, error={normalized}"
                    )
                    self._publish_runtime_model_event(
                        SystemEvent.MODEL_RETRY_SCHEDULED,
                        {
                            "model": call_model,
                            "provider": self.config.provider,
                            "context": "chat_stream",
                            "status": "retrying",
                            "attempt": attempt + 1,
                            "max_attempts": max_attempts,
                            "error": str(normalized),
                            "status_code": normalized.status_code,
                            "endpoint": normalized.endpoint,
                        },
                    )
                    yield StreamChunk(
                        type="status",
                        status="retrying",
                        error=str(normalized),
                        finish_reason=None,
                    )
                    if delay:
                        await asyncio.sleep(delay)
                        delay *= backoff

        except TimeoutError:
            error_msg = f"流式调用超时 ({self.config.timeout}s)"
            logger.error(error_msg)
            self._publish_error(
                ModelError(error_msg),
                {
                    "context": "chat_stream",
                    "timeout": self.config.timeout,
                    "message_count": len(messages),
                    "provider": self.config.provider,
                    "model": call_model,
                    "endpoint": getattr(self._provider, "endpoint", None),
                },
            )
            raise

        except ResourceLimitError as error:
            raise self._resource_limit_model_error(error) from error

        except Exception as e:
            if (
                isinstance(e, ModelError)
                and getattr(e, "error_code", None) == "resource.limit_exceeded"
            ):
                raise
            normalized = (
                e
                if isinstance(e, ModelAPIError)
                else normalize_model_error(
                    e,
                    provider=self.config.provider,
                    model=call_model,
                    retry_status_codes=self._retry_status_codes(),
                )
            )
            error_msg = f"流式模型调用失败: {normalized}"
            logger.opt(colors=False).error(error_msg)

            self._publish_error(
                normalized,
                {
                    "context": "chat_stream",
                    "message_count": len(messages),
                },
            )
            raise ModelError(error_msg) from e

    def update_embedding_config(self, embedding_config: EmbeddingConfig | None) -> None:
        """更新独立 Embedding 配置并重建 Embedding Provider。"""
        self._embedding_config = embedding_config or EmbeddingConfig()
        self._embedding_provider = None
        logger.info("ModelClient Embedding 配置已更新")

    def update_config(self, config: ModelConfig) -> None:
        """
        更新配置（例如运行时切换模型或 Provider）

        Args:
            config: 新的模型配置
        """
        old_provider = self.config.provider
        self.config = config

        if config.provider != old_provider:
            # Provider 类型变了，需要重建
            self._provider = ProviderFactory.create(config)
            logger.info(
                f"ModelClient Provider 已切换: {old_provider} -> {config.provider}, "
                f"模型: {config.name}"
            )
        else:
            # 同一 Provider，只更新配置
            self._provider.update_config(config)
            logger.info(f"ModelClient 配置已更新，Provider: {config.provider}, 模型: {config.name}")

    async def generate_image(
        self,
        prompt: str,
        model: str | None = None,
        **kwargs,
    ) -> ImageGenerationResult:
        """统一图片生成入口。"""
        call_model = model or self.config.name
        if not (
            self._provider.supports(ProviderCapability.IMAGE_GENERATION)
            or self._provider.supports(ProviderCapability.IMAGE)
        ):
            raise ModelError(f"当前 Provider ({self.config.provider}) 不支持图片生成")

        request = ImageGenerationRequest(
            prompt=prompt,
            model=call_model,
            size=kwargs.pop("size", None),
            quality=kwargs.pop("quality", None),
            style=kwargs.pop("style", None),
            n=kwargs.pop("n", 1),
            response_format=kwargs.pop("response_format", None),
            metadata=kwargs.pop("metadata", {}),
        )
        timing = ModelCallTiming(
            context="image_generation",
            provider=self.config.provider,
            model=call_model,
            start_time=self._now(),
            prompt_length=len(prompt),
        )
        try:
            self._publish_model_started(
                context="image_generation",
                provider=self.config.provider,
                model=call_model,
                prompt_length=len(prompt),
            )
            response = cast(
                ImageGenerationResult,
                await self._call_with_retry(
                    lambda: self._provider.image_generation(request, **kwargs),
                    context="image_generation",
                    model=call_model,
                ),
            )
            self._finish_timing(timing)
            response.timing = timing
            self._publish_timing(timing)
            self._publish_model_completed(timing)
            return response
        except NotImplementedError as e:
            raise ModelError(f"当前 Provider ({self.config.provider}) 不支持图片生成") from e
        except Exception as e:
            if (
                isinstance(e, ModelError)
                and getattr(e, "error_code", None) == "resource.limit_exceeded"
            ):
                raise
            normalized = (
                e
                if isinstance(e, ModelAPIError)
                else normalize_model_error(
                    e,
                    provider=self.config.provider,
                    model=call_model,
                    retry_status_codes=self._retry_status_codes(),
                )
            )
            self._publish_error(
                normalized,
                {"context": "image_generation", "prompt_length": len(prompt)},
            )
            raise ModelError(f"图片生成失败: {normalized}") from e

    async def embeddings(
        self,
        prompt: str,
        model: str | None = None,
        timeout: float | None = None,
        **kwargs,
    ) -> UnifiedEmbeddingResponse:
        """
        获取文本向量

        Args:
            prompt: 要向量化的文本
            model: 模型名称（可选，默认使用 embedding.name）
            timeout: 超时时间（可选，默认使用配置中的超时）
            **kwargs: 其他传递给 Provider 的参数

        Returns:
            UnifiedEmbeddingResponse: 统一 embedding 响应

        Raises:
            ModelError: 调用失败或超时
        """
        embedding_provider_name = self._embedding_config.provider or self.config.provider
        call_model = model or self._embedding_config.name or ""
        try:
            embedding_provider, embedding_model_config = self._get_embedding_provider()
            call_model = model or embedding_model_config.name
            call_timeout = timeout or embedding_model_config.timeout
            embedding_kwargs = self._build_embedding_kwargs(kwargs)
            logger.debug(
                f"调用 embeddings 模型，Provider: {embedding_model_config.provider}, "
                f"模型: {call_model}, 文本长度: {len(prompt)}"
            )
            timing = ModelCallTiming(
                context="embeddings",
                provider=embedding_model_config.provider,
                model=call_model,
                start_time=self._now(),
                prompt_length=len(prompt),
            )
            self._publish_model_started(
                context="embeddings",
                provider=embedding_model_config.provider,
                model=call_model,
                prompt_length=len(prompt),
            )
            response = cast(
                UnifiedEmbeddingResponse,
                await self._call_with_retry(
                    lambda: embedding_provider.embeddings(
                        model=call_model,
                        prompt=prompt,
                        **embedding_kwargs,
                    ),
                    context="embeddings",
                    model=call_model,
                    provider=embedding_provider,
                    provider_name=embedding_model_config.provider,
                    timeout=call_timeout,
                    endpoint=getattr(embedding_provider, "endpoint", None),
                ),
            )
            timing.embedding_dimension = len(response.embedding)
            self._finish_timing(timing)
            self._publish_timing(timing)
            self._publish_model_completed(timing)
            logger.debug(
                f"embeddings 响应完成，向量维度: {len(response.embedding)}，耗时: {timing.duration_ms}ms"
            )
            return response

        except TimeoutError as error:
            effective_timeout = timeout or self._embedding_config.timeout or self.config.timeout
            error_msg = f"embeddings 调用超时 ({effective_timeout}秒)"
            logger.error(f"embeddings 调用超时 ({effective_timeout}s)")
            self._publish_error(
                ModelError(error_msg),
                {
                    "context": "embeddings",
                    "timeout": effective_timeout,
                    "prompt_length": len(prompt),
                    "provider": embedding_provider_name,
                    "model": call_model,
                },
            )
            raise ModelError(error_msg) from error

        except NotImplementedError as e:
            logger.warning(f"当前 Provider 不支持 embeddings: {e}")
            self._publish_error(
                e,
                {
                    "context": "embeddings",
                    "prompt_length": len(prompt),
                    "provider": embedding_provider_name,
                    "model": call_model,
                },
            )
            raise ModelError(
                f"当前 embedding Provider ({embedding_provider_name}) 不支持 embeddings"
            ) from e

        except Exception as e:
            normalized = (
                e
                if isinstance(e, ModelAPIError)
                else normalize_model_error(
                    e,
                    provider=embedding_provider_name,
                    model=call_model,
                    retry_status_codes=self._retry_status_codes(),
                )
            )
            logger.error(f"embeddings 调用失败: {normalized}")
            self._publish_error(
                normalized,
                {
                    "context": "embeddings",
                    "prompt_length": len(prompt),
                    "provider": embedding_provider_name,
                    "model": call_model,
                },
            )
            raise ModelError(f"embeddings 调用失败: {normalized}") from e

    async def embeddings_batch(
        self,
        prompts: list[str],
        model: str | None = None,
        timeout: float | None = None,
        **kwargs,
    ) -> list[list[float]]:
        """
        批量获取文本向量。

        优先使用 Provider 原生批量 API；若 Provider 未实现，则回退到
        asyncio.gather 并发单条调用。

        Args:
            prompts: 要向量化的文本列表
            model: 模型名称（可选）
            timeout: 超时时间（可选）
            **kwargs: 其他参数

        Returns:
            向量列表；失败的条目对应空列表。
        """
        if not prompts:
            return []

        embedding_provider_name = self._embedding_config.provider or self.config.provider
        call_model = model or self._embedding_config.name or ""
        embedding_provider: BaseProvider | None = None
        embedding_model_config: ModelConfig | None = None

        try:
            embedding_provider, embedding_model_config = self._get_embedding_provider()
            call_model = model or embedding_model_config.name
            call_timeout = timeout or embedding_model_config.timeout
            embedding_kwargs = self._build_embedding_kwargs(kwargs)

            logger.debug(
                f"批量调用 embeddings，Provider: {embedding_model_config.provider}, "
                f"模型: {call_model}, 文本数: {len(prompts)}"
            )

            timing = ModelCallTiming(
                context="embeddings_batch",
                provider=embedding_model_config.provider,
                model=call_model,
                start_time=self._now(),
                prompt_length=sum(len(p) for p in prompts),
            )
            self._publish_model_started(
                context="embeddings_batch",
                provider=embedding_model_config.provider,
                model=call_model,
                prompt_length=sum(len(p) for p in prompts),
            )

            try:
                response = await self._call_with_retry(
                    lambda: embedding_provider.embeddings_batch(
                        model=call_model,
                        prompts=prompts,
                        **embedding_kwargs,
                    ),
                    context="embeddings_batch",
                    model=call_model,
                    provider=embedding_provider,
                    provider_name=embedding_model_config.provider,
                    timeout=call_timeout,
                    endpoint=getattr(embedding_provider, "endpoint", None),
                )
                embeddings = [list(r.embedding) for r in response]
            except NotImplementedError:
                # Provider 未实现原生批量 API，回退到并发单条调用
                tasks = [self.embeddings(prompt, model, timeout, **kwargs) for prompt in prompts]
                responses = await asyncio.gather(*tasks, return_exceptions=True)
                embeddings = []
                for resp in responses:
                    if isinstance(resp, BaseException):
                        logger.warning(f"批量 embeddings 中有异常: {resp}")
                        embeddings.append([])
                    else:
                        embeddings.append(list(resp.embedding))

            timing.embedding_dimension = len(embeddings[0]) if embeddings else 0
            self._finish_timing(timing)
            self._publish_timing(timing)
            self._publish_model_completed(timing)
            logger.debug(
                f"embeddings_batch 完成，向量数: {len(embeddings)}，耗时: {timing.duration_ms}ms"
            )
            return embeddings

        except TimeoutError as error:
            effective_timeout = timeout or self._embedding_config.timeout or self.config.timeout
            error_msg = f"embeddings_batch 调用超时 ({effective_timeout}秒)"
            logger.error(error_msg)
            self._publish_error(
                ModelError(error_msg),
                {
                    "context": "embeddings_batch",
                    "timeout": effective_timeout,
                    "prompt_count": len(prompts),
                    "provider": embedding_provider_name,
                    "model": call_model,
                },
            )
            raise ModelError(error_msg) from error

        except NotImplementedError as e:
            logger.warning(f"当前 Provider 不支持 embeddings: {e}")
            self._publish_error(
                e,
                {
                    "context": "embeddings_batch",
                    "prompt_count": len(prompts),
                    "provider": embedding_provider_name,
                    "model": call_model,
                },
            )
            raise ModelError(
                f"当前 embedding Provider ({embedding_provider_name}) 不支持 embeddings"
            ) from e

        except Exception as e:
            normalized = (
                e
                if isinstance(e, ModelAPIError)
                else normalize_model_error(
                    e,
                    provider=embedding_provider_name,
                    model=call_model,
                    retry_status_codes=self._retry_status_codes(),
                )
            )
            logger.error(f"embeddings_batch 调用失败: {normalized}")
            self._publish_error(
                normalized,
                {
                    "context": "embeddings_batch",
                    "prompt_count": len(prompts),
                    "provider": embedding_provider_name,
                    "model": call_model,
                },
            )
            raise ModelError(f"embeddings_batch 调用失败: {normalized}") from e

    @property
    def model_name(self) -> str:
        """获取当前使用的模型名称"""
        return self.config.name

    @property
    def provider_name(self) -> str:
        """获取当前使用的 Provider 名称"""
        return self.config.provider

    @property
    def provider(self) -> BaseProvider:
        """获取 Provider 实例（仅供高级用途）"""
        return self._provider

    @property
    def supports_embeddings(self) -> bool:
        """检查当前 Provider 是否支持 embeddings"""
        if not self._embedding_config.name:
            return False

        try:
            embedding_provider, _ = self._get_embedding_provider()
        except Exception:
            return False

        if embedding_provider.supports(ProviderCapability.EMBEDDINGS):
            return True

        # 兼容旧自定义 Provider：检查是否覆盖了 BaseProvider 的默认实现。
        from .providers.base import BaseProvider as _Base

        return type(embedding_provider).embeddings is not _Base.embeddings
