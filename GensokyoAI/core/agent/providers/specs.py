"""Provider 元数据声明：配置校验规则的唯一事实源（数据驱动）。

每个 Provider 在此声明字段约束与专属规则；`ConfigValidator` 只负责消费，
不再硬编码 provider 名单、字段矩阵或特例分支。新增 Provider 时在
providers/ 下实现客户端，并在此登记一行即可。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from msgspec import Struct, field

# 扩展校验钩子的单条发现：(字段名, 技术描述, 用户建议, code, severity)
RuleFinding = tuple[str, str, str, str, str]


class ProviderSpec(Struct, frozen=True):
    """单个 Provider 的配置校验声明。"""

    requires_api_key: bool = False
    allow_private_base_url: bool = False  # 本地服务（如 ollama）允许内网 base_url
    unsupported: frozenset[str] = field(default_factory=frozenset)
    discouraged: frozenset[str] = field(default_factory=frozenset)
    supported_web_search: frozenset[str] = field(default_factory=frozenset)
    # 特定不支持字段的定制诊断：field -> (技术描述, 建议, code)
    unsupported_messages: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    # 集合无法表达的 provider 专属规则钩子
    extra_rule: Callable[[dict[str, Any]], list[RuleFinding]] | None = None


def _deepseek_extra_rule(data: dict[str, Any]) -> list[RuleFinding]:
    """reasoning_effort 在 thinking 关闭时被忽略——DeepSeek 专属语义。"""
    if data.get("thinking_enabled") is False and data.get("reasoning_effort"):
        return [
            (
                "reasoning_effort",
                "reasoning_effort is ignored when DeepSeek thinking_enabled is false",
                "关闭 thinking mode 时建议同时移除 reasoning_effort，避免误以为推理强度仍生效。",
                "config.model.reasoning_effort_ignored",
                "warning",
            )
        ]
    return []


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "ollama": ProviderSpec(
        allow_private_base_url=True,
        unsupported=frozenset(
            {
                "api_path",
                "extra_headers",
                "web_search_enabled",
                "web_search_context_size",
                "web_search_user_location",
                "web_search_metadata",
                "web_search_allow_fallback",
            }
        ),
        discouraged=frozenset({"api_key", "auth", "reasoning_effort"}),
        unsupported_messages={
            "api_path": (
                "Ollama provider does not support custom api_path",
                "Ollama 请只配置 base_url，例如 http://127.0.0.1:11434；不要配置 api_path。",
                "config.provider.api_path_unsupported",
            )
        },
    ),
    "openai": ProviderSpec(
        requires_api_key=True,
        discouraged=frozenset(
            {"web_search_context_size", "web_search_user_location", "web_search_metadata"}
        ),
    ),
    "openrouter": ProviderSpec(
        requires_api_key=True,
        discouraged=frozenset(
            {"web_search_context_size", "web_search_user_location", "web_search_metadata"}
        ),
    ),
    "deepseek": ProviderSpec(
        requires_api_key=True,
        unsupported=frozenset(
            {
                "api_path",
                "web_search_enabled",
                "web_search_context_size",
                "web_search_user_location",
                "web_search_metadata",
                "web_search_allow_fallback",
            }
        ),
        extra_rule=_deepseek_extra_rule,
    ),
    "openai_responses": ProviderSpec(
        requires_api_key=True,
        supported_web_search=frozenset(
            {
                "web_search_enabled",
                "web_search_strategy",
                "web_search_context_size",
                "web_search_user_location",
                "web_search_metadata",
                "web_search_allow_fallback",
            }
        ),
    ),
    "claude": ProviderSpec(
        requires_api_key=True,
        unsupported=frozenset(
            {
                "api_path",
                "web_search_enabled",
                "web_search_context_size",
                "web_search_user_location",
                "web_search_metadata",
                "web_search_allow_fallback",
            }
        ),
    ),
    "gemini": ProviderSpec(
        requires_api_key=True,
        unsupported=frozenset(
            {
                "api_path",
                "extra_headers",
                "auth",
                "web_search_context_size",
                "web_search_user_location",
                "web_search_metadata",
            }
        ),
        supported_web_search=frozenset(
            {"web_search_enabled", "web_search_strategy", "web_search_allow_fallback"}
        ),
    ),
}
