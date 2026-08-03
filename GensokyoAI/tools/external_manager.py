"""外部工具管理预研接口。

本模块定义外部工具源（例如 MCP stdio/http server）的后端边界，并提供
工具命名空间、权限元数据、调用超时与输出治理的最小闭环。
"""

from __future__ import annotations

import asyncio
import json
import re
from enum import StrEnum
from typing import Any, Protocol

from msgspec import Struct, field

from GensokyoAI.runtime.event_contract import sanitize_event_payload
from GensokyoAI.tools.errors import ToolError, ToolExecutionError
from GensokyoAI.utils.command_security import (
    CommandSecurityError,
    validate_external_tool_command,
)

EXTERNAL_TOOL_PREFIX = "external"
DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS = 30.0
DEFAULT_EXTERNAL_TOOL_MAX_OUTPUT_CHARS = 8000
_EXTERNAL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_EXTERNAL_TOOL_NAME_PATTERN = re.compile(r"^external__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")


class ExternalToolSourceStatus(StrEnum):
    """外部工具源生命周期状态。"""

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    RECONNECTING = "reconnecting"


class ExternalToolPermission(StrEnum):
    """外部工具权限标签。"""

    SAFE = "safe"
    READ_ONLY = "read-only"
    NETWORK = "network"
    FILESYSTEM = "filesystem"
    DESTRUCTIVE = "destructive"
    COSTLY = "costly"


DEFAULT_ALLOWED_EXTERNAL_PERMISSIONS = frozenset(
    {
        ExternalToolPermission.SAFE.value,
        ExternalToolPermission.READ_ONLY.value,
        ExternalToolPermission.NETWORK.value,
    }
)
RISKY_EXTERNAL_PERMISSIONS = frozenset(
    {
        ExternalToolPermission.FILESYSTEM.value,
        ExternalToolPermission.DESTRUCTIVE.value,
        ExternalToolPermission.COSTLY.value,
    }
)


class ExternalToolExecutionPolicy(Struct, frozen=True):
    """外部工具执行策略。"""

    allowed_permissions: frozenset[str] = DEFAULT_ALLOWED_EXTERNAL_PERMISSIONS
    require_confirmation_for: frozenset[str] = RISKY_EXTERNAL_PERMISSIONS
    timeout_seconds: float = DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS
    max_output_chars: int = DEFAULT_EXTERNAL_TOOL_MAX_OUTPUT_CHARS

    def allows(self, permissions: set[str]) -> bool:
        if permissions & self.require_confirmation_for:
            return False
        return permissions.issubset(self.allowed_permissions)


class ExternalToolDefinition(Struct, frozen=True):
    """外部工具定义快照。

    namespaced_name 是注入模型与 Runtime 调用时使用的稳定名称，格式为
    ``external__server__tool``。source_id/tool_name 保留原始外部工具身份。
    """

    source_id: str
    tool_name: str
    namespaced_name: str
    description: str
    schema: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def permissions(self) -> set[str]:
        return normalize_external_permissions(self.metadata.get("permissions"))

    def permission_allowed(self, policy: ExternalToolExecutionPolicy | None = None) -> bool:
        policy = policy or ExternalToolExecutionPolicy()
        return policy.allows(self.permissions)

    def to_openai_schema(self) -> dict[str, Any]:
        schema = dict(self.schema)
        function = dict(schema.get("function") or {})
        function["name"] = self.namespaced_name
        if self.description and not function.get("description"):
            function["description"] = self.description
        schema["type"] = schema.get("type", "function")
        schema["function"] = function
        return schema

    def to_dict(self) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata["permissions"] = sorted(self.permissions)
        return {
            "source_id": self.source_id,
            "tool_name": self.tool_name,
            "namespaced_name": self.namespaced_name,
            "description": self.description,
            "schema": self.to_openai_schema(),
            "metadata": metadata,
            "permissions": sorted(self.permissions),
        }


