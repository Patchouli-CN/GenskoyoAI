"""Frontend-agnostic runtime service for GensokyoAI.

This module is the public backend boundary for local clients, desktop apps,
web adapters, CLIs, and third-party frontends. It intentionally contains no
Flutter-specific behavior. Clients should interact with it through a stable RPC
transport such as ``bridge_main.py`` or a future HTTP/WebSocket adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from msgspec import Struct, to_builtins

from GensokyoAI.core.agent import Agent
from GensokyoAI.core.agent.model_registry import ModelRegistryService
from GensokyoAI.core.agent.types import ModelInfo
from GensokyoAI.core.character_package import CharacterPackageService
from GensokyoAI.core.character_validator import CharacterValidator
from GensokyoAI.core.config import ConfigLoader
from GensokyoAI.core.config_validator import ConfigDiagnostic, ConfigValidator
from GensokyoAI.core.events import Event, EventBus, SystemEvent
from GensokyoAI.core.migrations import migration_diagnostics_summary
from GensokyoAI.core.schema_versions import (
    CONFIG_SCHEMA_VERSION,
    MEMORY_SCHEMA_VERSION,
    SESSION_EXPORT_FORMAT,
    SESSION_EXPORT_SCHEMA_VERSION,
    SESSION_SCHEMA_VERSION,
    schema_versions_payload,
)
from GensokyoAI.core.version import package_version
from GensokyoAI.runtime.auth import authorize_rpc, current_principal
from GensokyoAI.runtime.dependencies import InstallScope, dependency_status, install_dependencies
from GensokyoAI.runtime.event_contract import sanitize_event_payload
from GensokyoAI.runtime.event_store import RuntimeEventStore
from GensokyoAI.runtime.media_store import MediaStore
from GensokyoAI.runtime.operation_store import RuntimeOperationStore
from GensokyoAI.runtime.resource_control import (
    ResourceGate,
    ResourceLimitError,
    build_resource_gates,
    resource_limit_payload,
    resource_scope,
)
from GensokyoAI.runtime.rpc import (
    NETWORK_IDEMPOTENCY_METHODS,
    NETWORK_REVISION_METHODS,
    NETWORK_SESSION_METHODS,
    RpcError,
    dispatch_rpc,
    legacy_rpc_methods,
    rpc_method_specs,
    rpc_methods,
    runtime_error_to_dict,
    runtime_protocol_metadata,
)
from GensokyoAI.session.context import SessionContext
from GensokyoAI.tools.external_manager import ExternalToolManager
from GensokyoAI.utils.helpers import utc_now
from GensokyoAI.utils.logger import logger
from GensokyoAI.world.persistence import WorldPersistence
from GensokyoAI.world.types import USER_OCCUPANT_ID
from GensokyoAI.world.world import GensokyoWorld, WorldAssemblyError

RUNTIME_EVENT_BACKPRESSURE_DROPPED = "runtime.backpressure.dropped"
MAX_TENANT_AGENTS_PER_USER = 8
RUNTIME_DEPRECATED_FIELDS: tuple[dict[str, str | None], ...] = ()
RUNTIME_COMPATIBILITY_NOTES: tuple[dict[str, str], ...] = (
    {
        "scope": "runtime.rpc.legacy_methods",
        "status": "deprecated",
        "message": "Legacy non-namespaced RPC methods remain available for compatibility; new clients should use namespaced methods from runtime.info.methods.",
        "replacement": "Use runtime.info.method_specs to map legacy methods to namespaced replacements.",
    },
)


class RuntimeState(Struct):
    """Mutable state owned by a single runtime service instance."""

    root_dir: Path
    config_path: Path | None = None
    character_path: Path | None = None
    agent: Agent | None = None
    # 多角色 World 模式；与单角色 agent 互斥（init 时硬校验）。
    # root 服务的 state.agent 必须保持为 None 才能走网络租户路由，
    # world 字段绝不影响 _uses_network_tenancy 的判定。
    world: GensokyoWorld | None = None
    started: bool = False


def runtime_deprecated_fields() -> list[dict[str, str | None]]:
    """Return Runtime public field deprecation metadata."""

    return [dict(item) for item in RUNTIME_DEPRECATED_FIELDS]


def runtime_compatibility_notes() -> list[dict[str, str]]:
    """Return Runtime compatibility notes for clients and release docs."""

    return [dict(item) for item in RUNTIME_COMPATIBILITY_NOTES]


class RuntimeService:
    """Frontend-agnostic facade around :class:`GensokyoAI.core.agent.Agent`.

    The service accepts plain JSON-compatible parameters and returns plain
    JSON-compatible payloads. It must not depend on a concrete frontend or UI
    toolkit. The current Flutter client is only one caller of this API.
    """

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        tenant_key: tuple[str, str] | None = None,
        storage_root: Path | None = None,
    ) -> None:
        self.state = RuntimeState(root_dir=(root_dir or Path.cwd()).resolve())
        self._tenant_key = tenant_key
        self._storage_root = storage_root
        self._tenant_services: dict[tuple[str, str], RuntimeService] = {}
        self._tenant_subscription_owners: dict[str, tuple[RuntimeService, str]] = {}
        self._tenant_operation_lock = asyncio.Lock()
        self._event_store = (
            RuntimeEventStore(storage_root / "events.jsonl") if storage_root is not None else None
        )
        self._media_store = MediaStore(storage_root / "media") if storage_root is not None else None
        self._operation_store = (
            RuntimeOperationStore(storage_root / "operations.json")
            if storage_root is not None
            else None
        )
        self._event_store_subscription_ids: list[str] = []
        self._recorded_event_payloads: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._model_registry = ModelRegistryService()
        self.external_tool_manager = ExternalToolManager()
        self._runtime_event_subscriptions: dict[str, list[str]] = {}
        self._config_validator = ConfigValidator()
        self._character_validator = CharacterValidator()
        self._character_package_service = CharacterPackageService()
        self._resource_gates = self._build_resource_gates()
        self._draining = False
        self._active_network_operations = 0
        self._drained_event = asyncio.Event()
        self._drained_event.set()
        # world_init 时记录的 World 存档根（world.session.* 未装配 World 时也可读存档）
        self._world_persistence_path: Path | None = None
        if self._tenant_key is None:
            self._load_tenant_catalog()

    async def handle(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        structured_errors: bool = False,
    ) -> Any:
        principal = current_principal()
        if self._uses_network_tenancy(principal.network):
            async with self._network_operation_scope(method):
                authorize_rpc(method, principal)
                return await self._handle_tenant_rpc(method, dict(params or {}))
        return await dispatch_rpc(self, method, params, structured_errors=structured_errors)

    async def _handle_tenant_rpc(self, method: str, params: dict[str, Any]) -> Any:
        principal = current_principal()
        if method in {"runtime.info", "runtime.health", "runtime.ready"}:
            return await dispatch_rpc(self, method, params)
        if method in {"runtime.shutdown", "shutdown"}:
            return await self.shutdown()
        if method == "agent.list":
            return self._tenant_agent_list(principal.user_id)
        if method == "agent.delete":
            return await self._delete_tenant_agent(principal.user_id, params)
        if method in {"agent.init", "init"}:
            return await self._init_tenant_agent(principal.user_id, params)
        if method == "world.init":
            return await self._init_tenant_world(principal.user_id, params)

        if not self._is_tenant_method(method):
            return await dispatch_rpc(self, method, params)
        agent_id = self._pop_required_id(params, "agent_id")
        service = self._require_tenant_service(principal.user_id, agent_id)
        session_id = params.get("session_id")
        if self._requires_explicit_session(method) and not session_id:
            raise RpcError(
                f"Runtime method '{method}' requires session_id",
                code="session.explicit_id_required",
                user_message="网络调用必须明确指定会话。",
                recoverable=True,
                action_hint="请传入 session_id，不要依赖当前会话。",
            )
        if self._requires_expected_revision(method):
            expected_revision = params.get("expected_revision")
            if not isinstance(expected_revision, int) or expected_revision < 0:
                raise RpcError(
                    f"Runtime method '{method}' requires expected_revision",
                    code="session.expected_revision_required",
                    user_message="写操作必须携带读取时的会话修订号。",
                    recoverable=True,
                    action_hint="请从 session.messages 响应读取 revision 后重试。",
                )
        if method in NETWORK_IDEMPOTENCY_METHODS and method != "message.status":
            idempotency_key = params.get("idempotency_key")
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise RpcError(
                    "Network message sends require idempotency_key",
                    code="message.idempotency_key_required",
                    user_message="网络发送必须携带幂等键。",
                    recoverable=True,
                )
        if method == "message.status":
            result = await service.handle(method, params)
            return self._attach_resource_ids(result, principal.user_id, agent_id)
        async with service._tenant_operation_lock:
            if isinstance(session_id, str) and method.startswith(
                ("memory.", "scene.", "initiative_timer.")
            ):
                service._activate_tenant_session(session_id)
                params.pop("session_id", None)
            result = await service.handle(method, params)
        return self._attach_resource_ids(result, principal.user_id, agent_id)

    async def _init_tenant_agent(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        principal = current_principal()
        if not principal.has_role("admin") and any(
            params.get(name) is not None
            for name in ("config_path", "character_path", "model_overrides", "embedding_overrides")
        ):
            raise RpcError(
                "Custom Agent paths and model overrides require the admin role",
                code="authorization.forbidden",
                user_message="普通聊天身份只能从服务端角色目录初始化 Agent。",
                recoverable=False,
                details={"required_role": "admin"},
            )
        agent_id = params.pop("agent_id", None) or str(uuid4())
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("Runtime agent_id must be a non-empty string")
        if len(agent_id.strip()) > 128:
            raise ValueError("Runtime agent_id must not exceed 128 characters")
        agent_id = agent_id.strip()
        key = (user_id, agent_id)
        service = self._tenant_services.get(key)
        if service is None:
            owned_count = sum(owner_id == user_id for owner_id, _ in self._tenant_services)
            if owned_count >= MAX_TENANT_AGENTS_PER_USER:
                raise RpcError(
                    "Runtime per-user Agent limit exceeded",
                    code="agent.limit_exceeded",
                    user_message="当前用户创建的 Agent 数量已达到上限。",
                    recoverable=True,
                    details={"maximum": MAX_TENANT_AGENTS_PER_USER},
                )
            storage_root = self._tenant_storage_root(user_id, agent_id)
            service = RuntimeService(
                self.state.root_dir,
                tenant_key=key,
                storage_root=storage_root,
            )
            self._tenant_services[key] = service
        result = await service.handle("agent.init", params)
        self._save_tenant_manifest(user_id, agent_id, service)
        return self._attach_resource_ids(result, user_id, agent_id)

    async def _init_tenant_world(self, user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """网络路径 world.init：World 状态按 (user_id, agent_id) 租户隔离。"""
        principal = current_principal()
        if not principal.has_role("admin") and params.get("config_path") is not None:
            raise RpcError(
                "Custom World config path requires the admin role",
                code="authorization.forbidden",
                user_message="普通聊天身份只能从服务端默认配置装配 World。",
                recoverable=False,
                details={"required_role": "admin"},
            )
        agent_id = params.pop("agent_id", None) or str(uuid4())
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("Runtime agent_id must be a non-empty string")
        if len(agent_id.strip()) > 128:
            raise ValueError("Runtime agent_id must not exceed 128 characters")
        agent_id = agent_id.strip()
        key = (user_id, agent_id)
        service = self._tenant_services.get(key)
        if service is None:
            owned_count = sum(owner_id == user_id for owner_id, _ in self._tenant_services)
            if owned_count >= MAX_TENANT_AGENTS_PER_USER:
                raise RpcError(
                    "Runtime per-user Agent limit exceeded",
                    code="agent.limit_exceeded",
                    user_message="当前用户创建的 Agent 数量已达到上限。",
                    recoverable=True,
                    details={"maximum": MAX_TENANT_AGENTS_PER_USER},
                )
            storage_root = self._tenant_storage_root(user_id, agent_id)
            service = RuntimeService(
                self.state.root_dir,
                tenant_key=key,
                storage_root=storage_root,
            )
            self._tenant_services[key] = service
        result = await service.handle("world.init", params)
        self._save_tenant_manifest(user_id, agent_id, service)
        return self._attach_resource_ids(result, user_id, agent_id)

    def _tenant_agent_list(self, user_id: str) -> list[dict[str, Any]]:
        return [
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "initialized": service.state.agent is not None,
                "started": service.state.started,
            }
            for (owner_id, agent_id), service in sorted(self._tenant_services.items())
            if owner_id == user_id
        ]

    async def _delete_tenant_agent(
        self,
        user_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = self._pop_required_id(params, "agent_id")
        service = self._tenant_services.pop((user_id, agent_id), None)
        if service is None:
            raise ValueError(f"Runtime Agent does not exist: {agent_id}")
        await service.shutdown()
        manifest = service._storage_root / "agent.json" if service._storage_root else None
        if manifest is not None:
            manifest.unlink(missing_ok=True)
        return {
            "deleted": True,
            "data_retained": True,
            "user_id": user_id,
            "agent_id": agent_id,
        }

    def _require_tenant_service(self, user_id: str, agent_id: str) -> RuntimeService:
        service = self._tenant_services.get((user_id, agent_id))
        if service is None:
            raise RpcError(
                f"Runtime Agent does not exist: {agent_id}",
                code="agent.not_found",
                user_message="指定的 Agent 不存在或不属于当前用户。",
                recoverable=True,
                action_hint="请先调用 agent.list 或 agent.init。",
            )
        return service

    def _tenant_storage_root(self, user_id: str, agent_id: str) -> Path:
        def component(value: str) -> str:
            digest = hashlib.sha256(value.encode()).hexdigest()[:24]
            return digest

        return (
            self.state.root_dir
            / "runtime_data"
            / "users"
            / component(user_id)
            / "agents"
            / component(agent_id)
        )

    def _load_tenant_catalog(self) -> None:
        catalog_root = self.state.root_dir / "runtime_data" / "users"
        if not catalog_root.exists():
            return
        for manifest_path in catalog_root.glob("*/agents/*/agent.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                user_id = manifest["user_id"]
                agent_id = manifest["agent_id"]
                if not isinstance(user_id, str) or not isinstance(agent_id, str):
                    continue
                # 单个租户的持久化损坏（如 operations.json 不可读）只跳过该租户，
                # 绝不拖垮整个进程启动；损坏文件原样保留待人工处理，
                # 不静默重建空账本（那会丢掉幂等恢复语义）
                service = RuntimeService(
                    self.state.root_dir,
                    tenant_key=(user_id, agent_id),
                    storage_root=manifest_path.parent,
                )
            except OSError, KeyError, json.JSONDecodeError:
                continue
            except Exception as error:
                logger.warning(
                    f"⚠️ [Runtime] 租户目录加载失败，已跳过: {manifest_path.parent} ({error})"
                )
                continue
            self._tenant_services[(user_id, agent_id)] = service

    @staticmethod
    def _save_tenant_manifest(
        user_id: str,
        agent_id: str,
        service: RuntimeService,
    ) -> None:
        if service._storage_root is None:
            return
        service._storage_root.mkdir(parents=True, exist_ok=True)
        manifest_path = service._storage_root / "agent.json"
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "character_path": (
                        str(service.state.character_path) if service.state.character_path else None
                    ),
                    "created_at": utc_now().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(manifest_path)

    def _uses_network_tenancy(self, network_request: bool) -> bool:
        return network_request and self._tenant_key is None and self.state.agent is None

    @staticmethod
    def _pop_required_id(params: dict[str, Any], name: str) -> str:
        value = params.pop(name, None)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Runtime {name} is required")
        return value.strip()

    @staticmethod
    def _is_tenant_method(method: str) -> bool:
        return method.startswith(
            (
                "agent.",
                "session.",
                "memory.",
                "scene.",
                "initiative_timer.",
                "model.",
                "media.",
                "message.",
                "world.",
            )
        ) or method in {
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

    @staticmethod
    def _requires_explicit_session(method: str) -> bool:
        return method.startswith(("memory.", "scene.", "initiative_timer.")) or method in (
            NETWORK_SESSION_METHODS
        )

    @staticmethod
    def _requires_expected_revision(method: str) -> bool:
        return method in NETWORK_REVISION_METHODS

    @staticmethod
    def _attach_resource_ids(result: Any, user_id: str, agent_id: str) -> Any:
        if isinstance(result, dict):
            return {"user_id": user_id, "agent_id": agent_id, **result}
        return result

    def _activate_tenant_session(self, session_id: str) -> None:
        agent = self._require_agent()
        if not agent.resume_session(session_id):
            raise ValueError(f"Session does not exist: {session_id}")

    @asynccontextmanager
    async def _network_operation_scope(self, method: str) -> AsyncIterator[None]:
        allowed_while_draining = {
            "runtime.health",
            "runtime.info",
            "runtime.ready",
            "runtime.shutdown",
            "message.status",
        }
        if self._draining and method not in allowed_while_draining:
            raise RpcError(
                "Runtime is draining and is not accepting new operations",
                code="runtime.draining",
                user_message="Runtime 正在停止服务，暂不接受新的操作。",
                recoverable=True,
                action_hint="请稍后连接新的 Runtime 实例，或等待服务恢复就绪。",
                details={"active_operations": self._active_network_operations},
            )
        self._active_network_operations += 1
        self._drained_event.clear()
        try:
            yield
        finally:
            self._active_network_operations = max(0, self._active_network_operations - 1)
            if self._active_network_operations == 0:
                self._drained_event.set()

    def begin_drain(self) -> None:
        """Reject new remote work while allowing active operations to settle."""

        self._draining = True

    async def wait_for_drain(self, timeout: float = 30.0) -> bool:
        """Wait for active remote operations to finish within ``timeout`` seconds."""

        self.begin_drain()
        if self._active_network_operations == 0:
            return True
        try:
            await asyncio.wait_for(self._drained_event.wait(), timeout=max(0.0, timeout))
        except TimeoutError:
            return False
        return True

    async def health(self) -> dict[str, Any]:
        """Return a lightweight runtime health payload."""

        return {
            "ok": True,
            "root_dir": str(self.state.root_dir),
            "initialized": self.state.agent is not None
            or self.state.world is not None
            or bool(self._tenant_services),
            "started": self.state.started
            or any(service.state.started for service in self._tenant_services.values()),
            "active_tenant_agents": len(self._tenant_services),
            "draining": self._draining,
            "active_operations": self._active_network_operations,
        }

    async def readiness(self) -> dict[str, Any]:
        """Return whether this process can accept new remote operations."""

        ready = not self._draining
        return {
            "ok": ready,
            "ready": ready,
            "draining": self._draining,
            "active_operations": self._active_network_operations,
            "active_tenant_agents": len(self._tenant_services),
        }

    async def info(self) -> dict[str, Any]:
        """Return runtime capability information for generic clients."""

        protocol_metadata = runtime_protocol_metadata()
        return {
            "name": "GensokyoAI Runtime",
            "package_version": package_version(self.state.root_dir),
            "protocol": "gensokyo-runtime-rpc",
            **protocol_metadata,
            "capabilities": [
                "agent.lifecycle",
                "agent.messaging",
                "agent.reasoning.public",
                "agent.streaming",
                "character.discovery",
                "character.validation",
                "character_package.management",
                "dependency.management",
                "external_tool.status",
                "memory.management",
                "memory.search",
                "memory.graph",
                "media.upload",
                "media.image_input",
                "message.operation_status",
                "model.discovery",
                "config.validation",
                "migration.diagnostics",
                "resource_control.runtime_gates",
                "runtime.events",
                "runtime.health",
                "runtime.readiness",
                "runtime.graceful_drain",
                "runtime.transport_discovery",
                "runtime.multi_user",
                "runtime.rbac",
                "runtime.versioning",
                "session.management",
                "initiative_timer.management",
                "world.orchestration",
            ],
            "methods": rpc_methods(),
            "legacy_methods": legacy_rpc_methods(),
            "method_specs": rpc_method_specs(self, network=current_principal().network),
            "transports": [
                {"name": "json-lines", "streaming": "aggregate"},
                {"name": "http", "streaming": "aggregate"},
                {"name": "websocket", "streaming": "incremental"},
                {"name": "sse", "streaming": "runtime-events"},
            ],
            "stream_protocol": {
                "version": 2,
                "reasoning_default": "public",
                "start_acknowledgement": True,
                "correlation_fields": ["stream_id", "generation_id"],
                "generation_resume_supported": False,
                "recovery": "message.status then session.messages",
            },
            "event_replay": {
                "scope": "user_id/agent_id",
                "cursor": "sequence",
                "sse_resume_header": "Last-Event-ID",
                "max_replay_events": 1000,
            },
            "resource_hierarchy": ["user_id", "agent_id", "session_id", "message_id"],
            "authentication": {
                "identity_claim": "sub",
                "roles": ["read", "chat", "admin"],
                "network_requires_explicit_agent": True,
                "network_requires_explicit_session": True,
            },
            "media": {
                "upload_path": "/media?agent_id={agent_id}",
                "multipart_field": "file",
                "content_part": {"type": "media", "media_id": "..."},
                "allowed_content_types": sorted(MediaStore.ALLOWED_CONTENT_TYPES),
                "model_input_content_types": ["image/*"],
            },
            "schema_versions": schema_versions_payload(),
            "config_schema_version": CONFIG_SCHEMA_VERSION,
            "deprecated_fields": runtime_deprecated_fields(),
            "compatibility_notes": runtime_compatibility_notes(),
            "migration_diagnostics": migration_diagnostics_summary(),
            "external_tools": self.external_tool_manager.source_status(include_tools=False),
            "resource_control": self._resource_control_payload(),
        }

    async def validate_config(
        self,
        config_path: str | None = None,
        config: dict[str, Any] | None = None,
        model_overrides: dict[str, Any] | None = None,
        embedding_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return structured configuration diagnostics without initializing Agent."""

        loader = ConfigLoader()
        diagnostics: list[ConfigDiagnostic] = []
        resolved_config_path = None
        if config is not None:
            diagnostics.extend(loader.validate_dict(config))
        else:
            resolved_config_path = (
                self._resolve_optional(config_path)
                or self.state.config_path
                or self.state.root_dir / "config" / "default.yaml"
            )
            with open(resolved_config_path, encoding="utf-8") as file:
                config_data = yaml.safe_load(file) or {}
            diagnostics.extend(loader.validate_dict(config_data))

        if model_overrides:
            diagnostics.extend(self._config_validator.validate_model_overrides(model_overrides))
        if embedding_overrides:
            diagnostics.extend(
                self._config_validator.validate_embedding_overrides(embedding_overrides)
            )

        return self._config_validation_payload(
            diagnostics,
            config_path=resolved_config_path,
            source="inline" if config is not None else "file",
        )

    async def validate_character(
        self,
        character_path: str | None = None,
        character: str | None = None,
        character_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return structured character YAML diagnostics and preview."""

        resolved_character_path: Path | None = None
        source = "inline" if character_data is not None else "file"
        if character_data is None:
            resolved_character_path = self._resolve_character(
                character_path=character_path,
                character=character,
            )
            if resolved_character_path is None:
                raise ValueError("Character path or inline character_data is required")
            with open(resolved_character_path, encoding="utf-8") as file:
                character_data = yaml.safe_load(file) or {}

        diagnostics = self._character_validator.validate_character_dict(character_data)
        preview = self._character_validator.build_preview(
            character_data,
            fallback_id=resolved_character_path.stem if resolved_character_path else None,
        )
        return self._character_validation_payload(
            diagnostics,
            character_path=resolved_character_path,
            source=source,
            preview=preview,
        )

    async def validate_character_package(self, package_path: str) -> dict[str, Any]:
        """Return structured diagnostics for a .gensokyo-character package."""

        resolved_package_path = self._resolve_sandboxed_path(package_path)
        return self._character_package_service.validate_package(resolved_package_path)

    async def preview_character_package(self, package_path: str) -> dict[str, Any]:
        """Return manifest and character preview for a .gensokyo-character package."""

        resolved_package_path = self._resolve_sandboxed_path(package_path)
        return self._character_package_service.preview_package(resolved_package_path)

    async def import_character_package(
        self,
        package_path: str,
        locale: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Import a .gensokyo-character package into the Runtime characters directory."""

        resolved_package_path = self._resolve_sandboxed_path(package_path)
        return self._character_package_service.import_package(
            resolved_package_path,
            self.state.root_dir / "characters",
            locale=locale,
            overwrite=overwrite,
        )

    async def import_uploaded_character_package(
        self,
        data: bytes,
        *,
        filename: str,
        locale: str | None = None,
        overwrite: bool = False,
        allow_untrusted: bool = False,
    ) -> dict[str, Any]:
        """Validate and import a remotely uploaded character package."""

        if not filename.endswith(".gensokyo-character"):
            raise ValueError("Uploaded character package must use .gensokyo-character")
        upload_dir = self.state.root_dir / "runtime_data" / "character_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        package_path = upload_dir / f"{uuid4()}.gensokyo-character"
        package_path.write_bytes(data)
        try:
            validation = self._character_package_service.validate_package(package_path)
            if not validation.get("ok"):
                raise RpcError(
                    "Uploaded character package failed validation",
                    code="character_package.validation_failed",
                    user_message="角色包未通过安全与格式校验。",
                    recoverable=False,
                    details={"diagnostics": validation.get("diagnostics", [])},
                )
            security = validation.get("security", {})
            verification = security.get("signature_verification", "none")
            if verification != "verified" and not allow_untrusted:
                raise RpcError(
                    "Uploaded character package has no cryptographically verified signature",
                    code="character_package.untrusted",
                    user_message="角色包签名尚未得到密码学验证。",
                    recoverable=True,
                    action_hint="管理员审阅来源与校验结果后，可显式设置 allow_untrusted=true。",
                    details={"signature_verification": verification},
                )
            imported = self._character_package_service.import_package(
                package_path,
                self.state.root_dir / "characters",
                locale=locale,
                overwrite=overwrite,
            )
            return {
                **imported,
                "uploaded_filename": Path(filename).name,
                "trust": {
                    "signature_verification": verification,
                    "untrusted_override": verification != "verified" and allow_untrusted,
                },
            }
        finally:
            package_path.unlink(missing_ok=True)

    async def export_character_package(
        self,
        character_path: str,
        output_path: str,
        package_id: str | None = None,
        author: str | None = None,
        license: str | None = None,
        assets: list[str] | None = None,
        overwrite: bool = False,
        source: str | None = None,
        author_url: str | None = None,
        license_url: str | None = None,
        license_detail: str | None = None,
        attribution: list[dict[str, Any]] | None = None,
        external_links: list[dict[str, Any]] | None = None,
        repository: dict[str, Any] | None = None,
        signature: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Export a character YAML file as a .gensokyo-character package."""

        resolved_character_path = self._resolve_sandboxed_path(character_path)
        resolved_output_path = self._resolve_sandboxed_path(output_path)
        resolved_assets = [self._resolve_sandboxed_path(asset) for asset in assets or []]
        return self._character_package_service.export_package(
            resolved_character_path,
            resolved_output_path,
            package_id=package_id,
            author=author,
            license=license,
            assets=resolved_assets,
            overwrite=overwrite,
            source=source,
            author_url=author_url,
            license_url=license_url,
            license_detail=license_detail,
            attribution=attribution,
            external_links=external_links,
            repository=repository,
            signature=signature,
        )

    async def init(
        self,
        config_path: str | None = None,
        character_path: str | None = None,
        character: str | None = None,
        session_id: str | None = None,
        new_session: bool = False,
        start: bool = True,
        model_overrides: dict[str, Any] | None = None,
        embedding_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Initialize the Agent and prepare a session.

        A current session must exist before ``Agent.start()`` because the semantic
        memory and think engine are session-scoped.
        """
        async with self._lock:
            # 与 World 模式互斥：同一 RuntimeService 实例绝不同时持有两者
            if self.state.world is not None:
                raise RpcError(
                    "Runtime World and Agent modes are mutually exclusive",
                    code="world.world_mode_active",
                    user_message="当前 Runtime 已装配多角色 World，请先 world.shutdown 再初始化 Agent。",
                    recoverable=False,
                )
            if self.state.agent is not None:
                await self._shutdown_locked()

            config_file = (
                self._resolve_optional(config_path)
                or self.state.root_dir / "config" / "default.yaml"
            )
            char_file = self._resolve_character(
                character_path=character_path,
                character=character,
            )

            loader = ConfigLoader()
            config = loader.load(config_file)
            if self._storage_root is not None:
                config.session.save_path = self._storage_root / "sessions"
                config.session.save_path.mkdir(parents=True, exist_ok=True)
            self._apply_model_overrides(config.model, model_overrides)
            self._apply_embedding_overrides(config.embedding, embedding_overrides)
            agent = Agent(config=config, config_file=config_file, character_file=char_file)

            if session_id:
                if not agent.resume_session(session_id):
                    raise ValueError(f"Session does not exist: {session_id}")
            elif new_session or not agent.session_manager.list_sessions():
                agent.create_session()
            else:
                sessions = agent.session_manager.list_sessions()
                latest = max(sessions, key=lambda item: item.last_active)
                agent.session_manager.set_current_session(latest.session_id)

            if start:
                await agent.start()
                self.state.started = True

            self.state.agent = agent
            self.state.config_path = config_file
            self.state.character_path = char_file
            self._resource_gates = agent.runtime_context.resource_gates
            agent.runtime_context.model_client.update_resource_gates(self._resource_gates)
            agent.runtime_context.tool_executor.update_resource_gates(self._resource_gates)
            self._start_event_recording(agent.event_bus)

            current = agent.session_manager.get_current_session()
            character_name = agent.config.character.name if agent.config.character else None
            return {
                "character": self._character_payload(char_file, character_name),
                "session": self._session_payload(current) if current else None,
                "started": self.state.started,
            }

    async def list_characters(self, locale: str | None = None) -> list[dict[str, Any]]:
        characters_dir = self.state.root_dir / "characters"
        search_dirs = []
        if locale:
            search_dirs.append(characters_dir / locale)
        search_dirs.append(characters_dir)
        if characters_dir.exists():
            search_dirs.extend(path for path in characters_dir.iterdir() if path.is_dir())

        seen: set[Path] = set()
        characters: list[dict[str, Any]] = []
        for directory in search_dirs:
            if not directory.exists():
                continue
            for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    with open(path, encoding="utf-8") as file:
                        data = yaml.safe_load(file) or {}
                    diagnostics = self._character_validator.validate_character_dict(data)
                    preview = (
                        self._character_validator.build_preview(data, fallback_id=path.stem) or {}
                    )
                    characters.append(
                        {
                            "id": path.stem,
                            "name": preview.get("name") or path.stem,
                            "path": str(path.relative_to(self.state.root_dir)),
                            "greeting": data.get("greeting", "") if isinstance(data, dict) else "",
                            "metadata": preview.get("metadata", {}),
                            "preview": preview,
                            "diagnostics": [item.to_dict() for item in diagnostics],
                            "ok": not any(item.severity == "error" for item in diagnostics),
                        }
                    )
                except yaml.YAMLError as exc:  # keep listing robust for broken user files
                    diagnostic = ConfigDiagnostic(
                        code="character.yaml.invalid",
                        path="$",
                        severity="error",
                        message=f"Character YAML is invalid: {exc}",
                        suggestion="请检查 YAML 缩进、冒号和列表格式。",
                    )
                    characters.append(
                        {
                            "id": path.stem,
                            "name": path.stem,
                            "path": str(path.relative_to(self.state.root_dir)),
                            "error": str(exc),
                            "diagnostics": [diagnostic.to_dict()],
                            "ok": False,
                        }
                    )
                except Exception as exc:  # keep listing robust for broken user files
                    diagnostic = ConfigDiagnostic(
                        code="character.load.failed",
                        path="$",
                        severity="error",
                        message=str(exc),
                        suggestion="请确认角色文件可读取且格式正确。",
                    )
                    characters.append(
                        {
                            "id": path.stem,
                            "name": path.stem,
                            "path": str(path.relative_to(self.state.root_dir)),
                            "error": str(exc),
                            "diagnostics": [diagnostic.to_dict()],
                            "ok": False,
                        }
                    )
        return characters

    async def list_agents(self) -> list[dict[str, Any]]:
        """List the local bridge Agent; network callers are routed per user."""

        if self.state.agent is None:
            return []
        return [{"agent_id": "local", "initialized": True, "started": self.state.started}]

    async def delete_agent(self, agent_id: str = "local") -> dict[str, Any]:
        """Delete the local bridge Agent; network callers are routed per user."""

        if agent_id != "local" or self.state.agent is None:
            raise ValueError(f"Runtime Agent does not exist: {agent_id}")
        await self.shutdown()
        return {"deleted": True, "agent_id": agent_id}

    async def upload_media(
        self,
        agent_id: str,
        data: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        principal = current_principal()
        if not self._uses_network_tenancy(principal.network):
            raise RuntimeError("Media upload is only available on the network Runtime")
        service = self._require_tenant_service(principal.user_id, agent_id)
        if service._media_store is None:
            raise RuntimeError("Runtime media storage is unavailable")
        item = service._media_store.put(data, filename=filename, content_type=content_type)
        return {"user_id": principal.user_id, "agent_id": agent_id, **item}

    async def get_media(self, agent_id: str, media_id: str) -> tuple[dict[str, Any], bytes]:
        principal = current_principal()
        service = self._require_tenant_service(principal.user_id, agent_id)
        if service._media_store is None:
            raise RuntimeError("Runtime media storage is unavailable")
        return service._media_store.get(media_id)

    async def media_list(self) -> list[dict[str, Any]]:
        if self._media_store is None:
            return []
        return self._media_store.list()

    async def media_delete(self, media_id: str) -> dict[str, Any]:
        if self._media_store is None or not self._media_store.delete(media_id):
            raise ValueError(f"Runtime media does not exist: {media_id}")
        return {"deleted": True, "media_id": media_id}

    async def list_models(
        self,
        refresh: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return current runtime model metadata through ModelRegistryService."""
        agent = self._require_agent()
        config = agent.config.model
        models = await self._model_registry.list_models(
            config,
            refresh=refresh,
            overrides=overrides,
        )
        return {
            "provider": config.provider,
            "model": config.name,
            "models": [self._model_payload(model) for model in models],
        }

    async def model_info(
        self,
        model_id: str | None = None,
        refresh: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return metadata for one model in the current runtime provider."""
        agent = self._require_agent()
        config = agent.config.model
        model = await self._model_registry.get_model_info(
            config,
            model_id=model_id,
            refresh=refresh,
            overrides=overrides,
        )
        return {
            "provider": config.provider,
            "requested_model": model_id or config.name,
            "model": self._model_payload(model),
        }

    async def create_session(self) -> dict[str, Any]:
        agent = self._require_agent()
        async with self._lock:
            session = agent.create_session()
            return self._session_payload(session)

    async def list_sessions(
        self,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        agent = self._require_agent()
        limit = self._validate_page_limit(limit, maximum=200)
        sessions = sorted(
            agent.session_manager.list_sessions(),
            key=lambda item: (item.last_active, item.session_id),
            reverse=True,
        )
        start = self._cursor_start(
            [session.session_id for session in sessions],
            cursor,
            resource="session",
        )
        page = sessions[start : start + limit]
        has_more = start + len(page) < len(sessions)
        return {
            "sessions": [self._session_payload(session) for session in page],
            "next_cursor": page[-1].session_id if page and has_more else None,
            "has_more": has_more,
        }

    async def current_session(self, session_id: str | None = None) -> dict[str, Any] | None:
        agent = self._require_agent()
        session = (
            agent.session_manager.get_session(session_id)
            if session_id
            else agent.session_manager.get_current_session()
        )
        return self._session_payload(session) if session else None

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        agent = self._require_agent()
        async with self._lock:
            if not agent.resume_session(session_id):
                raise ValueError(f"Session does not exist: {session_id}")
            session = agent.session_manager.get_current_session()
            return self._session_payload(session) if session else {}

    async def delete_session(
        self,
        session_id: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if not session_id:
            raise ValueError("Session id is required")

        agent = self._require_agent()
        async with self._lock:
            self._assert_session_revision(agent.session_manager, session_id, expected_revision)
            current = agent.session_manager.get_current_session()
            was_current = bool(current and current.session_id == session_id)
            deleted = agent.session_manager.delete_session(session_id)
            if not deleted:
                raise ValueError(f"Session does not exist: {session_id}")
            next_current = agent.session_manager.get_current_session()
            remaining_sessions = [
                self._session_payload(session) for session in agent.session_manager.list_sessions()
            ]
            return {
                "deleted": True,
                "session_id": session_id,
                "was_current": was_current,
                "current_session": self._session_payload(next_current) if next_current else None,
                "remaining_count": len(remaining_sessions),
                "remaining_sessions": remaining_sessions,
            }

    async def export_session(self, session_id: str | None = None) -> dict[str, Any]:
        agent = self._require_agent()
        manager = agent.session_manager
        current = manager.get_current_session()
        target_session_id = session_id or (current.session_id if current else None)
        if not target_session_id:
            raise ValueError("No active session to export")

        if current and current.session_id == target_session_id:
            manager.save_current()

        session = manager.get_session(target_session_id)
        if session is None:
            raise ValueError(f"Session does not exist: {target_session_id}")

        messages = manager.persistence.load_messages(target_session_id)
        is_current = bool(current and current.session_id == target_session_id)
        character_name = agent.config.character.name if agent.config.character else None
        return {
            "format": SESSION_EXPORT_FORMAT,
            "version": SESSION_EXPORT_SCHEMA_VERSION,
            "schema_version": SESSION_EXPORT_SCHEMA_VERSION,
            "session_schema_version": SESSION_SCHEMA_VERSION,
            "memory_schema_version": MEMORY_SCHEMA_VERSION,
            "exported_at": utc_now().isoformat(),
            "is_current": is_current,
            "character": self._character_payload(self.state.character_path, character_name),
            "session": self._session_payload(session),
            "messages": [self._public_message(message) for message in messages],
            "message_count": len(messages),
            "runtime": {
                "root_dir": str(self.state.root_dir),
                "config_path": str(self.state.config_path) if self.state.config_path else None,
                "character_path": (
                    str(self.state.character_path) if self.state.character_path else None
                ),
                "started": self.state.started,
            },
        }

    async def rename_session(
        self,
        title: str,
        session_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("Session title is required")

        agent = self._require_agent()
        manager = agent.session_manager
        current = manager.get_current_session()
        target_session_id = session_id or (current.session_id if current else None)
        if not target_session_id:
            raise ValueError("No active session to rename")

        async with self._lock:
            session = manager.get_session(target_session_id)
            if session is None:
                raise ValueError(f"Session does not exist: {target_session_id}")
            self._assert_session_revision(manager, target_session_id, expected_revision)
            session.metadata["title"] = normalized_title
            session.revision += 1
            session.touch()
            manager.persistence.save_session(session)
            return self._session_payload(session)

    async def session_messages(
        self,
        session_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one stable page of editable messages for a session."""
        agent = self._require_agent()
        manager = agent.session_manager
        current = manager.get_current_session()
        target_session_id = session_id or (current.session_id if current else None)
        if not target_session_id:
            raise ValueError("No active session to read messages")

        session = manager.get_session(target_session_id)
        if session is None:
            raise ValueError(f"Session does not exist: {target_session_id}")

        limit = self._validate_page_limit(limit, maximum=500)
        messages = manager.persistence.load_messages(target_session_id)
        start = self._cursor_start(
            [str(message.get("message_id", "")) for message in messages],
            cursor,
            resource="message",
        )
        page = messages[start : start + limit]
        has_more = start + len(page) < len(messages)
        return {
            **self._session_messages_payload(manager, session, page),
            "total_message_count": len(messages),
            "next_cursor": page[-1].get("message_id") if page and has_more else None,
            "has_more": has_more,
        }

    async def session_replace_messages(
        self,
        messages: list[dict[str, Any]],
        session_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Replace all messages in a session after frontend-side edits."""
        agent = self._require_agent()
        manager = agent.session_manager
        current = manager.get_current_session()
        target_session_id = session_id or (current.session_id if current else None)
        if not target_session_id:
            raise ValueError("No active session to replace messages")

        session = manager.get_session(target_session_id)
        if session is None:
            raise ValueError(f"Session does not exist: {target_session_id}")

        normalized_messages = [
            self._resolve_persisted_message(message)
            for message in self._normalize_session_messages(messages)
        ]
        async with self._lock:
            self._assert_session_revision(manager, target_session_id, expected_revision)
            if not manager.replace_messages(target_session_id, normalized_messages):
                raise ValueError(f"Session does not exist: {target_session_id}")
            updated_session = manager.get_session(target_session_id) or session
            updated_messages = manager.persistence.load_messages(target_session_id)
            return {
                "replaced": True,
                **self._session_messages_payload(manager, updated_session, updated_messages),
            }

    async def session_regenerate_from(
        self,
        message_index: int,
        session_id: str | None = None,
        system_contexts: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Truncate from a historical user message and regenerate following assistant reply."""
        if message_index < 0:
            raise ValueError("Message index must be greater than or equal to 0")

        async with (
            self._resource_scope("runtime", "agent_message"),
            self._resource_scope("agent_message", "agent_message"),
        ):
            agent = await self._ensure_started()
            manager = agent.session_manager
            current = manager.get_current_session()
            target_session_id = session_id or (current.session_id if current else None)
            if not target_session_id:
                raise ValueError("No active session to regenerate messages")

            session = manager.get_session(target_session_id)
            if session is None:
                raise ValueError(f"Session does not exist: {target_session_id}")
            self._assert_session_revision(manager, target_session_id, expected_revision)

            original_messages = manager.persistence.load_messages(target_session_id)
            if message_index >= len(original_messages):
                raise ValueError("Message index is out of range")

            user_index = self._find_regeneration_user_index(original_messages, message_index)
            if user_index is None:
                raise ValueError("No user message found at or before message_index")

            user_message = original_messages[user_index]
            user_content = user_message.get("content")
            if not isinstance(user_content, str) or not user_content:
                raise ValueError("Regeneration target user message content is required")

            previous_session_id = current.session_id if current else None
            async with self._lock:
                self._activate_session_for_regeneration(agent, target_session_id)
                manager.replace_messages(target_session_id, original_messages[:user_index])

            try:
                response = await agent.send(user_content, system_contexts)
                content = response.content if response else ""
            finally:
                if previous_session_id and previous_session_id != target_session_id:
                    self._activate_session_for_regeneration(agent, previous_session_id)

            updated_session = manager.get_session(target_session_id) or session
            updated_messages = manager.persistence.load_messages(target_session_id)
            return {
                "regenerated": True,
                "from_index": message_index,
                "user_message_index": user_index,
                "role": "assistant",
                "content": content,
                **self._session_messages_payload(manager, updated_session, updated_messages),
            }

    async def rollback_session(
        self,
        num: int = 1,
        mode: str = "turns",
        session_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if num < 1:
            raise ValueError("Rollback num must be greater than or equal to 1")
        if mode not in {"turns", "messages"}:
            raise ValueError("Rollback mode must be either 'turns' or 'messages'")

        agent = self._require_agent()
        async with self._lock:
            if session_id:
                self._activate_tenant_session(session_id)
            session = agent.session_manager.get_current_session()
            if session is None:
                raise ValueError("No active session to rollback")
            self._assert_session_revision(
                agent.session_manager,
                session.session_id,
                expected_revision,
            )
            before_messages = agent.session_manager.get_working_memory().get_context()
            before_total_turns = session.total_turns
            agent.rollback(num=num, mode=mode)  # type: ignore[arg-type]
            agent.session_manager.save_current()
            after_session = agent.session_manager.get_current_session()
            after_messages = agent.session_manager.persistence.load_messages(session.session_id)
            return {
                "rolled_back": True,
                "num": num,
                "mode": mode,
                "before_total_turns": before_total_turns,
                "after_total_turns": after_session.total_turns if after_session else 0,
                "before_message_count": len(before_messages),
                "after_message_count": len(after_messages),
                "message_count": len(after_messages),
                "session": self._session_payload(after_session) if after_session else None,
            }

    async def send_message(
        self,
        message: str | list[dict[str, Any]],
        system_contexts: list[str] | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        async with (
            self._resource_scope("runtime", "agent_message"),
            self._resource_scope("agent_message", "agent_message"),
        ):
            agent = await self._ensure_started()
            async with self._lock:
                if session_id:
                    self._activate_tenant_session(session_id)
                session = agent.session_manager.get_current_session()
                if session is None:
                    raise ValueError("No active session to send a message")
                fingerprint = self._message_request_fingerprint(message, system_contexts)
                replay = self._idempotent_response(
                    agent,
                    session.session_id,
                    idempotency_key,
                    request_fingerprint=fingerprint,
                )
                if replay is not None:
                    return replay
                self._assert_session_revision(
                    agent.session_manager,
                    session.session_id,
                    expected_revision,
                )
                operation_session_id = session.session_id
                generation_id = str(uuid4())
                self._begin_message_operation(
                    operation_session_id,
                    idempotency_key,
                    request_fingerprint=fingerprint,
                    generation_id=generation_id,
                )
                try:
                    resolved_message = self._resolve_message_input(message)
                    response = (
                        await agent.send_multimodal(resolved_message, system_contexts)
                        if isinstance(resolved_message, list)
                        else await agent.send(resolved_message, system_contexts)
                    )
                    content = response.content if response else ""
                    message_payload = self._finalize_message_operation(
                        agent,
                        operation_session_id,
                        idempotency_key,
                        generation_id=generation_id,
                    )
                    session = agent.session_manager.get_current_session()
                    result = {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": getattr(response, "reasoning_content", None),
                        "message_id": message_payload.get("message_id"),
                        "generation_id": generation_id,
                        "idempotent_replay": False,
                        "session": self._session_payload(session) if session else None,
                        "initiative_timer": self._agent_initiative_timer_payload(agent),
                    }
                    self._succeed_message_operation(
                        operation_session_id,
                        idempotency_key,
                        result,
                    )
                    return result
                except asyncio.CancelledError:
                    self._cancel_message_operation(operation_session_id, idempotency_key)
                    raise
                except Exception as error:
                    self._fail_message_operation(operation_session_id, idempotency_key, error)
                    raise

    async def iter_message_stream(
        self,
        message: str | list[dict[str, Any]],
        system_contexts: list[str] | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        agent_id: str | None = None,
        expected_revision: int | None = None,
        *,
        generation_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield Runtime stream events as soon as Agent stream chunks are produced."""

        principal = current_principal()
        if self._uses_network_tenancy(principal.network):
            if not agent_id:
                raise ValueError("Runtime agent_id is required")
            if not session_id:
                raise ValueError("Runtime session_id is required")
            if not isinstance(expected_revision, int) or expected_revision < 0:
                raise ValueError("Runtime expected_revision is required")
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ValueError("Runtime idempotency_key is required")
            service = self._require_tenant_service(principal.user_id, agent_id)
            async with (
                self._network_operation_scope("agent.send_message_stream"),
                service._tenant_operation_lock,
            ):
                # 显式持有并关闭内层流：消费者提前关闭本生成器时，
                # 确定性关闭链保证 service 层账本被收敛，而非依赖 GC 终结
                inner_stream = cast(
                    AsyncGenerator[dict[str, Any]],
                    service.iter_message_stream(
                        message,
                        system_contexts,
                        session_id=session_id,
                        idempotency_key=idempotency_key,
                        expected_revision=expected_revision,
                        generation_id=generation_id,
                    ),
                )
                try:
                    async for event in inner_stream:
                        yield {
                            "user_id": principal.user_id,
                            "agent_id": agent_id,
                            **event,
                        }
                finally:
                    await inner_stream.aclose()
            return

        await self._ensure_started()
        async with (
            self._resource_scope("runtime", "agent_stream"),
            self._resource_scope("agent_message", "agent_stream"),
            self._resource_scope("stream", "agent_stream"),
            self._lock,
        ):
            if session_id:
                self._activate_tenant_session(session_id)
            agent = await self._ensure_started()
            session = agent.session_manager.get_current_session()
            if session is None:
                raise ValueError("No active session to send a message")
            fingerprint = self._message_request_fingerprint(message, system_contexts)
            replay = self._idempotent_response(
                agent,
                session.session_id,
                idempotency_key,
                request_fingerprint=fingerprint,
            )
            if replay is not None:
                yield {
                    "type": "finish",
                    "index": 0,
                    "content": replay.get("content", ""),
                    "reasoning_content": replay.get("reasoning_content"),
                    "message_id": replay.get("message_id"),
                    "generation_id": replay.get("generation_id") or generation_id or str(uuid4()),
                    "idempotent_replay": True,
                    "session": replay.get("session"),
                }
                return
            self._assert_session_revision(
                agent.session_manager,
                session.session_id,
                expected_revision,
            )
            resolved_generation_id = generation_id or str(uuid4())
            self._begin_message_operation(
                session.session_id,
                idempotency_key,
                request_fingerprint=fingerprint,
                generation_id=resolved_generation_id,
            )
            locked_stream = cast(
                AsyncGenerator[dict[str, Any]],
                self._iter_message_stream_locked(
                    message,
                    system_contexts,
                    generation_id=resolved_generation_id,
                    idempotency_key=idempotency_key,
                    session_id=session.session_id,
                ),
            )
            try:
                async for event in locked_stream:
                    yield event
            finally:
                await locked_stream.aclose()

    async def _iter_message_stream_locked(
        self,
        message: str | list[dict[str, Any]],
        system_contexts: list[str] | None = None,
        *,
        generation_id: str,
        idempotency_key: str | None = None,
        session_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        agent = await self._ensure_started()
        full_content = ""
        full_reasoning = ""
        index = 0

        try:
            resolved_message = self._resolve_message_input(message)
            stream = (
                agent.send_multimodal_stream(resolved_message, system_contexts)
                if isinstance(resolved_message, list)
                else agent.send_stream(resolved_message, system_contexts)
            )
            async for chunk in stream:
                event = self._stream_chunk_payload(chunk, index)
                event["generation_id"] = generation_id
                if event.get("type") == "content":
                    full_content += event.get("content", "")
                if reasoning := event.get("reasoning_content"):
                    full_reasoning += reasoning
                yield event
                index += 1
        except GeneratorExit:
            # 消费者中途关闭流（WS 断连/取消落在发送窗口）：把账本收敛为
            # cancelled，避免幂等记录永久 pending 卡死后续同键重试
            self._cancel_message_operation(session_id, idempotency_key)
            raise
        except asyncio.CancelledError:
            self._cancel_message_operation(session_id, idempotency_key)
            yield {
                "type": "cancelled",
                "index": index,
                "content": full_content,
                "reasoning_content": full_reasoning or None,
                "generation_id": generation_id,
            }
            raise
        except Exception as error:
            self._fail_message_operation(session_id, idempotency_key, error)
            yield {
                "type": "error",
                "index": index,
                "content": full_content,
                "reasoning_content": full_reasoning or None,
                "generation_id": generation_id,
                "error": runtime_error_to_dict(error),
            }
            raise

        session = agent.session_manager.get_current_session()
        message_payload = self._finalize_message_operation(
            agent,
            session.session_id if session else "",
            idempotency_key,
            generation_id=generation_id,
        )
        result = {
            "role": "assistant",
            "content": full_content,
            "reasoning_content": full_reasoning or None,
            "generation_id": generation_id,
            "message_id": message_payload.get("message_id"),
            "idempotent_replay": False,
            "session": self._session_payload(session) if session else None,
            "initiative_timer": self._agent_initiative_timer_payload(agent),
        }
        self._succeed_message_operation(session_id, idempotency_key, result)
        yield {
            "type": "finish",
            "index": index,
            **{key: value for key, value in result.items() if key != "role"},
        }

    async def send_message_stream(
        self,
        message: str | list[dict[str, Any]],
        system_contexts: list[str] | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []

        async for event in self.iter_message_stream(
            message,
            system_contexts,
            session_id=session_id,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
        ):
            events.append(event)

        finish_event = events[-1] if events else {}
        session_payload = finish_event.get("session")
        return {
            "role": "assistant",
            "content": finish_event.get("content", ""),
            "reasoning_content": finish_event.get("reasoning_content"),
            "generation_id": finish_event.get("generation_id"),
            "events": events,
            "session": session_payload,
        }

    async def initiative_timer_current(self) -> dict[str, Any] | None:
        agent = self._require_agent()
        return agent.current_initiative_timer()

    async def initiative_timer_update(
        self,
        timer_id: str | None = None,
        delay_seconds: int | float | None = None,
        due_at: str | None = None,
        pending_summary: str | None = None,
    ) -> dict[str, Any]:
        agent = self._require_agent()
        return await agent.update_initiative_timer(
            timer_id=timer_id,
            delay_seconds=delay_seconds,
            due_at=due_at,
            pending_summary=pending_summary,
        )

    async def initiative_timer_cancel(
        self,
        timer_id: str | None = None,
        reason: str = "cancelled",
    ) -> dict[str, Any]:
        agent = self._require_agent()
        return await agent.cancel_initiative_timer(timer_id=timer_id, reason=reason)

    async def initiative_timer_trigger(self, timer_id: str | None = None) -> dict[str, Any]:
        agent = self._require_agent()
        return await agent.trigger_initiative_timer(timer_id=timer_id)

    async def initiative_timer_hesitation(self) -> dict[str, Any]:
        """Deprecated：犹豫链已随对话欲阈值模型退役（remove_after 3.0.0）。"""

        self._require_agent()
        return {
            "enabled": False,
            "deprecated": True,
            "remove_after": "3.0.0",
            "message": "犹豫链已退役：主动发言改由 ThinkEngine 四维心情打分 + drive_threshold 阈值判断。",
        }

    async def initiative_timer_hesitation_set(
        self,
        enabled: bool,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Deprecated：犹豫链已退役，调用被忽略并返回提示。"""

        self._require_agent()
        return {
            "enabled": False,
            "deprecated": True,
            "ignored": True,
            "remove_after": "3.0.0",
            "message": "犹豫链已退役，本次设置未生效：主动发言改由 drive_threshold 阈值判断。",
        }

    # ==================== World 多角色编排 ====================

    async def world_init(
        self,
        config_path: str | None = None,
        session_id: str | None = None,
        start: bool = True,
    ) -> dict[str, Any]:
        """装配（或恢复）多角色 World；与单角色 Agent 模式互斥。"""
        async with self._lock:
            if self.state.agent is not None:
                raise RpcError(
                    "Runtime Agent and World modes are mutually exclusive",
                    code="world.agent_mode_active",
                    user_message="当前 Runtime 已初始化单角色 Agent，请先 shutdown 再装配 World。",
                    recoverable=False,
                )
            if self.state.world is not None:
                await self._shutdown_locked()

            config_file = (
                self._resolve_optional(config_path)
                or self.state.config_path
                or self.state.root_dir / "config" / "default.yaml"
            )
            loader = ConfigLoader()
            config = loader.load(config_file)
            if self._storage_root is not None:
                # 网络租户：World 存档与 Actor 会话根按租户隔离，绝不跨用户共享
                config.world.persistence.save_path = self._storage_root / "world"
                config.world.persistence.save_path.mkdir(parents=True, exist_ok=True)
                config.session.save_path = self._storage_root / "sessions"
                config.session.save_path.mkdir(parents=True, exist_ok=True)
            elif not config.world.persistence.save_path.is_absolute():
                config.world.persistence.save_path = (
                    self.state.root_dir / config.world.persistence.save_path
                )
            try:
                world = (
                    await GensokyoWorld.resume(config, session_id)
                    if session_id
                    else await GensokyoWorld.create(config)
                )
            except WorldAssemblyError as error:
                raise RpcError(
                    f"World assembly failed: {error}",
                    code="world.assembly_failed",
                    user_message="World 装配失败，请检查 world 配置与角色文件。",
                    recoverable=False,
                    details={
                        "diagnostics": [to_builtins(diagnostic) for diagnostic in error.diagnostics]
                    },
                ) from error
            if start:
                await world.start()
                self.state.started = True

            self.state.world = world
            self.state.config_path = config_file
            self._world_persistence_path = config.world.persistence.save_path
            self._start_event_recording(world.event_bus)
            return self._world_state_payload(world)

    async def world_start(self) -> dict[str, Any]:
        """启动已装配的 World（开场；幂等）。"""

        world = await self._ensure_world_started()
        return self._world_state_payload(world)

    async def world_send_message(
        self,
        message: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """用户发言（聚合返回本段自动表演全部回合）。"""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Runtime world message must not be empty")
        await self._ensure_world_started()
        async with (
            self._resource_scope("runtime", "agent_message"),
            self._resource_scope("agent_message", "agent_message"),
            self._lock,
        ):
            world = self._require_world()
            ledger_id = self._world_ledger_id(world)
            fingerprint = self._world_message_fingerprint(world, message)
            replay = self._lookup_operation_replay(
                ledger_id, idempotency_key, request_fingerprint=fingerprint
            )
            if replay is not None:
                return replay
            generation_id = str(uuid4())
            self._begin_message_operation(
                ledger_id,
                idempotency_key,
                request_fingerprint=fingerprint,
                generation_id=generation_id,
            )
            try:
                world_turns = await world.send_message(message)
                turns = [
                    {
                        "actor_id": turn.actor_id,
                        "actor_name": turn.actor_name,
                        "scene_id": turn.scene_id,
                        "content": turn.content,
                    }
                    for turn in world_turns
                ]
                result = self._world_message_result(
                    world, turns, generation_id=generation_id, idempotent_replay=False
                )
                self._succeed_message_operation(ledger_id, idempotency_key, result)
                return result
            except asyncio.CancelledError:
                self._cancel_message_operation(ledger_id, idempotency_key)
                raise
            except Exception as error:
                self._fail_message_operation(ledger_id, idempotency_key, error)
                raise

    async def iter_world_message_stream(
        self,
        message: str,
        agent_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        generation_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield World 流式事件（world.actor.* / world.waiting_user / world.finish）。"""

        principal = current_principal()
        if self._uses_network_tenancy(principal.network):
            if not agent_id:
                raise ValueError("Runtime agent_id is required")
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ValueError("Runtime idempotency_key is required")
            service = self._require_tenant_service(principal.user_id, agent_id)
            async with (
                self._network_operation_scope("world.send_message_stream"),
                service._tenant_operation_lock,
            ):
                # 显式持有并关闭内层流（对齐 agent 流的确定性关闭链）
                inner_stream = cast(
                    AsyncGenerator[dict[str, Any]],
                    service.iter_world_message_stream(
                        message,
                        idempotency_key=idempotency_key,
                        generation_id=generation_id,
                    ),
                )
                try:
                    async for event in inner_stream:
                        yield {
                            "user_id": principal.user_id,
                            "agent_id": agent_id,
                            **event,
                        }
                finally:
                    await inner_stream.aclose()
            return

        if not isinstance(message, str) or not message.strip():
            raise ValueError("Runtime world message must not be empty")
        await self._ensure_world_started()
        async with (
            self._resource_scope("runtime", "agent_stream"),
            self._resource_scope("agent_message", "agent_stream"),
            self._resource_scope("stream", "agent_stream"),
            self._lock,
        ):
            world = self._require_world()
            ledger_id = self._world_ledger_id(world)
            fingerprint = self._world_message_fingerprint(world, message)
            replay = self._lookup_operation_replay(
                ledger_id, idempotency_key, request_fingerprint=fingerprint
            )
            resolved_generation_id = generation_id or str(uuid4())
            if replay is not None:
                yield {
                    "type": "world.finish",
                    **replay,
                    "generation_id": replay.get("generation_id") or resolved_generation_id,
                }
                return
            self._begin_message_operation(
                ledger_id,
                idempotency_key,
                request_fingerprint=fingerprint,
                generation_id=resolved_generation_id,
            )
            turns: list[dict[str, Any]] = []
            try:
                async for event in world.send_message_stream(message):
                    event = {**event, "generation_id": resolved_generation_id}
                    if event.get("type") == "world.actor.completed":
                        turns.append(
                            {
                                "actor_id": event.get("actor_id"),
                                "actor_name": event.get("actor_name"),
                                "scene_id": event.get("scene_id"),
                                "content": event.get("content", ""),
                            }
                        )
                    yield event
            except GeneratorExit:
                # 消费者中途关闭流：账本收敛 cancelled（对齐 agent 流语义）
                self._cancel_message_operation(ledger_id, idempotency_key)
                raise
            except asyncio.CancelledError:
                self._cancel_message_operation(ledger_id, idempotency_key)
                yield {
                    "type": "cancelled",
                    "generation_id": resolved_generation_id,
                    "turns": turns,
                }
                raise
            except Exception as error:
                self._fail_message_operation(ledger_id, idempotency_key, error)
                yield {
                    "type": "error",
                    "generation_id": resolved_generation_id,
                    "turns": turns,
                    "error": runtime_error_to_dict(error),
                }
                raise
            result = self._world_message_result(
                world,
                turns,
                generation_id=resolved_generation_id,
                idempotent_replay=False,
            )
            self._succeed_message_operation(ledger_id, idempotency_key, result)
            yield {"type": "world.finish", **result}

    async def world_send_message_stream(
        self,
        message: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """world.send_message 的流式聚合形态（非 WS 传输下返回完整事件序列）。"""
        events: list[dict[str, Any]] = []
        async for event in self.iter_world_message_stream(
            message,
            idempotency_key=idempotency_key,
        ):
            events.append(event)

        finish_event = events[-1] if events else {}
        return {
            **{key: value for key, value in finish_event.items() if key != "type"},
            "events": events,
        }

    async def world_state(self) -> dict[str, Any]:
        """World 当前状态快照（舞台/roster/等待状态/恢复诊断）。"""

        return self._world_state_payload(self._require_world())

    async def world_roster(self) -> list[dict[str, Any]]:
        """在场角色名单及各自舞台位置。"""

        world = self._require_world()
        snapshot = world.state_snapshot()
        return [
            {
                "actor_id": actor_id,
                "name": name,
                "scene_id": snapshot.stage.get(actor_id),
                "is_current": actor_id == snapshot.current_actor_id,
            }
            for actor_id, name in snapshot.roster.items()
        ]

    async def world_transcript(
        self,
        scene_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """共享剧本（公开层）；默认返回用户当前场景分片。"""

        world = self._require_world()
        limit = self._validate_page_limit(limit, maximum=500)
        snapshot = world.state_snapshot()
        target_scene = scene_id or snapshot.stage.get(USER_OCCUPANT_ID)
        if not target_scene:
            raise ValueError("Runtime world transcript requires scene_id")
        entries = world.transcript_history(target_scene, limit)
        return {
            "world_id": snapshot.world_id,
            "scene_id": target_scene,
            "entries": [to_builtins(entry) for entry in entries],
            "entry_count": len(entries),
        }

    async def world_move(self, scene_id: str) -> dict[str, Any]:
        """把用户移动到指定场景（World 层完成校验与公开过渡事件）。"""

        if not isinstance(scene_id, str) or not scene_id.strip():
            raise ValueError("Runtime world move requires scene_id")
        world = await self._ensure_world_started()
        await world.move_user(scene_id.strip())
        return self._world_state_payload(world)

    async def world_session_create(
        self,
        config_path: str | None = None,
        start: bool = True,
    ) -> dict[str, Any]:
        """强制新建 World 存档（同 world.init 不带 session_id）。"""

        return await self.world_init(config_path=config_path, session_id=None, start=start)

    async def world_session_list(self, world_id: str | None = None) -> dict[str, Any]:
        """列出某 world 的全部存档。"""

        resolved_world_id = self._world_id_or_current(world_id)
        records = await asyncio.to_thread(self._world_persistence().list, resolved_world_id)
        return {
            "world_id": resolved_world_id,
            "sessions": [to_builtins(record) for record in records],
        }

    async def world_session_resume(
        self,
        session_id: str,
        config_path: str | None = None,
        start: bool = True,
    ) -> dict[str, Any]:
        """恢复指定 World 存档（先关闭当前 World，再按存档恢复编排）。"""

        return await self.world_init(config_path=config_path, session_id=session_id, start=start)

    async def world_session_delete(
        self,
        session_id: str,
        world_id: str | None = None,
    ) -> dict[str, Any]:
        """删除指定 World 存档；运行中的活动存档拒绝删除。"""

        resolved_world_id = self._world_id_or_current(world_id)
        world = self.state.world
        if (
            world is not None
            and world.world_id == resolved_world_id
            and world.session_id == session_id
        ):
            raise RpcError(
                "Cannot delete the active World session",
                code="world.session_active",
                user_message="不能删除正在运行的 World 存档，请先 world.shutdown 或切换存档。",
                recoverable=False,
            )
        deleted = await self._world_persistence().delete_async(resolved_world_id, session_id)
        if not deleted:
            raise ValueError(f"World session does not exist: {session_id}")
        return {"deleted": True, "world_id": resolved_world_id, "session_id": session_id}

    async def world_session_export(
        self,
        session_id: str,
        world_id: str | None = None,
    ) -> dict[str, Any]:
        """导出机器可读的独立 World session bundle。"""

        resolved_world_id = self._world_id_or_current(world_id)
        return await asyncio.to_thread(
            self._world_persistence().export, resolved_world_id, session_id
        )

    async def world_shutdown(self) -> dict[str, Any]:
        """关闭 World：保存存档、关停全部 Actor 与 World 总线。"""

        self._require_world()
        async with self._lock:
            await self._shutdown_locked()
        return {"ok": True, "shutdown": True}

    def _require_world(self) -> GensokyoWorld:
        if self.state.world is None:
            raise RpcError(
                "Runtime World is not initialized. Call world.init first.",
                code="world.not_initialized",
                user_message="World 尚未初始化，请先调用 world.init。",
                recoverable=True,
                action_hint="调用 world.init 装配多角色世界后再试。",
            )
        return self.state.world

    async def _ensure_world_started(self) -> GensokyoWorld:
        world = self._require_world()
        if not self.state.started:
            async with self._lock:
                if not self.state.started:
                    await world.start()
                    self.state.started = True
        return world

    def _world_state_payload(self, world: GensokyoWorld) -> dict[str, Any]:
        snapshot = world.state_snapshot()
        return {
            "world_id": snapshot.world_id,
            "session_id": snapshot.session_id,
            "protagonist": snapshot.protagonist,
            "current_actor_id": snapshot.current_actor_id,
            "waiting_for_user": snapshot.waiting_for_user,
            "stage": dict(snapshot.stage),
            "roster": dict(snapshot.roster),
            "transcript_counts": dict(snapshot.transcript_counts),
            "started": self.state.started,
            "resume_diagnostics": [
                to_builtins(diagnostic) for diagnostic in world.resume_diagnostics
            ],
        }

    @staticmethod
    def _world_ledger_id(world: GensokyoWorld) -> str:
        """幂等账本的会话槽位：World 存档 id，未启用持久化时用稳定占位。"""

        return world.session_id or f"world:{world.world_id}:ephemeral"

    @staticmethod
    def _world_message_fingerprint(world: GensokyoWorld, message: str) -> str:
        return RuntimeOperationStore.request_fingerprint(
            {"world_id": world.world_id, "message": message}
        )

    @staticmethod
    def _world_message_result(
        world: GensokyoWorld,
        turns: list[dict[str, Any]],
        *,
        generation_id: str,
        idempotent_replay: bool,
    ) -> dict[str, Any]:
        return {
            "world_id": world.world_id,
            "session_id": world.session_id,
            "turns": turns,
            "waiting_for_user": world.waiting_for_user,
            "generation_id": generation_id,
            "idempotent_replay": idempotent_replay,
        }

    def _world_persistence(self) -> WorldPersistence:
        if self._storage_root is not None:
            return WorldPersistence(self._storage_root / "world")
        root = self._world_persistence_path or (self.state.root_dir / "sessions" / "world")
        return WorldPersistence(root)

    def _world_id_or_current(self, world_id: str | None) -> str:
        if isinstance(world_id, str) and world_id.strip():
            return world_id.strip()
        world = self.state.world
        if world is not None:
            return world.world_id
        raise ValueError("Runtime world_id is required when no World is initialized")

    async def dependency_status(self, providers: list[str] | None = None) -> dict[str, Any]:
        """Return optional Provider dependency status for generic clients."""

        return dependency_status(providers)

    async def install_dependencies(
        self,
        providers: list[str],
        scope: InstallScope = "current_runtime",
        timeout: int = 600,
    ) -> dict[str, Any]:
        """Install whitelisted optional Provider dependencies."""

        async with (
            self._resource_scope("runtime", "dependency_install"),
            self._resource_scope("dependency_install", "dependency_install"),
        ):
            configured_timeout = self._resource_control_config().dependency_install_timeout_seconds
            requested_timeout = configured_timeout if timeout == 600 else timeout
            # 调用方 timeout 只允许比配置上限更短，防止无限期占用工作线程
            effective_timeout = max(1, min(int(requested_timeout), int(configured_timeout)))
            # pip install 同步 subprocess 最长可达数分钟，放工作线程执行，
            # 避免冻结整个事件循环
            return await asyncio.to_thread(
                install_dependencies, providers, scope=scope, timeout=effective_timeout
            )

    async def external_tool_status(self, include_tools: bool = True) -> dict[str, Any]:
        """Return external tool source status without exposing transport details."""

        return self.external_tool_manager.source_status(include_tools=include_tools)

    async def memory_list(
        self,
        topic_id: str | None = None,
        topic_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List current-session semantic memories with topic diagnostics."""

        memory = self._require_semantic_memory()
        return memory.list_memories(
            topic_id=topic_id,
            topic_name=topic_name,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )

    async def memory_search(
        self,
        query: str,
        top_k: int | None = None,
        threshold: float | None = None,
        include_embedding: bool = True,
    ) -> dict[str, Any]:
        """Search current-session semantic memories with explainable diagnostics."""

        if not query or not query.strip():
            raise ValueError("Memory search query is required")
        memory = self._require_semantic_memory()
        items = await memory.search_async(
            query=query,
            top_k=top_k,
            threshold=threshold,
            include_embedding=include_embedding,
        )
        diagnostics = (
            items[0].get("diagnostics", {})
            if items
            else {
                "embedding_requested": include_embedding,
                "embedding_used": False,
                "threshold": threshold,
            }
        )
        return {
            "query": query,
            "items": items,
            "count": len(items),
            "diagnostics": diagnostics,
        }

    async def memory_get(self, memory_id: str) -> dict[str, Any]:
        """Return one semantic memory by id."""

        if not memory_id:
            raise ValueError("Memory id is required")
        memory = self._require_semantic_memory()
        item = memory.get_memory(memory_id)
        if item is None:
            raise ValueError(f"Memory does not exist: {memory_id}")
        return item

    async def memory_update(
        self,
        memory_id: str,
        content: str | None = None,
        importance: float | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update one semantic memory by id."""

        if not memory_id:
            raise ValueError("Memory id is required")
        memory = self._require_semantic_memory()
        item = await memory.update_memory(
            memory_id,
            content=content,
            importance=importance,
            tags=tags,
        )
        if item is None:
            raise ValueError(f"Memory does not exist: {memory_id}")
        return {"updated": True, "memory": item}

    async def memory_delete(self, memory_id: str) -> dict[str, Any]:
        """Delete one semantic memory by id."""

        if not memory_id:
            raise ValueError("Memory id is required")
        memory = self._require_semantic_memory()
        deleted = await memory.delete_memory(memory_id)
        if not deleted:
            raise ValueError(f"Memory does not exist: {memory_id}")
        return {"deleted": True, "memory_id": memory_id}

    async def memory_graph(self) -> dict[str, Any]:
        """Return current-session topic graph for memory visualization."""

        memory = self._require_semantic_memory()
        graph = memory.get_topic_graph()
        return {
            **graph,
            "topic_count": len(graph.get("nodes", [])),
            "edge_count": len(graph.get("edges", [])),
        }

    # ==================== 场景（Scene）====================

    def _require_scene_manager(self) -> Any:
        """Return the enabled scene manager, or raise if scenes are disabled."""
        agent = self._require_agent()
        manager = getattr(agent, "scene_manager", None)
        if manager is None or not getattr(manager, "enabled", False):
            raise RuntimeError("Scene system is not enabled")
        return manager

    @staticmethod
    def _scene_payload(scene: Any) -> dict[str, Any]:
        """Serialize a Scene into a stable, client-facing structure."""
        return {
            "id": scene.id,
            "name": scene.name,
            "description": scene.description,
            "atmosphere": scene.atmosphere,
            "time_of_day": scene.time_of_day,
            "connected_scenes": list(scene.connected_scenes),
            "props": list(scene.props),
            "metadata": dict(scene.metadata),
        }

    async def scene_current(self) -> dict[str, Any] | None:
        """Return the current scene of the active session, or None."""
        manager = self._require_scene_manager()
        scene = await manager.get_current_scene()
        return self._scene_payload(scene) if scene else None

    async def scene_list(self) -> list[dict[str, Any]]:
        """List every scene in the shared scene library."""
        manager = self._require_scene_manager()
        scenes = await manager.list_scenes()
        return [self._scene_payload(scene) for scene in scenes]

    async def scene_get(self, scene_id: str) -> dict[str, Any]:
        """Return a single scene definition by id."""
        if not scene_id:
            raise ValueError("Scene id is required")
        manager = self._require_scene_manager()
        scene = await manager.get_scene(scene_id)
        if scene is None:
            raise ValueError(f"Scene does not exist: {scene_id}")
        return self._scene_payload(scene)

    async def scene_switch(self, scene_id: str) -> dict[str, Any]:
        """Switch the current scene from the frontend/integration side.

        Reuses the same SCENE_SWITCH_REQUESTED path as the model tool, so the
        switch is validated, persisted to the session, and broadcast via
        SCENE_SWITCHED identically.
        """
        if not scene_id:
            raise ValueError("Scene id is required")
        self._require_scene_manager()
        agent = self._require_agent()
        request_event = Event(
            type=SystemEvent.SCENE_SWITCH_REQUESTED,
            source="runtime.scene_switch",
            data={"scene_id": scene_id},
        )
        result = await agent.event_bus.request(request_event, timeout=10.0)
        if not isinstance(result, dict) or not result.get("ok"):
            error = (result or {}).get("error") if isinstance(result, dict) else None
            raise ValueError(error or "Failed to switch scene")
        return {"switched": True, "scene": await self.scene_current()}

    async def scene_graph(self) -> dict[str, Any]:
        """Return the scene connectivity graph for visualization."""
        manager = self._require_scene_manager()
        scenes = await manager.list_scenes()
        valid_ids = {scene.id for scene in scenes}
        nodes = [{"id": scene.id, "name": scene.name} for scene in scenes]
        edges = [
            {"from": scene.id, "to": target}
            for scene in scenes
            for target in scene.connected_scenes
            if target in valid_ids
        ]
        current = await manager.get_current_scene()
        return {
            "nodes": nodes,
            "edges": edges,
            "current_scene_id": current.id if current else None,
            "enforce_connectivity": bool(manager.config.enforce_connectivity),
            "generated_at": utc_now().isoformat(),
        }

    async def create_event_subscription(
        self,
        event_types: list[str] | None = None,
        categories: list[str] | None = None,
        queue_size: int = 100,
        agent_id: str | None = None,
        after_sequence: int = 0,
        replay_limit: int = 500,
    ) -> dict[str, Any]:
        """Create an EventBus-backed Runtime event subscription."""

        principal = current_principal()
        if self._uses_network_tenancy(principal.network):
            if not agent_id:
                raise ValueError("Runtime agent_id is required")
            service = self._require_tenant_service(principal.user_id, agent_id)
            subscription = await service.create_event_subscription(
                event_types,
                categories,
                queue_size,
                after_sequence=after_sequence,
                replay_limit=replay_limit,
            )
            public_id = str(uuid4())
            self._tenant_subscription_owners[public_id] = (
                service,
                subscription["subscription_id"],
            )
            return {
                **subscription,
                "subscription_id": public_id,
                "user_id": principal.user_id,
                "agent_id": agent_id,
            }

        event_bus = self._runtime_event_bus()
        resolved_events = self._resolve_runtime_event_types(event_types, categories)
        if queue_size < 1:
            raise ValueError("Subscription queue_size must be greater than or equal to 1")

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        subscription_ids: list[str] = []
        dropped_count = 0

        async def enqueue_event(event: Event) -> None:
            nonlocal dropped_count
            payload = self._recorded_event_payloads.get(event.id)
            if payload is None:
                payload = self._runtime_event_payload(event)
                if self._tenant_key is not None:
                    payload = {
                        "user_id": self._tenant_key[0],
                        "agent_id": self._tenant_key[1],
                        **payload,
                    }
            if queue.full():
                dropped_count += 1
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                payload = self._runtime_backpressure_payload(
                    dropped_count=dropped_count,
                    dropped_event=payload,
                    queue_size=queue_size,
                )
                if queue.full():
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
            queue.put_nowait(payload)

        if self._event_store is not None:
            replayed = await self._event_store.replay(
                after_sequence=after_sequence,
                event_types={event_type.value for event_type in resolved_events},
                limit=min(replay_limit, queue_size),
            )
            for payload in replayed:
                queue.put_nowait(payload)

        for event_type in resolved_events:
            subscription_ids.append(event_bus.subscribe(event_type, enqueue_event))

        subscription_id = ",".join(subscription_ids)
        self._runtime_event_subscriptions[subscription_id] = subscription_ids
        return {
            "subscription_id": subscription_id,
            "event_types": [event_type.value for event_type in resolved_events],
            "queue": queue,
            "queue_size": queue_size,
            "after_sequence": after_sequence,
            "replayed_count": len(replayed) if self._event_store is not None else 0,
            "earliest_sequence": (
                self._event_store.earliest_sequence if self._event_store is not None else None
            ),
            "latest_sequence": (
                self._event_store.latest_sequence if self._event_store is not None else None
            ),
        }

    def _runtime_event_bus(self) -> EventBus:
        """Runtime 状态事件的订阅总线：World 模式取 World 总线，否则取 Agent 总线。"""
        if self.state.world is not None:
            return self.state.world.event_bus
        return self._require_agent().event_bus

    async def close_event_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Close a previously created Runtime event subscription."""

        if self._tenant_key is None:
            owner = self._tenant_subscription_owners.pop(subscription_id, None)
            if owner is not None:
                service, inner_id = owner
                result = await service.close_event_subscription(inner_id)
                return {**result, "subscription_id": subscription_id}

        event_bus = self._runtime_event_bus()
        subscription_ids = self._runtime_event_subscriptions.pop(subscription_id, None)
        if subscription_ids is None:
            raise ValueError(f"Runtime event subscription does not exist: {subscription_id}")

        removed = 0
        for event_bus_subscription_id in subscription_ids:
            if event_bus.unsubscribe(event_bus_subscription_id):
                removed += 1
        return {"subscription_id": subscription_id, "closed": True, "removed": removed}

    async def shutdown(self) -> dict[str, Any]:
        if self._tenant_key is None:
            self.begin_drain()
        if self._tenant_key is None and self._tenant_services:
            services = list(self._tenant_services.values())
            self._tenant_services.clear()
            await asyncio.gather(*(service.shutdown() for service in services))
        async with self._lock:
            await self._shutdown_locked()
        return {"ok": True}

    def _resource_control_config(self) -> Any:
        agent = self.state.agent
        if agent is not None and hasattr(agent, "config"):
            config = agent.config
            resource_control = getattr(config, "resource_control", None)
            if resource_control is not None:
                return resource_control
        return ConfigLoader().load().resource_control

    def _build_resource_gates(self, resource_control: Any | None = None) -> dict[str, ResourceGate]:
        config = resource_control or ConfigLoader().load().resource_control
        return build_resource_gates(config)

    def _resource_limit_rpc_error(self, error: ResourceLimitError) -> RpcError:
        payload = resource_limit_payload(error)
        return RpcError(
            payload["technical_message"],
            code=payload["code"],
            user_message=payload["user_message"],
            recoverable=payload["recoverable"],
            action_hint=payload["action_hint"],
            details=payload["details"],
        )

    @asynccontextmanager
    async def _resource_scope(self, gate_name: str, action: str) -> AsyncIterator[None]:
        try:
            async with resource_scope(self._resource_gates.get(gate_name), action):
                yield
        except ResourceLimitError as error:
            raise self._resource_limit_rpc_error(error) from error

    def _resource_control_payload(self) -> dict[str, Any]:
        config = self._resource_control_config()
        return {
            "enabled": bool(getattr(config, "enabled", True)),
            "categories": {
                "model": getattr(config, "model_max_concurrent", 2),
                "tool": getattr(config, "tool_max_concurrent", 2),
                "web_search": getattr(config, "web_search_max_concurrent", 1),
                "image_generation": getattr(config, "image_generation_max_concurrent", 1),
                "dependency_install": getattr(config, "dependency_install_max_concurrent", 1),
            },
            "provider_max_concurrent": getattr(config, "provider_max_concurrent", 2),
            "default_timeout_seconds": getattr(config, "default_timeout_seconds", 120.0),
            "dependency_install_timeout_seconds": getattr(
                config,
                "dependency_install_timeout_seconds",
                600,
            ),
            "gates": {name: gate.snapshot() for name, gate in self._resource_gates.items()},
        }

    async def _ensure_started(self) -> Agent:
        agent = self._require_agent()
        if not self.state.started:
            async with self._lock:
                if not self.state.started:
                    await agent.start()
                    self.state.started = True
        return agent

    async def _shutdown_locked(self) -> None:
        for subscription_id in list(self._runtime_event_subscriptions):
            try:
                await self.close_event_subscription(subscription_id)
            except Exception:
                self._runtime_event_subscriptions.pop(subscription_id, None)
        agent = self.state.agent
        if agent is not None:
            await agent.shutdown()
        world = self.state.world
        if world is not None:
            await world.shutdown()
        self._event_store_subscription_ids.clear()
        self._recorded_event_payloads.clear()
        self.state.agent = None
        self.state.world = None
        self.state.started = False

    def _start_event_recording(self, event_bus: EventBus) -> None:
        if self._event_store is None or self._event_store_subscription_ids:
            return
        for event_type in SystemEvent:
            self._event_store_subscription_ids.append(
                event_bus.subscribe(event_type, self._record_runtime_event)
            )

    async def _record_runtime_event(self, event: Event) -> None:
        if self._event_store is None:
            return
        payload = self._runtime_event_payload(event)
        if self._tenant_key is not None:
            payload = {
                "user_id": self._tenant_key[0],
                "agent_id": self._tenant_key[1],
                **payload,
            }
        stored = await self._event_store.append(payload)
        self._recorded_event_payloads[event.id] = stored
        self._recorded_event_payloads.move_to_end(event.id)
        while len(self._recorded_event_payloads) > 1000:
            self._recorded_event_payloads.popitem(last=False)

    def _require_agent(self) -> Agent:
        if self.state.agent is None:
            raise RuntimeError("Runtime is not initialized. Call init first.")
        return self.state.agent

    def _require_semantic_memory(self) -> Any:
        agent = self._require_agent()
        try:
            return agent.semantic_memory
        except Exception as error:
            raise RuntimeError(
                "Semantic memory is not available for the current session"
            ) from error

    def _resolve_optional(self, value: str | None) -> Path | None:
        if not value:
            return None
        return self._resolve_sandboxed_path(value)

    def _resolve_sandboxed_path(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.state.root_dir / path
        resolved = path.resolve()
        root = self.state.root_dir.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise ValueError(f"Path is outside Runtime root directory: {value}")
        return resolved

    def _resolve_character(self, character_path: str | None, character: str | None) -> Path | None:
        if character_path:
            return self._resolve_optional(character_path)
        if not character:
            return None

        base = self.state.root_dir / "characters"
        candidates = [
            base / f"{character}.yaml",
            base / f"{character}.yml",
            base / "zh_cn" / f"{character}.yaml",
            base / "zh_cn" / f"{character}.yml",
            self.state.root_dir / character,
        ]
        for candidate in candidates:
            if candidate.exists():
                return self._resolve_sandboxed_path(str(candidate))
        raise FileNotFoundError(f"Character not found: {character}")

    def _character_payload(self, path: Path | None, name: str | None = None) -> dict[str, Any]:
        return {
            "id": path.stem if path else name,
            "name": name or (path.stem if path else "Unknown"),
            "path": (
                str(path.relative_to(self.state.root_dir))
                if path and path.is_relative_to(self.state.root_dir)
                else (str(path) if path else None)
            ),
        }

    @staticmethod
    def _apply_model_overrides(model: Any, overrides: dict[str, Any] | None) -> None:
        if not overrides:
            return
        validator = ConfigValidator()
        validator.raise_for_errors(validator.validate_model_overrides(overrides))
        RuntimeService._apply_overrides(model, overrides, ConfigValidator.MODEL_OVERRIDE_FIELDS)

    @staticmethod
    def _apply_embedding_overrides(embedding: Any, overrides: dict[str, Any] | None) -> None:
        if not overrides:
            return
        validator = ConfigValidator()
        validator.raise_for_errors(validator.validate_embedding_overrides(overrides))
        RuntimeService._apply_overrides(
            embedding,
            overrides,
            ConfigValidator.EMBEDDING_OVERRIDE_FIELDS,
        )

    @staticmethod
    def _apply_overrides(target: Any, overrides: dict[str, Any], allowed: set[str]) -> None:
        for key, value in overrides.items():
            if key not in allowed or value == "":
                continue
            setattr(target, key, value)

    def _character_validation_payload(
        self,
        diagnostics: list[ConfigDiagnostic],
        *,
        character_path: Path | None,
        source: str,
        preview: dict[str, Any] | None,
    ) -> dict[str, Any]:
        errors = [item for item in diagnostics if item.severity == "error"]
        warnings = [item for item in diagnostics if item.severity == "warning"]
        return {
            "ok": not errors,
            "source": source,
            "character_path": str(character_path) if character_path else None,
            "preview": preview,
            "diagnostics": [item.to_dict() for item in diagnostics],
            "error_count": len(errors),
            "warning_count": len(warnings),
        }

    def _config_validation_payload(
        self,
        diagnostics: list[ConfigDiagnostic],
        *,
        config_path: Path | None,
        source: str,
    ) -> dict[str, Any]:
        errors = [item for item in diagnostics if item.severity == "error"]
        warnings = [item for item in diagnostics if item.severity == "warning"]
        return {
            "ok": not errors,
            "source": source,
            "config_path": str(config_path) if config_path else None,
            "diagnostics": [item.to_dict() for item in diagnostics],
            "error_count": len(errors),
            "warning_count": len(warnings),
        }

    @staticmethod
    def _model_payload(model: ModelInfo) -> dict[str, Any]:
        return {
            "id": model.id,
            "name": model.name,
            "context_window": model.context_window,
            "capabilities": list(model.capabilities),
            "owned_by": model.owned_by,
            "metadata": dict(model.metadata),
        }

    @staticmethod
    def _stream_chunk_payload(chunk: Any, index: int) -> dict[str, Any]:
        chunk_type = getattr(chunk, "type", "text") or "text"
        reasoning_content = getattr(chunk, "reasoning_content", None)
        event_type = "content" if chunk_type == "text" else chunk_type
        if reasoning_content and not getattr(chunk, "content", ""):
            event_type = "reasoning"
        event: dict[str, Any] = {
            "type": event_type,
            "index": index,
            "content": getattr(chunk, "content", "") or "",
        }
        optional_fields = (
            "reasoning_content",
            "is_tool_call",
            "tool_info",
            "status",
            "error",
            "error_code",
            "error_details",
            "usage",
            "finish_reason",
        )
        for field_name in optional_fields:
            value = getattr(chunk, field_name, None)
            if value not in (None, False, "", [], {}):
                event[field_name] = RuntimeService._sanitize_runtime_event_value(value)
        if getattr(chunk, "timing", None) is not None:
            event["timing"] = str(chunk.timing)
        references = getattr(chunk, "web_search_references", None)
        if references:
            event["web_search_references"] = [str(reference) for reference in references]
        diagnostics = getattr(chunk, "web_search_diagnostics", None)
        if diagnostics is not None:
            event["web_search_diagnostics"] = str(diagnostics)
        return event

    @staticmethod
    def _runtime_event_payload(event: Event) -> dict[str, Any]:
        return {
            "type": event.type.value,
            "id": event.id,
            "source": event.source,
            "data": RuntimeService._sanitize_runtime_event_value(event.data),
            "timestamp": event.timestamp.isoformat(),
            "metadata": RuntimeService._sanitize_runtime_event_value(event.metadata),
        }

    @staticmethod
    def _agent_initiative_timer_payload(agent: Any) -> dict[str, Any] | None:
        current = getattr(agent, "current_initiative_timer", None)
        status_getter = getattr(agent, "initiative_hesitation_status", None)
        status = status_getter() if callable(status_getter) else None
        status = status if isinstance(status, dict) else None
        if not callable(current):
            return {"timer": None, "hesitation": status} if status is not None else None
        payload = current()
        if isinstance(payload, dict):
            if status is not None and "hesitation_enabled" not in payload:
                return {**payload, "hesitation_enabled": status.get("enabled")}
            return payload
        return {"timer": None, "hesitation": status} if status is not None else None

    @staticmethod
    def _runtime_backpressure_payload(
        *,
        dropped_count: int,
        dropped_event: dict[str, Any],
        queue_size: int,
    ) -> dict[str, Any]:
        return {
            "type": RUNTIME_EVENT_BACKPRESSURE_DROPPED,
            "id": f"backpressure-{dropped_count}",
            "source": "runtime.service",
            "data": {
                "dropped_count": dropped_count,
                "queue_size": queue_size,
                "dropped_event_type": dropped_event.get("type"),
                "dropped_event_id": dropped_event.get("id"),
            },
            "timestamp": utc_now().isoformat(),
            "metadata": {},
        }

    @staticmethod
    def _sanitize_runtime_event_value(value: Any) -> Any:
        # 脱敏统一走 event_contract.sanitize_event_payload（全项目唯一实现），
        # 这里只先做 JSON 兼容化
        return sanitize_event_payload(RuntimeService._json_compatible(value))

    @staticmethod
    def _json_compatible(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): RuntimeService._json_compatible(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [RuntimeService._json_compatible(item) for item in value]
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return RuntimeService._json_compatible(value.to_dict())
        if hasattr(value, "value"):
            return RuntimeService._json_compatible(value.value)
        return str(value)

    @staticmethod
    def _resolve_runtime_event_types(
        event_types: Iterable[str] | None = None,
        categories: Iterable[str] | None = None,
    ) -> list[SystemEvent]:
        requested = set(event_types or [])
        category_names = set(categories or [])
        if "all" in category_names or "*" in requested:
            return list(SystemEvent)

        category_map = RuntimeService._runtime_event_category_map()
        for category in category_names:
            if category not in category_map:
                raise ValueError(f"Unknown Runtime event category: {category}")
            requested.update(event_type.value for event_type in category_map[category])

        if not requested:
            requested.update(event_type.value for event_type in category_map["runtime_observable"])

        by_value = {event_type.value: event_type for event_type in SystemEvent}
        unknown = sorted(value for value in requested if value not in by_value)
        if unknown:
            raise ValueError(f"Unknown Runtime event types: {', '.join(unknown)}")
        return [by_value[value] for value in sorted(requested)]

    @staticmethod
    def _runtime_event_category_map() -> dict[str, tuple[SystemEvent, ...]]:
        categories = {
            "tool": (
                SystemEvent.TOOL_CALL_SELECTED,
                SystemEvent.TOOL_CALL_STARTED,
                SystemEvent.TOOL_CALL_PROGRESS,
                SystemEvent.TOOL_CALL_COMPLETED,
                SystemEvent.TOOL_CALL_FAILED,
            ),
            "model": (
                SystemEvent.MODEL_CALL_TIMING,
                SystemEvent.MODEL_AUTH,
                SystemEvent.MODEL_REQUEST_STARTED,
                SystemEvent.MODEL_RETRY_SCHEDULED,
                SystemEvent.MODEL_FIRST_TOKEN,
                SystemEvent.MODEL_COMPLETED,
                SystemEvent.MODEL_FAILED,
            ),
            "background": (
                SystemEvent.BACKGROUND_TASK_SUBMITTED,
                SystemEvent.BACKGROUND_TASK_COMPLETED,
                SystemEvent.BACKGROUND_TASK_FAILED,
                SystemEvent.BACKGROUND_WORKER_STARTED,
                SystemEvent.BACKGROUND_WORKER_IDLE,
                SystemEvent.BACKGROUND_WORKER_FAILED,
            ),
            "persistence": (
                SystemEvent.PERSISTENCE_SAVE_STARTED,
                SystemEvent.PERSISTENCE_SAVE_COMPLETED,
                SystemEvent.PERSISTENCE_SAVE_FAILED,
            ),
            "error": (
                SystemEvent.ERROR_OCCURRED,
                SystemEvent.MODEL_ERROR,
                SystemEvent.TOOL_ERROR,
            ),
        }
        categories["initiative_timer"] = (
            SystemEvent.INITIATIVE_TIMER_CREATED,
            SystemEvent.INITIATIVE_TIMER_UPDATED,
            SystemEvent.INITIATIVE_TIMER_CANCELLED,
            SystemEvent.INITIATIVE_TIMER_TRIGGERED,
            SystemEvent.INITIATIVE_TIMER_DISCARDED,
        )
        categories["world"] = (
            SystemEvent.WORLD_STARTED,
            SystemEvent.WORLD_SHUTDOWN,
            SystemEvent.WORLD_ACTOR_TURN_STARTED,
            SystemEvent.WORLD_ACTOR_TURN_CHUNK,
            SystemEvent.WORLD_ACTOR_TURN_COMPLETED,
            SystemEvent.WORLD_DIRECTOR_DECISION,
            SystemEvent.WORLD_SCENE_MOVED,
            SystemEvent.WORLD_WAITING_USER,
        )
        categories["runtime_observable"] = (
            *categories["tool"],
            *categories["model"],
            *categories["background"],
            *categories["persistence"],
            *categories["error"],
            *categories["initiative_timer"],
            *categories["world"],
        )
        return categories

    @staticmethod
    def _normalize_session_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(messages, list):
            raise ValueError("Messages must be a list")

        normalized: list[dict[str, Any]] = []
        allowed_roles = {"system", "user", "assistant", "tool"}
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ValueError(f"Message at index {index} must be an object")
            role = message.get("role")
            content = message.get("content")
            if role not in allowed_roles:
                raise ValueError(f"Message at index {index} has invalid role")
            if not isinstance(content, str | list):
                raise ValueError(
                    f"Message at index {index} content must be text or a content-parts array"
                )
            if isinstance(content, list) and not all(isinstance(part, dict) for part in content):
                raise ValueError(f"Message at index {index} contains an invalid content part")
            normalized.append(dict(message))
        return normalized

    @staticmethod
    def _find_regeneration_user_index(
        messages: list[dict[str, Any]], message_index: int
    ) -> int | None:
        for index in range(message_index, -1, -1):
            if messages[index].get("role") == "user":
                return index
        return None

    @staticmethod
    def _activate_session_for_regeneration(agent: Agent, session_id: str) -> None:
        if hasattr(agent, "resume_session"):
            if not agent.resume_session(session_id):
                raise ValueError(f"Session does not exist: {session_id}")
            return
        if not agent.session_manager.set_current_session(session_id):
            raise ValueError(f"Session does not exist: {session_id}")

    def _session_messages_payload(
        self,
        manager: Any,
        session: SessionContext,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        current = manager.get_current_session()
        is_current = bool(current and current.session_id == session.session_id)
        return {
            "session": self._session_payload(session),
            "session_id": session.session_id,
            "revision": session.revision,
            "is_current": is_current,
            "messages": [self._public_message(message) for message in messages],
            "message_count": len(messages),
        }

    def _lookup_operation_replay(
        self,
        ledger_id: str,
        idempotency_key: str | None,
        *,
        request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """查幂等账本：有记录则按状态 replay 或抛错；无记录/无账本返回 None。

        ``ledger_id`` 对 Agent 会话是 session_id，对 World 是 World 存档槽位。
        """
        if idempotency_key is None:
            return None
        key = self._normalize_idempotency_key(idempotency_key)
        if self._operation_store is None:
            return None
        operation = self._operation_store.get(ledger_id, key)
        if operation is None:
            return None
        stored_fingerprint = operation.get("request_fingerprint")
        if request_fingerprint and stored_fingerprint != request_fingerprint:
            raise RpcError(
                "Idempotency key was already used for a different request",
                code="message.idempotency_conflict",
                user_message="同一幂等键不能用于不同的消息请求。",
                recoverable=False,
                action_hint="请为新的消息请求生成新的 idempotency_key。",
                details={
                    "operation_id": operation.get("operation_id"),
                    "session_id": ledger_id,
                },
            )
        return self._operation_replay(operation)

    def _idempotent_response(
        self,
        agent: Agent,
        session_id: str,
        idempotency_key: str | None,
        *,
        request_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        if idempotency_key is None:
            return None
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise ValueError("Runtime idempotency_key must contain 1 to 128 characters")
        replay = self._lookup_operation_replay(
            session_id, key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return replay
        messages = agent.session_manager.persistence.load_messages(session_id)
        for index, message in enumerate(messages):
            if message.get("role") != "user" or message.get("idempotency_key") != key:
                continue
            assistant = next(
                (
                    candidate
                    for candidate in messages[index + 1 :]
                    if candidate.get("role") == "assistant"
                ),
                None,
            )
            if assistant is None:
                raise RpcError(
                    f"Idempotency key is already in progress: {key}",
                    code="message.idempotency_in_progress",
                    user_message="同一发送请求仍在处理中。",
                    recoverable=True,
                    action_hint="请稍后使用同一 idempotency_key 重试。",
                )
            session = agent.session_manager.get_session(session_id)
            return {
                "role": "assistant",
                "content": assistant.get("content", ""),
                "reasoning_content": assistant.get("reasoning_content"),
                "message_id": assistant.get("message_id"),
                "generation_id": assistant.get("generation_id"),
                "idempotent_replay": True,
                "session": self._session_payload(session),
                "initiative_timer": self._agent_initiative_timer_payload(agent),
            }
        return None

    async def message_status(
        self,
        session_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Return the durable state of one remote message operation."""

        if self._operation_store is None:
            raise RpcError(
                "Persistent message operation storage is unavailable",
                code="message.operation_store_unavailable",
                user_message="当前 Runtime 入口不提供持久化消息状态查询。",
                recoverable=False,
            )
        key = self._normalize_idempotency_key(idempotency_key)
        operation = self._operation_store.get(session_id, key)
        if operation is None:
            raise RpcError(
                "Message operation does not exist",
                code="message.operation_not_found",
                user_message="指定的消息操作不存在。",
                recoverable=True,
                action_hint="请确认 session_id 和 idempotency_key 属于同一次发送。",
                details={"session_id": session_id},
            )
        return self._public_operation(operation)

    @staticmethod
    def _message_request_fingerprint(
        message: str | list[dict[str, Any]],
        system_contexts: list[str] | None,
    ) -> str:
        return RuntimeOperationStore.request_fingerprint(
            {"message": message, "system_contexts": system_contexts or []}
        )

    def _begin_message_operation(
        self,
        session_id: str,
        idempotency_key: str | None,
        *,
        request_fingerprint: str,
        generation_id: str,
    ) -> None:
        if self._operation_store is None or idempotency_key is None:
            return
        key = self._normalize_idempotency_key(idempotency_key)
        operation = self._operation_store.begin(
            session_id=session_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            generation_id=generation_id,
        )
        if operation.get("status") != "pending":
            self._operation_replay(operation)

    def _succeed_message_operation(
        self,
        session_id: str,
        idempotency_key: str | None,
        result: dict[str, Any],
    ) -> None:
        if self._operation_store is None or idempotency_key is None or not session_id:
            return
        self._operation_store.succeed(
            session_id,
            self._normalize_idempotency_key(idempotency_key),
            result,
        )

    def _fail_message_operation(
        self,
        session_id: str,
        idempotency_key: str | None,
        error: Exception,
    ) -> None:
        if self._operation_store is None or idempotency_key is None:
            return
        self._operation_store.fail(
            session_id,
            self._normalize_idempotency_key(idempotency_key),
            runtime_error_to_dict(error),
        )

    def _cancel_message_operation(
        self,
        session_id: str,
        idempotency_key: str | None,
    ) -> None:
        if self._operation_store is None or idempotency_key is None:
            return
        self._operation_store.cancel(
            session_id,
            self._normalize_idempotency_key(idempotency_key),
            {
                "code": "message.operation_cancelled",
                "error_code": "message.operation_cancelled",
                "message": "消息生成已取消。",
                "technical_message": "Message generation was cancelled",
                "user_message": "消息生成已取消。",
                "recoverable": True,
                "action_hint": "请读取会话确认当前状态；如需重新生成，请使用新的 idempotency_key。",
                "details": {},
            },
        )

    @staticmethod
    def _operation_replay(operation: dict[str, Any]) -> dict[str, Any] | None:
        status = operation.get("status")
        if status == "succeeded" and isinstance(operation.get("result"), dict):
            return {**operation["result"], "idempotent_replay": True}
        if status == "pending":
            raise RpcError(
                "Idempotent message operation is still in progress",
                code="message.idempotency_in_progress",
                user_message="同一发送请求仍在处理中。",
                recoverable=True,
                action_hint="请稍后调用 message.status 查询，不要更换 idempotency_key。",
                details={
                    "operation_id": operation.get("operation_id"),
                    "generation_id": operation.get("generation_id"),
                },
            )
        stored_error = operation.get("error")
        error: dict[str, Any] = stored_error if isinstance(stored_error, dict) else {}
        stored_details = error.get("details")
        error_details: dict[str, Any] = stored_details if isinstance(stored_details, dict) else {}
        raise RpcError(
            str(
                error.get("technical_message")
                or "Message operation already reached a terminal failure"
            ),
            code=str(error.get("code") or "message.operation_failed"),
            user_message=str(error.get("user_message") or "消息操作已经失败或取消。"),
            recoverable=bool(error.get("recoverable", True)),
            action_hint=error.get("action_hint"),
            details={
                **error_details,
                "operation_id": operation.get("operation_id"),
                "operation_status": status,
                "generation_id": operation.get("generation_id"),
            },
        )

    @staticmethod
    def _public_operation(operation: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in operation.items() if key != "request_fingerprint"}

    @staticmethod
    def _normalize_idempotency_key(idempotency_key: str) -> str:
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise ValueError("Runtime idempotency_key must contain 1 to 128 characters")
        return key

    def _resolve_message_input(
        self,
        message: str | list[dict[str, Any]],
    ) -> str | list[dict[str, Any]]:
        if isinstance(message, str):
            if not message:
                raise ValueError("Runtime message must not be empty")
            return message
        if not isinstance(message, list) or not message:
            raise ValueError("Runtime message must be text or a non-empty content-parts array")
        if self._media_store is None:
            raise RuntimeError("Runtime media storage is unavailable")
        return self._media_store.resolve_content_parts(message)

    def _resolve_persisted_message(self, message: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(message)
        content = resolved.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "media" for part in content
        ):
            if self._media_store is None:
                raise RuntimeError("Runtime media storage is unavailable")
            resolved["content"] = self._media_store.resolve_content_parts(content)
        return resolved

    @staticmethod
    def _public_message(message: dict[str, Any]) -> dict[str, Any]:
        public = dict(message)
        content = public.get("content")
        if not isinstance(content, list):
            return public
        public_parts: list[Any] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("media_id"), str):
                public_parts.append(
                    {
                        "type": "media",
                        "media_id": part["media_id"],
                        **(
                            {"detail": part["image"]["detail"]}
                            if isinstance(part.get("image"), dict) and part["image"].get("detail")
                            else {}
                        ),
                    }
                )
            else:
                public_parts.append(part)
        public["content"] = public_parts
        return public

    def _finalize_message_operation(
        self,
        agent: Agent,
        session_id: str,
        idempotency_key: str | None,
        *,
        generation_id: str | None = None,
    ) -> dict[str, Any]:
        if not session_id:
            return {}
        key = idempotency_key.strip() if idempotency_key else None
        if key is not None and (not key or len(key) > 128):
            raise ValueError("Runtime idempotency_key must contain 1 to 128 characters")
        manager = agent.session_manager
        messages = manager.get_working_memory(session_id).get_context()
        assistant_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "assistant"
            ),
            None,
        )
        if assistant_index is None:
            return {}
        if generation_id:
            messages[assistant_index]["generation_id"] = generation_id
        user_index = next(
            (
                index
                for index in range(assistant_index - 1, -1, -1)
                if messages[index].get("role") == "user"
            ),
            None,
        )
        if key and user_index is not None:
            messages[user_index]["idempotency_key"] = key
        manager.replace_messages(session_id, messages)
        persisted = manager.persistence.load_messages(session_id)
        return next(
            (message for message in reversed(persisted) if message.get("role") == "assistant"),
            {},
        )

    @staticmethod
    def _session_payload(session: SessionContext | None) -> dict[str, Any]:
        if session is None:
            return {}
        return session.to_dict()

    @staticmethod
    def _validate_page_limit(limit: int, *, maximum: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
            raise ValueError(f"Page limit must be between 1 and {maximum}")
        return limit

    @staticmethod
    def _assert_session_revision(
        manager: Any,
        session_id: str,
        expected_revision: int | None,
    ) -> Any:
        if hasattr(manager, "assert_revision"):
            return manager.assert_revision(session_id, expected_revision)
        session = manager.get_session(session_id)
        if session is None:
            raise ValueError(f"Session does not exist: {session_id}")
        current_revision = int(getattr(session, "revision", 0))
        if expected_revision is not None and expected_revision != current_revision:
            raise RpcError(
                "Session revision conflict",
                code="session.revision_conflict",
                details={
                    "session_id": session_id,
                    "expected_revision": expected_revision,
                    "current_revision": current_revision,
                },
            )
        return session

    @staticmethod
    def _cursor_start(values: list[str], cursor: str | None, *, resource: str) -> int:
        if cursor is None:
            return 0
        try:
            return values.index(cursor) + 1
        except ValueError as error:
            raise RpcError(
                f"Runtime {resource} cursor is invalid or stale",
                code="pagination.invalid_cursor",
                user_message="分页游标无效或对应资源已发生变化。",
                recoverable=True,
                action_hint="请从第一页重新读取。",
                details={"resource": resource},
            ) from error
