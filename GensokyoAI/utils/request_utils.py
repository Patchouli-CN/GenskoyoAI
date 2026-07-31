"""模型 API 请求辅助工具：错误归一化、重试判断、URL 规范化。"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import sys
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from msgspec import Struct

# 全局 ClientSession 管理（单例模式，支持连接池复用）
_session: aiohttp.ClientSession | None = None
_connector: aiohttp.TCPConnector | None = None

# 连接池配置
DEFAULT_POOL_LIMIT = 100
DEFAULT_POOL_LIMIT_PER_HOST = 10
DEFAULT_FORCE_CLOSE = False

# enable_cleanup_closed 在 Python 3.14+ 中不再需要（已修复 CPython 问题）
# 仅在 Python < 3.14 时启用
_ENABLE_CLEANUP_CLOSED = sys.version_info < (3, 14)


async def get_client_session(
    *,
    limit: int = DEFAULT_POOL_LIMIT,
    limit_per_host: int = DEFAULT_POOL_LIMIT_PER_HOST,
) -> aiohttp.ClientSession:
    """获取全局 ClientSession 单例，支持连接池复用。

    首次调用时创建 session，后续复用同一实例。
    如果 session 已关闭，自动重新创建。
    """
    global _session, _connector

    if _session is None or _session.closed:
        connector_kwargs: dict[str, Any] = {
            "limit": limit,
            "limit_per_host": limit_per_host,
            "force_close": DEFAULT_FORCE_CLOSE,
        }
        if _ENABLE_CLEANUP_CLOSED:
            connector_kwargs["enable_cleanup_closed"] = True
        _connector = aiohttp.TCPConnector(**connector_kwargs)
        _session = aiohttp.ClientSession(
            connector=_connector,
            raise_for_status=False,  # 我们自己处理状态码
        )
    return _session


async def close_client_session() -> None:
    """关闭全局 ClientSession 和 DNS 解析器，释放所有资源。

    应在程序退出时调用，确保优雅关闭。
    """
    global _session, _connector

    # 先关闭 URL 安全模块的异步 DNS 解析器
    from .url_security import _close_resolver

    await _close_resolver()

    if _session is not None and not _session.closed:
        await _session.close()
        _session = None

    if _connector is not None:
        await _connector.close()
        _connector = None


HTTP_STATUS_MESSAGES: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    408: "Request Timeout",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}

DEFAULT_RETRY_STATUS_CODES = {500, 502, 503, 504}


class NormalizedEndpoint(Struct, frozen=True):
    """规范化后的 API 端点。"""

    api_host: str
    api_path: str


class ModelAPIError(Exception):
    """结构化模型 API 错误。"""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status_code: int | None = None,
        response_body: str | None = None,
        endpoint: str | None = None,
        retryable: bool = False,
        original_error: Exception | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.response_body = response_body
        self.endpoint = endpoint
        self.retryable = retryable
        self.original_error = original_error


def is_html_response(text: str | None) -> bool:
    """判断响应体是否像 HTML 错误页。"""
    if not text:
        return False
    trimmed = text.lstrip().lower()
    return trimmed.startswith("<!doctype") or trimmed.startswith("<html")


def sanitize_response_body(status_code: int | None, response_body: str | None) -> str:
    """清洗 API 错误响应，避免把网关 HTML 页直接暴露给上层。"""
    if not response_body:
        return ""
    if is_html_response(response_body):
        if status_code is not None:
            status = HTTP_STATUS_MESSAGES.get(status_code, f"HTTP {status_code}")
            return f"{status} - 服务端返回了 HTML 错误页，而不是有效的 API 响应。"
        return "Server Error - 服务端返回了 HTML 错误页，而不是有效的 API 响应。"
    return response_body


def _ensure_scheme(api_host: str) -> str:
    if api_host.startswith(("http://", "https://")):
        return api_host
    return f"https://{api_host}"


def _strip_trailing_slash(value: str) -> str:
    return value[:-1] if value.endswith("/") else value


def normalize_openai_api_host_and_path(
    api_host: str | None,
    api_path: str | None = None,
    *,
    default_host: str = "https://api.openai.com/v1",
    default_path: str = "/chat/completions",
) -> NormalizedEndpoint:
    """规范化 OpenAI 兼容 API host/path。

    兼容用户填写 host、host/v1、完整 /chat/completions endpoint、OpenRouter/xAI 等常见形式。
    """
    host = (api_host or "").strip()
    path = (api_path or "").strip()

    if not host:
        return NormalizedEndpoint(default_host, path or default_path)

    host = _strip_trailing_slash(_ensure_scheme(host))
    if path and not path.startswith("/"):
        path = f"/{path}"

    if host.endswith(default_path):
        host = host[: -len(default_path)]
        path = default_path

    if host.endswith("://api.openai.com") or host.endswith("://api.openai.com/v1"):
        return NormalizedEndpoint(default_host, path or default_path)

    if host.endswith("://openrouter.ai") or host.endswith("://openrouter.ai/api"):
        return NormalizedEndpoint("https://openrouter.ai/api/v1", path or default_path)

    if host.endswith("://api.x.com") or host.endswith("://api.x.com/v1"):
        return NormalizedEndpoint("https://api.x.com/v1", path or default_path)

    if not path and not host.endswith("/v1"):
        host = f"{host}/v1"

    return NormalizedEndpoint(host, path or default_path)


def endpoint_url(endpoint: NormalizedEndpoint) -> str:
    """拼接规范化 endpoint 的完整 URL。"""
    return f"{endpoint.api_host}{endpoint.api_path}"


def normalize_search_url(url: str) -> str:
    """规范化搜索结果的 URL 用于去重：小写 scheme/host，去尾斜杠，移除 utm_* 参数。"""
    try:
        parts = urlsplit(_html.unescape(url).strip())
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if not key.lower().startswith("utm_")
            ]
        )
        return urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, "")
        )
    except Exception:
        return url


def has_arbitrary_api_path(endpoint: NormalizedEndpoint, default_path: str) -> bool:
    """判断 endpoint.api_path 是否无法通过 OpenAI SDK resource path 安全表达。"""
    return endpoint.api_path != default_path and not endpoint.api_path.endswith(default_path)


def sdk_base_url_for_endpoint(endpoint: NormalizedEndpoint, default_path: str) -> str:
    """计算 OpenAI SDK 可用的 base_url。"""
    if endpoint.api_path == default_path:
        return endpoint.api_host
    if endpoint.api_path.endswith(default_path):
        prefix = endpoint.api_path[: -len(default_path)].rstrip("/")
        return f"{endpoint.api_host}{prefix}"
    return endpoint.api_host


def normalize_openai_responses_host_and_path(
    api_host: str | None,
    api_path: str | None = None,
) -> NormalizedEndpoint:
    """规范化 OpenAI Responses API host/path。"""
    custom_path = (api_path or "").strip()
    has_custom_path = bool(custom_path and custom_path != "/responses")
    endpoint = normalize_openai_api_host_and_path(
        api_host,
        custom_path if has_custom_path else None,
        default_path="/responses",
    )
    if not has_custom_path:
        return NormalizedEndpoint(endpoint.api_host, "/responses")
    return endpoint


def normalize_deepseek_api_host(api_host: str | None) -> str:
    """规范化 DeepSeek API host，保持官方默认不强制追加 /v1。"""
    if not api_host:
        return "https://api.deepseek.com"
    return _strip_trailing_slash(_ensure_scheme(api_host.strip()))


def extract_status_code(error: BaseException) -> int | None:
    """从 SDK 异常中提取 HTTP 状态码。"""
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if isinstance(status, int):
        return status
    message = str(error)
    match = re.search(r"(?:Status Code|status(?:_code)?=?)\s*:?\s*(\d{3})", message, re.I)
    if match:
        return int(match.group(1))
    return None


def extract_response_body(error: BaseException) -> str | None:
    """从 SDK 异常中提取响应体。"""
    for attr in ("response_body", "body", "text"):
        value = getattr(error, attr, None)
        if value:
            return str(value)
    response = getattr(error, "response", None)
    if response is not None:
        for attr in ("text", "content"):
            value = getattr(response, attr, None)
            if value:
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return str(value)
    return None


def is_retryable_error(
    error: BaseException,
    retry_status_codes: set[int] | None = None,
) -> bool:
    """判断错误是否适合自动重试。"""
    if isinstance(error, asyncio.CancelledError):
        return False
    if isinstance(error, ModelAPIError):
        return error.retryable
    status_code = extract_status_code(error)
    return status_code in (retry_status_codes or DEFAULT_RETRY_STATUS_CODES)


def normalize_model_error(
    error: Exception,
    *,
    provider: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    retry_status_codes: set[int] | None = None,
) -> ModelAPIError:
    """将任意 SDK/网络异常转换为结构化模型 API 错误。"""
    if isinstance(error, ModelAPIError):
        return error

    status_code = extract_status_code(error)
    response_body = extract_response_body(error)
    sanitized_body = sanitize_response_body(status_code, response_body)
    retryable = status_code in (retry_status_codes or DEFAULT_RETRY_STATUS_CODES)

    if status_code is not None:
        status_text = HTTP_STATUS_MESSAGES.get(status_code, f"HTTP {status_code}")
        detail = sanitized_body or str(error)
        message = f"API 状态码 {status_code} ({status_text}): {detail}"
    else:
        message = str(error)

    return ModelAPIError(
        message,
        provider=provider,
        model=model,
        status_code=status_code,
        response_body=sanitized_body or response_body,
        endpoint=endpoint,
        retryable=retryable,
        original_error=error,
    )


def merge_headers(*headers: dict[str, Any] | None) -> dict[str, str]:
    """合并 headers，过滤空值。"""
    merged: dict[str, str] = {}
    for group in headers:
        if not group:
            continue
        for key, value in group.items():
            if value is not None:
                merged[str(key)] = str(value)
    return merged


async def post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float | None = None
) -> dict[str, Any]:
    """异步执行 JSON POST；使用全局 ClientSession 实现连接复用。"""
    request_headers = {"Content-Type": "application/json", **headers}
    timeout_obj = aiohttp.ClientTimeout(total=timeout)

    try:
        session = await get_client_session()
        async with session.post(
            url, json=payload, headers=request_headers, timeout=timeout_obj
        ) as response:
            raw = await response.text(encoding="utf-8", errors="replace")
            if response.status >= 400:
                raise ModelAPIError(
                    f"API 状态码 {response.status} ({HTTP_STATUS_MESSAGES.get(response.status, f'HTTP {response.status}')}): {sanitize_response_body(response.status, raw)}",
                    status_code=response.status,
                    response_body=sanitize_response_body(response.status, raw) or raw,
                    endpoint=url,
                    retryable=response.status in DEFAULT_RETRY_STATUS_CODES,
                )
            return json.loads(raw) if raw else {}
    except aiohttp.ClientError as e:
        raise ModelAPIError(
            f"网络请求失败: {e}",
            endpoint=url,
            retryable=False,
            original_error=e,
        ) from e


def _preview_text(value: str, limit: int = 500) -> str:
    """返回用于错误诊断的安全短文本。"""
    compact = " ".join((value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


async def post_sse(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float | None = None
) -> AsyncIterator[dict[str, Any]]:
    """异步执行 SSE POST，使用全局 ClientSession 实现连接复用，并逐条产出 JSON data。"""
    request_headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **headers}
    timeout_obj = aiohttp.ClientTimeout(total=timeout)
    try:
        session = await get_client_session()
        async with session.post(
            url, json=payload, headers=request_headers, timeout=timeout_obj
        ) as response:
            if response.status >= 400:
                raw = await response.text(encoding="utf-8", errors="replace")
                raise ModelAPIError(
                    f"API 状态码 {response.status} ({HTTP_STATUS_MESSAGES.get(response.status, f'HTTP {response.status}')}): {sanitize_response_body(response.status, raw)}",
                    status_code=response.status,
                    response_body=sanitize_response_body(response.status, raw) or raw,
                    endpoint=url,
                    retryable=response.status in DEFAULT_RETRY_STATUS_CODES,
                )
            event_lines: list[str] = []
            ignored_lines = 0
            event_index = 0
            line_number = 0
            while True:
                line_bytes = await response.content.readline()
                if not line_bytes:
                    break
                line_number += 1
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                stripped = line.strip()
                if not stripped:
                    if event_lines:
                        event_index += 1
                        for event in _parse_sse_event(
                            event_lines,
                            event_index,
                            line_number,
                            ignored_lines,
                        ):
                            yield event
                        event_lines = []
                    continue
                if stripped.startswith(":"):
                    continue
                if stripped.startswith("data:"):
                    event_lines.append(stripped[5:].strip())
                    continue
                ignored_lines += 1
            if event_lines:
                event_index += 1
                for event in _parse_sse_event(
                    event_lines,
                    event_index,
                    line_number,
                    ignored_lines,
                ):
                    yield event
    except aiohttp.ClientError as e:
        raise ModelAPIError(
            f"网络请求失败: {e}",
            endpoint=url,
            retryable=False,
            original_error=e,
        ) from e


def _parse_sse_event(
    event_lines: list[str],
    event_index: int,
    line_number: int,
    ignored_lines: int,
) -> list[dict[str, Any]]:
    """将 SSE data 行解析为 JSON 事件列表。"""
    event_data = "\n".join(event_lines)
    if event_data == "[DONE]" or not event_data:
        return []
    try:
        return [json.loads(event_data)]
    except json.JSONDecodeError as e:
        detail = (
            f"SSE JSON 解析失败: event_index={event_index}, line={line_number}, "
            f"ignored_lines={ignored_lines}, pos={e.pos}, preview={_preview_text(event_data)!r}"
        )
        raise ModelAPIError(
            detail,
            response_body=_preview_text(event_data),
            retryable=False,
            original_error=e,
        ) from e