class ExternalToolSourceState(Struct):
    """外部工具源状态快照。"""

    source_id: str
    status: ExternalToolSourceStatus = ExternalToolSourceStatus.STOPPED
    tools: list[ExternalToolDefinition] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_tools: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_id": self.source_id,
            "status": self.status.value,
            "error": self.error,
            "metadata": dict(self.metadata),
            "tool_count": len(self.tools),
        }
        if include_tools:
            data["tools"] = [tool.to_dict() for tool in self.tools]
        return data


class ExternalToolSource(Protocol):
    """外部工具源适配器协议。"""

    source_id: str
    # 可选：若适配器通过子进程启动，应在此暴露原始命令列表供安全校验。
    command: list[str] | None = None

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def list_tools(self) -> list[ExternalToolDefinition]: ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...


class ExternalToolManager:
    """外部工具生命周期与调用代理边界。

    支持注册适配器、状态快照、容错枚举、命名隔离、权限拒绝、调用超时与
    输出大小治理；MCP stdio/http 适配器可实现 ExternalToolSource 协议并接入这里。
    """

    def __init__(self, policy: ExternalToolExecutionPolicy | None = None) -> None:
        self._sources: dict[str, ExternalToolSource] = {}
        self._states: dict[str, ExternalToolSourceState] = {}
        self._policy = policy or ExternalToolExecutionPolicy()

    @property
    def policy(self) -> ExternalToolExecutionPolicy:
        return self._policy

    def register_source(self, source: ExternalToolSource) -> None:
        source_id = normalize_external_name(source.source_id, kind="source_id")
        if source_id in self._sources:
            raise ValueError(f"External tool source already registered: {source_id}")
        # 若适配器暴露启动命令，先进行安全校验
        command = getattr(source, "command", None)
        if command is not None:
            try:
                validate_external_tool_command(command)
            except CommandSecurityError as exc:
                raise ValueError(
                    f"External tool source command rejected for security: {exc}"
                ) from exc
        self._sources[source_id] = source
        self._states[source_id] = ExternalToolSourceState(source_id=source_id)

    def unregister_source(self, source_id: str) -> bool:
        source_id = normalize_external_name(source_id, kind="source_id")
        removed = self._sources.pop(source_id, None) is not None
        self._states.pop(source_id, None)
        return removed

    def source_status(self, *, include_tools: bool = True) -> dict[str, Any]:
        sources = [
            self._states[source_id].to_dict(include_tools=include_tools)
            for source_id in sorted(self._states)
        ]
        return {
            "sources": sources,
            "source_count": len(sources),
            "tool_count": sum(source["tool_count"] for source in sources),
            "policy": {
                "allowed_permissions": sorted(self._policy.allowed_permissions),
                "require_confirmation_for": sorted(self._policy.require_confirmation_for),
                "timeout_seconds": self._policy.timeout_seconds,
                "max_output_chars": self._policy.max_output_chars,
            },
        }

    async def start_source(self, source_id: str) -> ExternalToolSourceState:
        source_id = normalize_external_name(source_id, kind="source_id")
        source = self._require_source(source_id)
        state = self._states[source_id]
        state.status = ExternalToolSourceStatus.STARTING
        state.error = None
        try:
            await source.start()
            state.status = ExternalToolSourceStatus.RUNNING
            state.tools = await self._safe_list_source_tools(source_id, source)
        except Exception as exc:
            state.status = ExternalToolSourceStatus.FAILED
            state.error = str(exc)
        return state

    async def stop_source(self, source_id: str) -> ExternalToolSourceState:
        source_id = normalize_external_name(source_id, kind="source_id")
        source = self._require_source(source_id)
        state = self._states[source_id]
        state.status = ExternalToolSourceStatus.STOPPING
        try:
            await source.stop()
            state.status = ExternalToolSourceStatus.STOPPED
        except Exception as exc:
            state.status = ExternalToolSourceStatus.FAILED
            state.error = str(exc)
        return state

    async def list_tools(self, *, refresh: bool = False) -> list[ExternalToolDefinition]:
        tools: list[ExternalToolDefinition] = []
        for source_id in sorted(self._sources):
            source = self._sources[source_id]
            state = self._states[source_id]
            if refresh:
                state.tools = await self._safe_list_source_tools(source_id, source)
            tools.extend(state.tools)
        tools.sort(key=lambda item: item.namespaced_name)
        return tools

    async def call_tool(self, namespaced_name: str, arguments: dict[str, Any]) -> Any:
        source_id, tool_name = split_external_tool_name(namespaced_name)
        source = self._require_source(source_id)
        tool_def = self._find_tool_definition(source_id, tool_name)
        if tool_def is None:
            # fail-closed：工具不在源工具列表（未 list_tools / 动态新增 / 名字不匹配）
            # 一律拒绝，不再盲调——权限闸门不可被「查不到」绕过
            raise ToolExecutionError(
                ToolError(
                    error_code="external_tool.not_found",
                    technical_message=(
                        f"External tool {namespaced_name!r} not found in source "
                        f"{source_id!r} tool list."
                    ),
                    user_message="外部工具未在源工具列表中注册。",
                    recoverable=True,
                    action_hint="请先调用 list_tools() 刷新外部工具列表。",
                    details={"source_id": source_id, "tool_name": tool_name},
                )
            )
        if not tool_def.permission_allowed(self._policy):
            permissions = sorted(tool_def.permissions)
            raise ToolExecutionError(
                ToolError(
                    error_code="external_tool.permission_denied",
                    technical_message=(
                        f"External tool {namespaced_name!r} requires permissions {permissions}, "
                        "but the current policy requires explicit confirmation."
                    ),
                    user_message="外部工具需要额外权限确认，已阻止调用。",
                    recoverable=True,
                    action_hint="请在配置中允许该权限，或由客户端实现确认流程后重试。",
                    details={
                        "source_id": source_id,
                        "tool_name": tool_name,
                        "permissions": permissions,
                    },
                )
            )
        try:
            result = await asyncio.wait_for(
                source.call_tool(tool_name, arguments),
                timeout=self._policy.timeout_seconds,
            )
            return normalize_external_tool_output(
                result,
                max_chars=self._policy.max_output_chars,
                source_id=source_id,
                tool_name=tool_name,
            )
        except TimeoutError as exc:
            raise ToolExecutionError(
                ToolError(
                    error_code="external_tool.timeout",
                    technical_message=f"External tool {namespaced_name!r} timed out after {self._policy.timeout_seconds}s",
                    user_message="外部工具调用超时。",
                    recoverable=True,
                    action_hint="请稍后重试，或调高外部工具超时时间。",
                    details={
                        "source_id": source_id,
                        "tool_name": tool_name,
                        "timeout_seconds": self._policy.timeout_seconds,
                    },
                )
            ) from exc
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(
                ToolError(
                    error_code="external_tool.call_failed",
                    technical_message=str(exc),
                    user_message="外部工具调用失败。",
                    recoverable=True,
                    action_hint="请检查外部工具源状态后重试。",
                    details={"source_id": source_id, "tool_name": tool_name},
                )
            ) from exc

    async def _safe_list_source_tools(
        self,
        source_id: str,
        source: ExternalToolSource,
    ) -> list[ExternalToolDefinition]:
        state = self._states[source_id]
        try:
            tools = await source.list_tools()
            normalized = [normalize_external_tool_definition(tool) for tool in tools]
            state.error = None
            return normalized
        except Exception as exc:
            state.status = ExternalToolSourceStatus.FAILED
            state.error = str(exc)
            return []

    def _require_source(self, source_id: str) -> ExternalToolSource:
        source = self._sources.get(source_id)
        if source is None:
            raise KeyError(f"Unknown external tool source: {source_id}")
        return source

    def _find_tool_definition(
        self, source_id: str, tool_name: str
    ) -> ExternalToolDefinition | None:
        state = self._states.get(source_id)
        if state is None:
            return None
        for tool in state.tools:
            if tool.tool_name == tool_name:
                return tool
        return None


