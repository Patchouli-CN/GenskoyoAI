"""工具构建服务。

ToolRegistry 负责发现与注册工具；ToolBuildService 负责根据模型能力、工具配置与
运行时上下文决定本轮模型调用注入哪些工具 schema 与附加 instructions。
"""

from __future__ import annotations

from msgspec import Struct, field

from GensokyoAI.core.agent.types import ProviderCapability
from GensokyoAI.core.config import ModelConfig, ToolConfig

from .base import ToolDefinition
from .external_manager import ExternalToolDefinition, ExternalToolExecutionPolicy
from .registry import ToolRegistry


class ToolBuildContext(Struct, frozen=True):
    """工具构建上下文。

    runtime_available_tools 表示 Runtime 层当前确认可用的工具名集合；None 代表
    Runtime 未提供额外限制，保持旧行为兼容。空集合代表 Runtime 当前没有可用工具。
    """

    tool_config: ToolConfig
    model_config: ModelConfig
    model_capabilities: set[str] = field(default_factory=set)
    runtime_available_tools: set[str] | None = None
    character_name: str = ""
    system_contexts: list[str] = field(default_factory=list)
    external_tools: list[ExternalToolDefinition] = field(default_factory=list)
    external_tool_policy: ExternalToolExecutionPolicy = field(
        default_factory=ExternalToolExecutionPolicy
    )


class ToolBuildResult(Struct, frozen=True):
    """工具构建结果。"""

    tools: list[dict] = field(default_factory=list)
    instructions: str = ""
    enabled_tool_names: list[str] = field(default_factory=list)
    disabled_reasons: dict[str, str] = field(default_factory=dict)
    model_supports_tools: bool = True


