"""Agent 运行时组件容器。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from msgspec import Struct, field

from ...runtime.resource_control import ResourceGate
from ...scene.manager import SceneManager
from ...session.manager import SessionManager
from ...tools.build_service import ToolBuildService
from ...tools.executor import ToolExecutor
from ...tools.external_manager import ExternalToolManager
from ...tools.registry import ToolRegistry
from ...tools.tool_context import SINGLE_ACTOR_ID
from ..events import EventBus
from .model_client import ModelClient
from .model_registry import ModelRegistryService

if TYPE_CHECKING:
    from .action_executor import ActionExecutor
    from .action_planner import ActionPlanner
    from .lifecycle import LifecycleManager
    from .message_builder import MessageBuilder
    from .response_handler import ResponseHandler
    from .save_coordinator import SaveCoordinator
    from .think_engine import ThinkEngine


class AgentDependencies(Struct):
    """可选的外部注入依赖，用于让多个 Actor 共享大脑。

    全部字段可空；为 None 时 :class:`AgentComposition` 保持当前自建行为。
    多角色（World）模式下由 World 装配时注入共享 ``model_client`` /
    ``resource_gates``、稳定的 ``actor_id`` / ``world_id`` 与世界级
    ``semantic_memory_root``；事件总线、Session/Memory/Scene manager 仍由每个
    Actor 独立创建。
    """

    model_client: ModelClient | None = None
    resource_gates: dict[str, ResourceGate] | None = None
    actor_id: str | None = None
    world_id: str | None = None
    semantic_memory_root: Path | None = None


class AgentRuntimeContext(Struct):
    """保存 Agent 初始化阶段装配出的核心组件。"""

    event_bus: EventBus
    memory_base_path: Path
    semantic_memory_root: Path | None
    resource_gates: dict[str, ResourceGate]
    model_client: ModelClient
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor
    tool_build_service: ToolBuildService
    external_tool_manager: ExternalToolManager
    model_registry_service: ModelRegistryService
    session_manager: SessionManager
    scene_manager: SceneManager
    # Actor 身份：单角色模式默认取角色名（deps 未注入时的兜底），
    # 多角色模式由 World 注入 roster 稳定 id。
    actor_id: str = SINGLE_ACTOR_ID
    world_id: str | None = None


class AgentLazyComponents(Struct):
    """保存 Agent 启动前后按需创建的行为组件。"""

    message_builder: MessageBuilder | None = None
    save_coordinator: SaveCoordinator | None = None
    response_handler: ResponseHandler | None = None
    lifecycle: LifecycleManager | None = None
    think_engine: ThinkEngine | None = None
    action_planner: ActionPlanner | None = None
    action_executor: ActionExecutor | None = None


class AgentBootstrapState(Struct):
    """Agent 初始化阶段装配出的完整状态。"""

    runtime_context: AgentRuntimeContext
    lazy_components: AgentLazyComponents = field(default_factory=AgentLazyComponents)
