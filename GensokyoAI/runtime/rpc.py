"""JSON-compatible RPC dispatch helpers for the GensokyoAI runtime.

This module owns the public method-name mapping for frontend-agnostic runtime
clients. It intentionally contains no transport logic, no UI assumptions, and no
Flutter-specific behavior. Transports such as ``bridge_main.py`` can use this
mapping through :class:`GensokyoAI.runtime.service.RuntimeService`.
"""

from __future__ import annotations

import inspect
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Union, get_args, get_origin, get_type_hints

from msgspec import Struct

from GensokyoAI.tools.errors import ToolExecutionError
from GensokyoAI.tools.external_manager import ExternalToolSourceStatus

_EXTERNAL_TOOL_STATUS_METHODS: dict[str, str] = {
    ExternalToolSourceStatus.STARTING.value: "external_tool.starting",
    ExternalToolSourceStatus.RUNNING.value: "external_tool.running",
    ExternalToolSourceStatus.STOPPING.value: "external_tool.stopping",
    ExternalToolSourceStatus.FAILED.value: "external_tool.failed",
    ExternalToolSourceStatus.RECONNECTING.value: "external_tool.reconnecting",
}


RuntimeRpcTarget = Any


class RpcMethodSpec(Struct, frozen=True):
    """Mapping from public RPC method name to runtime service method name."""

    method: str
    handler_name: str
    legacy: bool = False
    replacement: str | None = None
    remove_after: str | None = None
    remote_admin: bool = False

    @property
    def namespace(self) -> str:
        """Return the stable method namespace for documentation and clients."""

        return self.method.split(".", 1)[0] if "." in self.method else "legacy"

    @property
    def deprecated(self) -> bool:
        """Return whether this method is deprecated in the current protocol."""

        return self.legacy or self.replacement is not None


RUNTIME_PROTOCOL_VERSION = "2.1.0"
RUNTIME_PROTOCOL_MAJOR_VERSION = 2
RUNTIME_BREAKING_CHANGES: tuple[dict[str, str], ...] = (
    {
        "scope": "runtime.rpc.error_envelope",
        "change": "RPC method failures now use the transport-level ok=false envelope instead of a nested result.ok=false payload.",
        "migration": "Check the top-level ok field and read the top-level error object.",
    },
    {
        "scope": "runtime.websocket.stream_start",
        "change": "WebSocket streaming requests now receive a start acknowledgement before stream events.",
        "migration": "Read result.stream_id and result.generation_id from the acknowledgement frame before consuming event frames.",
    },
    {
        "scope": "runtime.info.protocol",
        "change": "runtime.info.protocol now identifies the shared RPC protocol instead of naming one transport.",
        "migration": "Discover concrete transports through runtime.info.transports.",
    },
    {
        "scope": "runtime.reasoning_visibility",
        "change": "Reasoning content is public by default in non-streaming and streaming agent responses.",
        "migration": "Render reasoning_content separately from content and apply client-side visibility policy when needed.",
    },
    {
        "scope": "runtime.multi_user_resources",
        "change": "Network Agent and session operations require explicit agent_id and session_id resource ownership.",
        "migration": "Initialize or list an Agent, then include its agent_id and the target session_id in every conversation-scoped request.",
    },
    {
        "scope": "runtime.message_concurrency",
        "change": "Network writes require expected_revision; message sends also require idempotency_key.",
        "migration": "Read the latest session revision before writes and reuse one stable idempotency_key when retrying a send.",
    },
    {
        "scope": "runtime.pagination",
        "change": "session.list and session.messages return cursor-paginated result objects.",
        "migration": "Read sessions/messages arrays and continue with next_cursor while has_more is true.",
    },
)