def normalize_external_name(value: str, *, kind: str) -> str:
    """校验外部工具源/工具名片段，避免注入分隔符或非法字符。"""

    if not value or not _EXTERNAL_NAME_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid external {kind}: {value!r}")
    if "__" in value:
        raise ValueError(f"External {kind} must not contain double underscores: {value!r}")
    return value


def make_external_tool_name(source_id: str, tool_name: str) -> str:
    """生成外部工具命名空间名称：external__server__tool。"""

    source_id = normalize_external_name(source_id, kind="source_id")
    tool_name = normalize_external_name(tool_name, kind="tool_name")
    return f"{EXTERNAL_TOOL_PREFIX}__{source_id}__{tool_name}"


def is_external_tool_name(name: str) -> bool:
    return bool(_EXTERNAL_TOOL_NAME_PATTERN.fullmatch(name))


def split_external_tool_name(name: str) -> tuple[str, str]:
    if not is_external_tool_name(name):
        raise ValueError(f"Invalid external tool name: {name!r}")
    _, source_id, tool_name = name.split("__", 2)
    return source_id, tool_name


def normalize_external_permissions(value: Any) -> set[str]:
    if value is None:
        return {ExternalToolPermission.SAFE.value}
    raw_permissions = [value] if isinstance(value, str) else list(value)
    permissions: set[str] = set()
    allowed = {item.value for item in ExternalToolPermission}
    for item in raw_permissions:
        permission = str(item).strip().lower()
        if not permission:
            continue
        if permission not in allowed:
            raise ValueError(f"Unknown external tool permission: {permission!r}")
        permissions.add(permission)
    return permissions or {ExternalToolPermission.SAFE.value}


