"""Claude (Anthropic) Provider 实现

支持 Anthropic Claude 系列模型 API。
"""

# GensokyoAI/core/agent/providers/claude_provider.py

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import aiohttp

from ....utils.logger import logger
from ....utils.request_utils import merge_headers
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
)
from .base import BaseProvider

if TYPE_CHECKING:
    from ...config import ModelConfig


class ClaudeProvider(BaseProvider):
    """
    Anthropic Claude Provider

    使用 anthropic SDK 调用 Claude 系列模型。
    注意：Claude 的消息格式与 OpenAI 有所不同，需要特殊处理。
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self._client = self._build_client()
        logger.debug(f"ClaudeProvider 初始化完成，model: {config.name}")

    @property
    def capabilities(self) -> set[str]:
        """Claude Provider 能力声明。"""
        return self.apply_model_capability_overrides(
            {
                ProviderCapability.CHAT,
                ProviderCapability.STREAM,
                ProviderCapability.TOOLS,
                ProviderCapability.VISION,
                ProviderCapability.REASONING,
                ProviderCapability.STRUCTURED_OUTPUT,
            }
        )

    def _build_client(self):
        """构建 Anthropic 异步客户端"""
        anthropic_module = self.import_optional_dependency(
            "anthropic",
            "使用 Claude Provider 需要安装 anthropic 包: pip install anthropic\n"
            "或者: pip install gensokyoai[claude]",
        )
        AsyncAnthropic = anthropic_module.AsyncAnthropic

        kwargs = {}
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self.config.extra_headers:
            kwargs["default_headers"] = merge_headers(self.config.extra_headers)

        return AsyncAnthropic(**kwargs)

    def _balance_url(self) -> str | None:
        """推导 Moonshot 余额接口地址；非 Moonshot 端点或无 api_key 返回 None。

        anthropic 端点形如 https://api.moonshot.cn/anthropic；
        余额接口在 OpenAI 侧 /v1/users/me/balance。
        """
        base_url = (self.config.base_url or "").rstrip("/")
        if "moonshot" not in base_url or not self.config.api_key:
            return None
        root = base_url
        for suffix in ("/anthropic", "/v1"):
            if root.endswith(suffix):
                root = root[: -len(suffix)]
        return f"{root}/v1/users/me/balance"

    async def get_quota(self) -> dict[str, Any] | None:
        """查询账户额度：Moonshot 端点走官方余额接口，其余端点返回 None。"""
        url = self._balance_url()
        if url is None:
            return None
        timeout = aiohttp.ClientTimeout(total=15)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                url, headers={"Authorization": f"Bearer {self.config.api_key}"}
            ) as response,
        ):
            if response.status != 200:
                logger.warning(f"[ClaudeProvider] 额度查询失败: HTTP {response.status}")
                return None
            payload = await response.json(content_type=None)
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _response_format_to_output_config(response_format: dict) -> dict:
        """将统一 response_format 转换为 Anthropic output_config。"""
        if response_format.get("type") != "json_schema":
            return {"format": response_format}
        json_schema = response_format.get("json_schema")
        if not isinstance(json_schema, dict):
            return {"format": response_format}
        schema = json_schema.get("schema")
        if not isinstance(schema, dict):
            return {"format": response_format}
        return {"format": {"type": "json_schema", "schema": schema}}

    def _build_call_kwargs(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        options: dict | None,
        think: bool | None,
    ) -> dict:
        """组装 messages.create/stream 的共用请求参数（chat 与 chat_stream 共用）。"""
        options = options or {}
        system_prompt, claude_messages = self._convert_messages_to_claude(messages)
        max_tokens = options.get("num_predict") or options.get("max_tokens") or 8192

        call_kwargs: dict = {
            "model": model,
            "messages": claude_messages,
            "max_tokens": max_tokens,
            "temperature": options.get("temperature", 0.7),
            "top_p": options.get("top_p", 0.9),
        }

        if system_prompt:
            call_kwargs["system"] = system_prompt

        if response_format := options.get("response_format"):
            call_kwargs["output_config"] = self._response_format_to_output_config(response_format)

        if tools:
            call_kwargs["tools"] = self._convert_tools_to_claude(tools)

        # Claude extended thinking 支持。
        # Anthropic 要求 thinking budget 小于 max_tokens；开启 thinking 时移除采样参数以避免模型族兼容问题。
        if think:
            thinking_budget = self._get_thinking_budget(options, max_tokens)
            if thinking_budget:
                call_kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": thinking_budget,
                }
                call_kwargs.pop("temperature", None)
                call_kwargs.pop("top_p", None)
        elif think is False:
            # 显式禁用：kimi-k2.5 等模型在 anthropic 端点默认开启思考，
            # 不传参数也会思考；必须显式打 disabled 才能关闭
            call_kwargs["thinking"] = {"type": "disabled"}

        return call_kwargs

    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        """非流式调用 Claude API"""
        response = await self._client.messages.create(
            **self._build_call_kwargs(model, messages, tools, options, kwargs.get("think"))
        )
        return self._convert_response(response)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用 Claude API"""
        # 工具调用 / thinking 累积；Claude 流式事件以 content block index 区分多个 block。
        tool_blocks: dict[int, dict[str, Any]] = {}
        thinking_parts: list[str] = []
        # token 用量：message_start 带 input 初始值，message_delta 带 output 累计值；
        # 附着到终止 chunk，供 ModelClient 成本采样（此前整条链路直接丢弃）
        usage: dict[str, int] = {}

        async with self._client.messages.stream(
            **self._build_call_kwargs(model, messages, tools, options, kwargs.get("think"))
        ) as stream:
            async for event in stream:
                # 文本内容
                if hasattr(event, "type"):
                    if event.type == "message_start":
                        start_usage = getattr(getattr(event, "message", None), "usage", None)
                        if start_usage is not None:
                            usage["input_tokens"] = getattr(start_usage, "input_tokens", 0) or 0
                            usage["output_tokens"] = getattr(start_usage, "output_tokens", 0) or 0
                            # 缓存分项（真消耗计费）：命中读取 / 创建分别提取
                            for cache_key in (
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens",
                            ):
                                if value := getattr(start_usage, cache_key, None):
                                    usage[cache_key] = value

                    elif event.type == "message_delta":
                        delta_usage = getattr(event, "usage", None)
                        if delta_usage is not None:
                            usage["output_tokens"] = getattr(
                                delta_usage, "output_tokens", None
                            ) or usage.get("output_tokens", 0)

                    elif event.type == "content_block_start":
                        block = event.content_block
                        index = getattr(event, "index", 0)
                        if getattr(block, "type", "") == "tool_use":
                            tool_blocks[index] = {
                                "id": getattr(block, "id", ""),
                                "name": getattr(block, "name", ""),
                                "input_json": "",
                            }

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        index = getattr(event, "index", 0)
                        if hasattr(delta, "type"):
                            if delta.type == "text_delta":
                                yield StreamChunk(content=delta.text)
                            elif delta.type == "thinking_delta":
                                thinking_parts.append(getattr(delta, "thinking", ""))
                            elif delta.type == "input_json_delta" and index in tool_blocks:
                                tool_blocks[index]["input_json"] += getattr(
                                    delta, "partial_json", ""
                                )

                    elif event.type == "message_stop" and tool_blocks:
                        unified_tool_calls: list[ToolCall] = []
                        for _index, tool_data in sorted(tool_blocks.items()):
                            try:
                                args = (
                                    json.loads(tool_data["input_json"])
                                    if tool_data["input_json"]
                                    else {}
                                )
                            except json.JSONDecodeError:
                                args = {}
                                tool_data["raw_arguments"] = tool_data["input_json"]

                            unified_tool_calls.append(
                                ToolCall(
                                    id=tool_data.get("id", ""),
                                    provider="claude",
                                    function=ToolCallFunction(
                                        name=tool_data.get("name", ""),
                                        arguments=args,
                                        provider="claude",
                                    ),
                                )
                            )

                        unified_msg = UnifiedMessage(
                            role="assistant",
                            content="",
                            tool_calls=unified_tool_calls,
                        )
                        tool_info = {
                            "message": unified_msg,
                            "thinking": "".join(thinking_parts) if thinking_parts else None,
                        }
                        raw_arguments = {
                            index: tool_data["raw_arguments"]
                            for index, tool_data in sorted(tool_blocks.items())
                            if "raw_arguments" in tool_data
                        }
                        if raw_arguments:
                            tool_info["raw_arguments"] = raw_arguments
                        yield StreamChunk(
                            type="tool_call",
                            is_tool_call=True,
                            tool_info=tool_info,
                            finish_reason="tool_use",
                            usage=usage or None,
                        )
                    elif event.type == "message_stop":
                        yield StreamChunk(type="finish", finish_reason="stop", usage=usage or None)

    async def list_models(self) -> list[ModelInfo]:
        """Claude SDK 暂不统一暴露模型列表，返回当前配置模型作为 fallback。"""
        return [
            ModelInfo(
                id=self.config.name,
                name=self.config.name,
                capabilities=sorted(self.apply_model_capability_overrides(self.capabilities)),
                metadata={"fallback": True},
            )
        ]

    async def embeddings(
        self,
        model: str,
        prompt: str,
        **kwargs,
    ) -> UnifiedEmbeddingResponse:
        """Claude 不支持 embeddings"""
        raise NotImplementedError("Anthropic Claude 不提供 embeddings API")

    # ==================== 转换工具方法 ====================

    @staticmethod
    def _get_thinking_budget(options: dict, max_tokens: int) -> int | None:
        """获取 Claude extended thinking 的 token 预算。"""
        budget = options.get("thinking_budget_tokens") or options.get("thinking_budget")
        if budget is not None:
            budget = int(budget)
            return budget if 0 < budget < max_tokens else None

        # Anthropic thinking budget 必须小于 max_tokens；max_tokens 太低时不自动开启，避免 400。
        if max_tokens <= 1024:
            logger.warning(
                "Claude extended thinking 已请求，但 max_tokens <= 1024，跳过 thinking 参数"
            )
            return None
        return min(max_tokens - 1, max(1024, max_tokens // 2))

    @staticmethod
    def _image_to_block(image: ImageInput | dict | Any) -> dict:
        """将统一图片输入转换为 Claude image content block。"""
        url = image.get("url") if isinstance(image, dict) else getattr(image, "url", None)
        data = image.get("data") if isinstance(image, dict) else getattr(image, "data", None)
        mime_type = (
            image.get("mime_type") if isinstance(image, dict) else getattr(image, "mime_type", None)
        ) or "image/png"

        def _read_local(path: Path) -> bytes:
            return path.read_bytes()

        if url:
            parsed = urlparse(str(url))
            if parsed.scheme == "file":
                local_path = Path(url2pathname(parsed.path))
                if local_path.exists():
                    try:
                        data = base64.b64encode(_read_local(local_path)).decode("utf-8")
                        return {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": data,
                            },
                        }
                    except Exception as error:
                        logger.warning(f"读取本地图片失败: {local_path}, {error}")
            elif parsed.scheme in ("http", "https"):
                return {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": str(url),
                    },
                }
            local_path = Path(str(url))
            if local_path.exists():
                try:
                    data = base64.b64encode(_read_local(local_path)).decode("utf-8")
                    return {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": data,
                        },
                    }
                except Exception as error:
                    logger.warning(f"读取本地图片失败: {local_path}, {error}")
            # 未知 scheme 也尝试当 URL 发
            return {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": str(url),
                },
            }
        if data:
            if str(data).startswith("data:") and ";base64," in str(data):
                data = str(data).split(";base64,", 1)[1]
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": data,
                },
            }
        return {"type": "text", "text": ""}

    @staticmethod
    def _convert_content_blocks(content: Any) -> Any:
        """将统一多模态 content parts 转换为 Claude content blocks。"""
        if not isinstance(content, list):
            return content

        blocks: list[dict] = []
        for part in content:
            part_type = (
                part.get("type") if isinstance(part, dict) else getattr(part, "type", "text")
            )
            if part_type == "text":
                text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if text is not None:
                    blocks.append({"type": "text", "text": str(text)})
                continue

            image = part.get("image") if isinstance(part, dict) else getattr(part, "image", None)
            if part_type == "image" and image:
                blocks.append(ClaudeProvider._image_to_block(image))
        return blocks

    @classmethod
    def _convert_messages_to_claude(cls, messages: list[dict]) -> tuple[str, list[dict]]:
        """
        将项目内部 / OpenAI 风格 messages 转换为 Claude Messages API 格式。

        Claude 要求：
        - system 作为顶层 system 参数传入；
        - assistant 工具调用必须是 assistant content 中的 tool_use block；
        - 工具结果必须是紧随其后的 user content 中的 tool_result block；
        - 不支持 OpenAI 的 role=tool。
        """
        system_parts: list[str] = []
        claude_messages: list[dict] = []

        pending_tool_results: list[dict] = []

        def flush_tool_results() -> None:
            if pending_tool_results:
                # Claude 要求多个 tool_result block 在同一 user 消息中置于 content 数组最前面。
                claude_messages.append({"role": "user", "content": pending_tool_results.copy()})
                pending_tool_results.clear()

        for msg in messages:
            role = msg.get("role", "")
            content = cls._convert_content_blocks(msg.get("content", ""))

            if role == "system":
                if content:
                    if isinstance(content, list):
                        system_parts.extend(
                            block.get("text", "")
                            for block in content
                            if block.get("type") == "text"
                        )
                    else:
                        system_parts.append(str(content))
                continue

            if role == "tool":
                pending_tool_results.append(cls._convert_tool_result_block(msg))
                continue

            flush_tool_results()

            if role == "assistant":
                converted = cls._convert_assistant_message({**msg, "content": content})
                # 空 content 且无 tool_calls 的 assistant 消息是退化消息（Anthropic
                # 会 400，CODE_INF 04#12）：跳过，不发给 API
                if converted.get("content"):
                    claude_messages.append(converted)
            elif role == "user":
                claude_messages.append({"role": "user", "content": content})
            else:
                # Claude 只接受 user/assistant；未知角色降级为 user 文本。
                claude_messages.append(
                    {
                        "role": "user",
                        "content": content if isinstance(content, list) else str(content),
                    }
                )

        flush_tool_results()

        system_prompt = "\n\n".join(system_parts) if system_parts else ""
        return system_prompt, claude_messages

    @classmethod
    def _convert_assistant_message(cls, msg: dict) -> dict:
        """转换 assistant 消息，保留 tool_use blocks。"""
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            return {"role": "assistant", "content": content}

        blocks: list[dict] = []
        if content:
            if isinstance(content, list):
                blocks.extend(content)
            else:
                blocks.append({"type": "text", "text": str(content)})

        for tool_call in tool_calls:
            tool_id, name, arguments = cls._extract_tool_call(tool_call)
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": arguments,
                }
            )

        return {"role": "assistant", "content": blocks}

    @staticmethod
    def _convert_tool_result_block(msg: dict) -> dict:
        """将 OpenAI role=tool 消息转换为 Claude tool_result content block。"""
        content = msg.get("content", "")
        tool_result: dict = {
            "type": "tool_result",
            "tool_use_id": msg.get("tool_call_id", "") or msg.get("id", ""),
        }
        if content:
            tool_result["content"] = str(content)
        if msg.get("is_error") or str(content).startswith(("错误:", "调用出错啦:")):
            tool_result["is_error"] = True
        return tool_result

    @staticmethod
    def _extract_tool_call(tool_call) -> tuple[str, str, dict]:
        """从 ToolCall 对象或 OpenAI 风格 dict 中提取 Claude tool_use 所需字段。"""
        if isinstance(tool_call, dict):
            function = tool_call.get("function", {}) or {}
            args = function.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    args = {}
            return tool_call.get("id", ""), function.get("name", ""), args or {}

        function = getattr(tool_call, "function", None)
        return (
            getattr(tool_call, "id", ""),
            getattr(function, "name", "") if function else "",
            getattr(function, "arguments", {}) if function else {},
        )

    def _convert_response(self, response) -> UnifiedResponse:
        """将 Claude Message 转换为 UnifiedResponse"""
        content_parts = []
        tool_calls = []
        thinking = None

        for block in response.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        provider="claude",
                        function=ToolCallFunction(
                            name=block.name,
                            arguments=block.input or {},
                            provider="claude",
                        ),
                    )
                )
            elif block.type == "thinking":
                thinking = block.thinking

        usage = getattr(response, "usage", None)
        usage_dict = None
        if usage is not None:
            usage_dict = {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            }
            # 缓存分项（真消耗计费）：命中读取 / 创建分别提取
            for cache_key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                if value := getattr(usage, cache_key, None):
                    usage_dict[cache_key] = value
        return UnifiedResponse(
            message=UnifiedMessage(
                role="assistant",
                content="".join(content_parts),
                tool_calls=tool_calls if tool_calls else None,
            ),
            model=response.model or "",
            done=True,
            thinking=thinking,
            usage=usage_dict,
        )

    @staticmethod
    def _convert_tools_to_claude(tools: list[dict]) -> list[dict]:
        """
        将 Ollama/OpenAI 格式的工具定义转换为 Claude 格式

        Claude 格式:
          {"name": "...", "description": "...", "input_schema": {...}}

        OpenAI/Ollama 格式:
          {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
        """
        claude_tools = []
        for tool in tools:
            if "function" in tool:
                func = tool["function"]
                claude_tools.append(
                    {
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "input_schema": func.get("parameters", {}),
                    }
                )
            else:
                # 已经是 Claude 格式
                claude_tools.append(tool)
        return claude_tools