RPC_METHOD_SPECS: tuple[RpcMethodSpec, ...] = (
    RpcMethodSpec("runtime.info", "info"),
    RpcMethodSpec("runtime.health", "health"),
    RpcMethodSpec("runtime.ready", "readiness"),
    RpcMethodSpec("runtime.shutdown", "shutdown", remote_admin=True),
    RpcMethodSpec("config.validate", "validate_config"),
    RpcMethodSpec("character.validate", "validate_character"),
    RpcMethodSpec("character_package.validate", "validate_character_package"),
    RpcMethodSpec("character_package.preview", "preview_character_package"),
    RpcMethodSpec("character_package.import", "import_character_package", remote_admin=True),
    RpcMethodSpec("character_package.export", "export_character_package", remote_admin=True),
    RpcMethodSpec("agent.init", "init"),
    RpcMethodSpec("agent.list", "list_agents"),
    RpcMethodSpec("agent.delete", "delete_agent"),
    RpcMethodSpec("agent.send_message", "send_message"),
    RpcMethodSpec("agent.send_message_stream", "send_message_stream"),
    RpcMethodSpec("message.status", "message_status"),
    RpcMethodSpec("character.list", "list_characters"),
    RpcMethodSpec("model.list", "list_models"),
    RpcMethodSpec("model.info", "model_info"),
    RpcMethodSpec("session.create", "create_session"),
    RpcMethodSpec("session.list", "list_sessions"),
    RpcMethodSpec("session.current", "current_session"),
    RpcMethodSpec("session.resume", "resume_session"),
    RpcMethodSpec("session.delete", "delete_session"),
    RpcMethodSpec("session.export", "export_session"),
    RpcMethodSpec("session.rename", "rename_session"),
    RpcMethodSpec("session.messages", "session_messages"),
    RpcMethodSpec("session.replace_messages", "session_replace_messages"),
    RpcMethodSpec("session.regenerate_from", "session_regenerate_from"),
    RpcMethodSpec("session.rollback", "rollback_session"),
    RpcMethodSpec("dependency.status", "dependency_status"),
    RpcMethodSpec("dependency.install", "install_dependencies", remote_admin=True),
    RpcMethodSpec("external_tool.status", "external_tool_status"),
    RpcMethodSpec("initiative_timer.current", "initiative_timer_current"),
    RpcMethodSpec("initiative_timer.update", "initiative_timer_update"),
    RpcMethodSpec("initiative_timer.cancel", "initiative_timer_cancel"),
    RpcMethodSpec("initiative_timer.trigger", "initiative_timer_trigger"),
    RpcMethodSpec("initiative_timer.hesitation", "initiative_timer_hesitation"),
    RpcMethodSpec("initiative_timer.hesitation.set", "initiative_timer_hesitation_set"),
    RpcMethodSpec("world.init", "world_init"),
    RpcMethodSpec("world.start", "world_start"),
    RpcMethodSpec("world.send_message", "world_send_message"),
    RpcMethodSpec("world.send_message_stream", "world_send_message_stream"),
    RpcMethodSpec("world.state", "world_state"),
    RpcMethodSpec("world.roster", "world_roster"),
    RpcMethodSpec("world.transcript", "world_transcript"),
    RpcMethodSpec("world.move", "world_move"),
    RpcMethodSpec("world.session.create", "world_session_create"),
    RpcMethodSpec("world.session.list", "world_session_list"),
    RpcMethodSpec("world.session.resume", "world_session_resume"),
    RpcMethodSpec("world.session.delete", "world_session_delete"),
    RpcMethodSpec("world.session.export", "world_session_export"),
    RpcMethodSpec("world.shutdown", "world_shutdown"),
    RpcMethodSpec("memory.list", "memory_list"),
    RpcMethodSpec("memory.search", "memory_search"),
    RpcMethodSpec("memory.get", "memory_get"),
    RpcMethodSpec("memory.update", "memory_update"),
    RpcMethodSpec("memory.delete", "memory_delete"),
    RpcMethodSpec("memory.graph", "memory_graph"),
    RpcMethodSpec("media.list", "media_list"),
    RpcMethodSpec("media.delete", "media_delete"),
    RpcMethodSpec("scene.current", "scene_current"),
    RpcMethodSpec("scene.list", "scene_list"),
    RpcMethodSpec("scene.get", "scene_get"),
    RpcMethodSpec("scene.switch", "scene_switch"),
    RpcMethodSpec("scene.graph", "scene_graph"),
    RpcMethodSpec("init", "init", legacy=True, replacement="agent.init", remove_after="2.0.0"),
    RpcMethodSpec(
        "send_message",
        "send_message",
        legacy=True,
        replacement="agent.send_message",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "send_message_stream",
        "send_message_stream",
        legacy=True,
        replacement="agent.send_message_stream",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "list_characters",
        "list_characters",
        legacy=True,
        replacement="character.list",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "create_session",
        "create_session",
        legacy=True,
        replacement="session.create",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "list_sessions",
        "list_sessions",
        legacy=True,
        replacement="session.list",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "current_session",
        "current_session",
        legacy=True,
        replacement="session.current",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "resume_session",
        "resume_session",
        legacy=True,
        replacement="session.resume",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "delete_session",
        "delete_session",
        legacy=True,
        replacement="session.delete",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "export_session",
        "export_session",
        legacy=True,
        replacement="session.export",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "rename_session",
        "rename_session",
        legacy=True,
        replacement="session.rename",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "rollback_session",
        "rollback_session",
        legacy=True,
        replacement="session.rollback",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "shutdown",
        "shutdown",
        legacy=True,
        replacement="runtime.shutdown",
        remove_after="2.0.0",
        remote_admin=True,
    ),
    RpcMethodSpec(
        "dependency_status",
        "dependency_status",
        legacy=True,
        replacement="dependency.status",
        remove_after="2.0.0",
    ),
    RpcMethodSpec(
        "install_dependencies",
        "install_dependencies",
        legacy=True,
        replacement="dependency.install",
        remove_after="2.0.0",
        remote_admin=True,
    ),
    RpcMethodSpec(
        "external_tool_status",
        "external_tool_status",
        legacy=True,
        replacement="external_tool.status",
        remove_after="2.0.0",
    ),
)


