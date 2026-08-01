"""Agent 组件装配。"""

from __future__ import annotations

from ...runtime.resource_control import build_resource_gates
from ...scene.manager import SceneManager
from ...session.manager import SessionManager
from ...tools.build_service import ToolBuildService
from ...tools.executor import ToolExecutor
from ...tools.external_manager import ExternalToolManager
from ...tools.registry import ToolRegistry
from ...tools.web_search.service import WebSearchService
from ..config import AppConfig
from ..events import EventBus
from .model_client import ModelClient
from .model_registry import ModelRegistryService
from .runtime_context import AgentBootstrapState, AgentDependencies, AgentRuntimeContext


class AgentComposition:
    """负责创建 Agent 运行所需的基础组件。"""

    def __init__(
        self,
        config: AppConfig,
        character_name: str,
        deps: AgentDependencies | None = None,
    ) -> None:
        self.config = config
        self.character_name = character_name
        self.deps = deps or AgentDependencies()

    def build(self) -> AgentRuntimeContext:
        """装配核心运行时组件。

        事件总线、Session/Memory/Scene manager 始终每个 Actor 独立创建，避免多
        Actor 串台；``model_client`` / ``resource_gates`` 可由 ``deps`` 注入共享，
        用于多角色模式下共用同一个「大脑」与限流闸门。
        """
        event_bus = EventBus(enable_trace=self.config.event_trace_enabled)
        memory_base_path = self.config.session.save_path
        # resource_gates / model_client 优先复用注入的共享实例。
        resource_gates = self.deps.resource_gates or build_resource_gates(
            self.config.resource_control
        )
        actor_id = self.deps.actor_id or self.character_name
        world_id = self.deps.world_id
        model_client = self.deps.model_client or ModelClient(
            self.config.model,
            event_bus=event_bus,
            embedding_config=self.config.embedding,
            resource_gates=resource_gates,
        )
        tool_registry = ToolRegistry()
        external_tool_manager = ExternalToolManager()
        tool_executor = ToolExecutor(
            tool_registry,
            event_bus=event_bus,
            external_tool_manager=external_tool_manager,
            resource_gates=resource_gates,
            actor_id=actor_id,
            world_id=world_id,
            # 每个 Actor 持有自己的搜索服务，经工具上下文按调用注入，
            # 不再依赖模块级全局配置（多 Actor 不互相覆盖）。
            web_search_service=WebSearchService(self.config.tool.web_search),
        )
        tool_build_service = ToolBuildService(tool_registry)
        model_registry_service = ModelRegistryService()
        session_manager = SessionManager(
            self.config.session,
            self.character_name,
            working_max_turns=self.config.memory.working_max_turns,
        )
        scene_manager = SceneManager(self.config.scene)

        return AgentRuntimeContext(
            event_bus=event_bus,
            memory_base_path=memory_base_path,
            semantic_memory_root=self.deps.semantic_memory_root,
            resource_gates=resource_gates,
            model_client=model_client,
            tool_registry=tool_registry,
            tool_executor=tool_executor,
            tool_build_service=tool_build_service,
            external_tool_manager=external_tool_manager,
            model_registry_service=model_registry_service,
            session_manager=session_manager,
            scene_manager=scene_manager,
            actor_id=actor_id,
            world_id=world_id,
        )

    def bootstrap(self) -> AgentBootstrapState:
        """装配 Agent 初始化阶段所需的完整状态。"""
        return AgentBootstrapState(runtime_context=self.build())