class ToolBuildService:
    """统一工具注入控制面。"""

    _MODULE_TOOL_PREFIXES: dict[str, tuple[str, ...]] = {
        "time": ("get_current_time", "get_current_dateinfo"),
        "moon": ("get_moon_phase",),
        "system": ("get_system_info",),
        "web_search": ("web_search",),
        "scene": ("scene_switch", "get_current_scene"),
    }

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def build(self, context: ToolBuildContext) -> ToolBuildResult:
        """根据上下文构建工具 schema 与工具说明。"""
        if not context.tool_config.enabled:
            return ToolBuildResult(disabled_reasons={"*": "tool_config_disabled"})

        model_supports_tools = self._model_supports_tools(context)
        selected_tools, disabled_reasons = self._select_tools(context)
        external_tools, external_disabled_reasons = self._select_external_tools(context)
        disabled_reasons.update(external_disabled_reasons)
        instructions = self._build_instructions(
            selected_tools,
            external_tools,
            context,
            include_schema_hint=model_supports_tools,
        )

        if not model_supports_tools:
            disabled_reasons.setdefault("*", "model_does_not_support_tools")
            return ToolBuildResult(
                tools=[],
                instructions=instructions,
                enabled_tool_names=[
                    *[tool.name for tool in selected_tools],
                    *[tool.namespaced_name for tool in external_tools],
                ],
                disabled_reasons=disabled_reasons,
                model_supports_tools=False,
            )

        return ToolBuildResult(
            tools=[
                *[tool.to_openai_schema() for tool in selected_tools],
                *[tool.to_openai_schema() for tool in external_tools],
            ],
            instructions=instructions,
            enabled_tool_names=[
                *[tool.name for tool in selected_tools],
                *[tool.namespaced_name for tool in external_tools],
            ],
            disabled_reasons=disabled_reasons,
            model_supports_tools=True,
        )

    def _model_supports_tools(self, context: ToolBuildContext) -> bool:
        if context.model_capabilities:
            return ProviderCapability.TOOLS in context.model_capabilities
        return True

    def _select_tools(
        self, context: ToolBuildContext
    ) -> tuple[list[ToolDefinition], dict[str, str]]:
        allowed_modules = set(context.tool_config.builtin_tools or [])
        selected: list[ToolDefinition] = []
        disabled_reasons: dict[str, str] = {}

        for tool_def in self._registry.list():
            if not self._runtime_tool_available(context, tool_def.name):
                disabled_reasons[tool_def.name] = "runtime_unavailable"
                continue
            module = self._module_for_tool(tool_def.name)
            if module and module not in allowed_modules:
                disabled_reasons[tool_def.name] = "not_in_builtin_tools"
                continue
            if tool_def.name == "web_search" and not self._web_search_tool_enabled(context):
                disabled_reasons[tool_def.name] = (
                    "web_search_disabled_or_provider_builtin_search_enabled"
                )
                continue
            selected.append(tool_def)

        selected.sort(key=lambda item: item.name)
        return selected, disabled_reasons

    def _select_external_tools(
        self,
        context: ToolBuildContext,
    ) -> tuple[list[ExternalToolDefinition], dict[str, str]]:
        selected: list[ExternalToolDefinition] = []
        disabled_reasons: dict[str, str] = {}

        for tool_def in sorted(context.external_tools, key=lambda item: item.namespaced_name):
            if not self._runtime_tool_available(context, tool_def.namespaced_name):
                disabled_reasons[tool_def.namespaced_name] = "runtime_unavailable"
                continue
            if not tool_def.permission_allowed(context.external_tool_policy):
                disabled_reasons[tool_def.namespaced_name] = (
                    "external_permission_requires_confirmation"
                )
                continue
            selected.append(tool_def)

        return selected, disabled_reasons

    @staticmethod
    def _runtime_tool_available(context: ToolBuildContext, tool_name: str) -> bool:
        if context.runtime_available_tools is None:
            return True
        return tool_name in context.runtime_available_tools

    def _web_search_tool_enabled(self, context: ToolBuildContext) -> bool:
        web_search_config = context.tool_config.web_search
        if not web_search_config.enabled or web_search_config.trigger_strategy == "off":
            return False
        return not self._provider_builtin_web_search_enabled(context)

    @staticmethod
    def _provider_builtin_web_search_enabled(context: ToolBuildContext) -> bool:
        return bool(
            context.model_config.web_search_enabled
            and context.model_config.web_search_strategy != "off"
        )

    @classmethod
    def _module_for_tool(cls, tool_name: str) -> str | None:
        for module, prefixes in cls._MODULE_TOOL_PREFIXES.items():
            if tool_name in prefixes:
                return module
        return None

    def _build_instructions(
        self,
        tools: list[ToolDefinition],
        external_tools: list[ExternalToolDefinition],
        context: ToolBuildContext,
        *,
        include_schema_hint: bool,
    ) -> str:
        parts: list[str] = []
        if tools or external_tools:
            builtin_desc = [f"- {tool.name}: {tool.description}" for tool in tools]
            external_desc = [
                f"- {tool.namespaced_name}: {tool.description}"
                f"（权限: {', '.join(sorted(tool.permissions))}）"
                for tool in external_tools
            ]
            tools_desc = "\n".join([*builtin_desc, *external_desc])
            if include_schema_hint:
                parts.append(
                    "【可用工具】\n"
                    f"{tools_desc}\n"
                    "当需要获取外部信息或执行辅助操作时，请调用相应的工具。调用工具后，将结果整合到回复中。"
                )
            else:
                parts.append(
                    "【工具策略】当前模型未声明支持结构化 tool calling；不要生成伪造工具调用。"
                    "如确需外部能力，请在回复中说明需要调用方提供相应信息。"
                )

        if self._provider_builtin_web_search_enabled(context):
            parts.append(
                "【联网搜索策略】当前模型已启用 Provider 内置联网搜索；"
                "遇到需要实时信息的问题时，优先依赖模型内置搜索能力。"
            )

        return "\n\n".join(part for part in parts if part)


__all__ = ["ToolBuildContext", "ToolBuildResult", "ToolBuildService"]