NETWORK_SESSION_METHODS = frozenset(
    {
        "agent.send_message",
        "agent.send_message_stream",
        "message.status",
        "send_message",
        "send_message_stream",
        "session.current",
        "session.delete",
        "session.export",
        "session.rename",
        "session.messages",
        "session.replace_messages",
        "session.regenerate_from",
        "session.rollback",
        "current_session",
        "delete_session",
        "export_session",
        "rename_session",
        "rollback_session",
    }
)
NETWORK_REVISION_METHODS = frozenset(
    {
        "agent.send_message",
        "agent.send_message_stream",
        "send_message",
        "send_message_stream",
        "session.delete",
        "session.rename",
        "session.replace_messages",
        "session.regenerate_from",
        "session.rollback",
        "delete_session",
        "rename_session",
        "rollback_session",
    }
)
NETWORK_IDEMPOTENCY_METHODS = frozenset(
    {
        "agent.send_message",
        "agent.send_message_stream",
        "message.status",
        "send_message",
        "send_message_stream",
        "world.send_message",
        "world.send_message_stream",
    }
)

_NETWORK_RESOURCE_PREFIXES = (
    "agent.",
    "session.",
    "message.",
    "memory.",
    "scene.",
    "initiative_timer.",
    "model.",
    "media.",
    "world.",
)
_NETWORK_RESOURCE_LEGACY_METHODS = frozenset(
    {
        "send_message",
        "send_message_stream",
        "create_session",
        "list_sessions",
        "current_session",
        "resume_session",
        "delete_session",
        "export_session",
        "rename_session",
        "rollback_session",
    }
)
_NETWORK_AGENT_ID_EXEMPT_METHODS = frozenset({"agent.init", "agent.list", "init"})