def normalize_external_tool_definition(tool: ExternalToolDefinition) -> ExternalToolDefinition:
    expected = make_external_tool_name(tool.source_id, tool.tool_name)
    if tool.namespaced_name != expected:
        raise ValueError(
            f"External tool namespaced_name mismatch: expected {expected!r}, got {tool.namespaced_name!r}"
        )
    metadata = dict(tool.metadata)
    metadata["permissions"] = sorted(normalize_external_permissions(metadata.get("permissions")))
    return ExternalToolDefinition(
        source_id=normalize_external_name(tool.source_id, kind="source_id"),
        tool_name=normalize_external_name(tool.tool_name, kind="tool_name"),
        namespaced_name=tool.namespaced_name,
        description=tool.description,
        schema=dict(tool.schema),
        metadata=metadata,
    )


def normalize_external_tool_output(
    result: Any,
    *,
    max_chars: int,
    source_id: str,
    tool_name: str,
) -> Any:
    """序列化并限制外部工具输出大小。"""

    if isinstance(result, str):
        content = result
        was_string = True
    else:
        content = json.dumps(sanitize_event_payload(result), ensure_ascii=False, default=str)
        was_string = False

    if len(content) <= max_chars:
        return content if was_string else json.loads(content)

    truncated = content[:max_chars]
    return {
        "content": truncated,
        "truncated": True,
        "original_length": len(content),
        "max_output_chars": max_chars,
        "source_id": source_id,
        "tool_name": tool_name,
    }


__all__ = [
    "DEFAULT_ALLOWED_EXTERNAL_PERMISSIONS",
    "DEFAULT_EXTERNAL_TOOL_MAX_OUTPUT_CHARS",
    "DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS",
    "EXTERNAL_TOOL_PREFIX",
    "ExternalToolDefinition",
    "ExternalToolExecutionPolicy",
    "ExternalToolManager",
    "ExternalToolPermission",
    "ExternalToolSource",
    "ExternalToolSourceState",
    "ExternalToolSourceStatus",
    "RISKY_EXTERNAL_PERMISSIONS",
    "is_external_tool_name",
    "make_external_tool_name",
    "normalize_external_name",
    "normalize_external_permissions",
    "normalize_external_tool_definition",
    "normalize_external_tool_output",
    "split_external_tool_name",
]