_MESSAGE_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user_id": {"type": "string"},
        "agent_id": {"type": "string"},
        "role": {"const": "assistant"},
        "content": {"type": "string"},
        "reasoning_content": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "message_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "generation_id": {"type": "string"},
        "idempotent_replay": {"type": "boolean"},
        "session": {"type": "object"},
    },
    "required": [
        "role",
        "content",
        "generation_id",
        "idempotent_replay",
        "session",
    ],
    "additionalProperties": True,
}

_WORLD_MESSAGE_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user_id": {"type": "string"},
        "agent_id": {"type": "string"},
        "world_id": {"type": "string"},
        "session_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "actor_id": {"type": "string"},
                    "actor_name": {"type": "string"},
                    "scene_id": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["actor_id", "actor_name", "scene_id", "content"],
                "additionalProperties": True,
            },
        },
        "waiting_for_user": {"type": "boolean"},
        "generation_id": {"type": "string"},
        "idempotent_replay": {"type": "boolean"},
    },
    "required": [
        "world_id",
        "turns",
        "waiting_for_user",
        "generation_id",
        "idempotent_replay",
    ],
    "additionalProperties": True,
}

_PUBLIC_RESULT_SCHEMAS: dict[str, dict[str, Any]] = {
    "agent.send_message": _MESSAGE_RESULT_SCHEMA,
    "agent.send_message_stream": _MESSAGE_RESULT_SCHEMA,
    "world.send_message": _WORLD_MESSAGE_RESULT_SCHEMA,
    "world.send_message_stream": _WORLD_MESSAGE_RESULT_SCHEMA,
    "message.status": {
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
            "agent_id": {"type": "string"},
            "operation_id": {"type": "string"},
            "session_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
            "generation_id": {"type": "string"},
            "status": {"enum": ["pending", "succeeded", "failed", "cancelled"]},
            "created_at": {"type": "string"},
            "updated_at": {"type": "string"},
            "result": {"anyOf": [{"type": "object"}, {"type": "null"}]},
            "error": {"anyOf": [{"type": "object"}, {"type": "null"}]},
        },
        "required": [
            "operation_id",
            "session_id",
            "idempotency_key",
            "generation_id",
            "status",
            "created_at",
            "updated_at",
            "result",
            "error",
        ],
        "additionalProperties": False,
    },
}


class RpcError(Exception):
    """Runtime RPC 结构化错误。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "runtime.error",
        user_message: str | None = None,
        recoverable: bool = True,
        action_hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.technical_message = message
        self.user_message = user_message or message
        self.recoverable = recoverable
        self.action_hint = action_hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "error_code": self.code,
            "message": self.user_message,
            "technical_message": self.technical_message,
            "user_message": self.user_message,
            "recoverable": self.recoverable,
            "action_hint": self.action_hint,
            "details": dict(self.details),
        }


class RpcMethodNotFoundError(ValueError):
    """Raised when a runtime RPC method is not registered."""

    def __init__(self, method: str) -> None:
        super().__init__(f"Unknown method: {method}")
        self.method = method
        self.code = "method_not_found"
        self.technical_message = f"Unknown method: {method}"
        self.user_message = "请求的 Runtime RPC 方法不存在。"
        self.details = {"method": method, "allowed_methods": rpc_methods(include_legacy=True)}
        self.recoverable = True
        self.action_hint = "请改用 runtime.info 返回的 methods 或 legacy_methods 中列出的方法。"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "error_code": self.code,
            "message": self.user_message,
            "technical_message": self.technical_message,
            "user_message": self.user_message,
            "recoverable": self.recoverable,
            "action_hint": self.action_hint,
            "details": dict(self.details),
        }


def rpc_methods(*, include_legacy: bool = False) -> list[str]:
    """Return public runtime RPC method names."""

    return [spec.method for spec in RPC_METHOD_SPECS if include_legacy or not spec.legacy]


def external_tool_status_methods() -> dict[str, str]:
    """Return external tool lifecycle status to Runtime event-name mapping."""

    return dict(_EXTERNAL_TOOL_STATUS_METHODS)


def legacy_rpc_methods() -> list[str]:
    """Return backward-compatible legacy runtime RPC method names."""

    return [spec.method for spec in RPC_METHOD_SPECS if spec.legacy]


def rpc_method_specs(
    target: RuntimeRpcTarget | None = None,
    *,
    network: bool = False,
) -> list[dict[str, Any]]:
    """Return machine-readable method metadata for documentation clients."""

    result = []
    for spec in RPC_METHOD_SPECS:
        payload: dict[str, Any] = {
            "method": spec.method,
            "handler": spec.handler_name,
            "legacy": spec.legacy,
            "namespace": spec.namespace,
            "deprecated": spec.deprecated,
            "replacement": spec.replacement,
            "remove_after": spec.remove_after,
            "remote_admin": spec.remote_admin,
        }
        if target is not None:
            handler = getattr(target, spec.handler_name, None)
            if handler is not None:
                params_schema, result_schema = _handler_schemas(handler)
                if network:
                    params_schema = _network_params_schema(spec.method, params_schema)
                payload["params_schema"] = params_schema
                explicit_result = _PUBLIC_RESULT_SCHEMAS.get(spec.method)
                payload["result_schema"] = explicit_result or result_schema
                payload["result_schema_complete"] = explicit_result is not None
                payload["contract_scope"] = "network" if network else "local"
        result.append(payload)
    return result


def remote_admin_rpc_methods() -> frozenset[str]:
    """Return RPC methods disabled on remote transports unless explicitly enabled."""

    return frozenset(spec.method for spec in RPC_METHOD_SPECS if spec.remote_admin)


def network_rpc_requirements(method: str) -> frozenset[str]:
    """Return parameters injected or required by the remote resource model."""

    required: set[str] = set()
    is_resource_method = method.startswith(_NETWORK_RESOURCE_PREFIXES) or method in (
        _NETWORK_RESOURCE_LEGACY_METHODS
    )
    if is_resource_method and method not in _NETWORK_AGENT_ID_EXEMPT_METHODS:
        required.add("agent_id")
    if method in NETWORK_SESSION_METHODS or method.startswith(
        ("memory.", "scene.", "initiative_timer.")
    ):
        required.add("session_id")
    if method in NETWORK_REVISION_METHODS:
        required.add("expected_revision")
    if method in NETWORK_IDEMPOTENCY_METHODS:
        required.add("idempotency_key")
    return frozenset(required)


def _network_params_schema(method: str, schema: dict[str, Any]) -> dict[str, Any]:
    network_schema = {
        **schema,
        "properties": dict(schema.get("properties", {})),
        "required": list(schema.get("required", [])),
    }
    properties = network_schema["properties"]
    requirements = network_rpc_requirements(method)
    if method in {"agent.init", "init"}:
        properties["agent_id"] = {"type": "string", "minLength": 1, "maxLength": 128}
    for name in requirements:
        if name == "agent_id":
            properties[name] = {"type": "string", "minLength": 1, "maxLength": 128}
        elif name == "session_id":
            properties[name] = {"type": "string", "minLength": 1}
        elif name == "expected_revision":
            properties[name] = {"type": "integer", "minimum": 0}
        elif name == "idempotency_key":
            properties[name] = {"type": "string", "minLength": 1, "maxLength": 128}
        if name not in network_schema["required"]:
            network_schema["required"].append(name)
    network_schema["required"].sort()
    if not network_schema["required"]:
        network_schema.pop("required", None)
    return network_schema


def _handler_schemas(handler: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    signature = inspect.signature(handler)
    try:
        hints = get_type_hints(handler)
    except NameError, TypeError:
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if name == "self" or parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        schema = _annotation_schema(hints.get(name, parameter.annotation))
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
        else:
            schema = {**schema, "default": parameter.default}
        properties[name] = schema
    params_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        params_schema["required"] = required
    return params_schema, _annotation_schema(hints.get("return", signature.return_annotation))


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation in {Any, inspect.Signature.empty, inspect.Parameter.empty}:
        return {}
    if annotation is None or annotation is type(None):
        return {"type": "null"}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {types.UnionType, Union}:
        return {"anyOf": [_annotation_schema(argument) for argument in args]}
    if origin is list:
        return {"type": "array", "items": _annotation_schema(args[0] if args else Any)}
    if origin is dict:
        return {
            "type": "object",
            "additionalProperties": _annotation_schema(args[1] if len(args) > 1 else Any),
        }
    if origin is AsyncIterator:
        return {"type": "object", "x-stream": True}
    schema_type = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
        list: "array",
    }.get(annotation)
    return {"type": schema_type} if schema_type else {}


def deprecated_rpc_methods() -> list[dict[str, Any]]:
    """Return deprecated public methods with migration metadata."""

    return [spec for spec in rpc_method_specs() if spec["deprecated"]]


def runtime_protocol_metadata() -> dict[str, Any]:
    """Return versioned Runtime protocol metadata for clients and docs."""

    return {
        "protocol_version": RUNTIME_PROTOCOL_VERSION,
        "protocol_major_version": RUNTIME_PROTOCOL_MAJOR_VERSION,
        "deprecated_methods": deprecated_rpc_methods(),
        "breaking_changes": [dict(change) for change in RUNTIME_BREAKING_CHANGES],
    }


def runtime_error_to_dict(error: Exception) -> dict[str, Any]:
    """将 Runtime 边界异常规范化为兼容旧字符串字段的结构化错误。"""
    to_dict = getattr(error, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            data.setdefault("message", str(error))
            data.setdefault("error", data.get("technical_message") or str(error))
            return data

    if isinstance(error, ToolExecutionError):
        data = error.error.to_dict()
        data["code"] = data["error_code"]
        data["message"] = data["user_message"]
        data["error"] = data["technical_message"]
        return data

    code = getattr(error, "code", "runtime.error")
    details = getattr(error, "details", {}) or {}
    recoverable = getattr(error, "recoverable", True)
    technical_message = getattr(error, "technical_message", str(error))
    user_message = getattr(error, "user_message", str(error))
    action_hint = getattr(error, "action_hint", None)
    return {
        "code": code,
        "error_code": code,
        "message": user_message,
        "error": technical_message,
        "technical_message": technical_message,
        "user_message": user_message,
        "recoverable": recoverable,
        "action_hint": action_hint,
        "details": dict(details) if isinstance(details, dict) else {"details": details},
    }


def runtime_error_response(error: Exception) -> dict[str, Any]:
    """构造 Runtime RPC 错误返回，保留旧 error 字符串并新增结构化 error_object。"""
    error_object = runtime_error_to_dict(error)
    return {
        "ok": False,
        "error": error_object.get("error") or error_object.get("technical_message") or str(error),
        "error_code": error_object.get("error_code") or error_object.get("code"),
        "error_object": error_object,
    }


def resolve_rpc_handler(
    target: RuntimeRpcTarget,
    method: str,
) -> Callable[..., Awaitable[Any]]:
    """Resolve a public RPC method name to an async handler on ``target``."""

    for spec in RPC_METHOD_SPECS:
        if spec.method == method:
            handler = getattr(target, spec.handler_name)
            return handler
    raise RpcMethodNotFoundError(method)


async def dispatch_rpc(
    target: RuntimeRpcTarget,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    structured_errors: bool = False,
) -> Any:
    """Dispatch a JSON-compatible RPC request to a runtime service target."""

    try:
        handler = resolve_rpc_handler(target, method)
        return await handler(**(params or {}))
    except Exception as error:
        if structured_errors:
            return runtime_error_response(error)
        raise
