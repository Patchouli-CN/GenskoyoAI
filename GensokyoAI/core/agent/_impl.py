"""Agent 主类 - 事件驱动版"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from ...background import BackgroundManager, PersistenceWorker
from ...memory.semantic import SemanticMemoryManager
from ...memory.working import WorkingMemoryManager
from ...session.context import SessionContext
from ...tools.build_service import ToolBuildContext, ToolBuildResult
from ...tools.tool_builtin.web_search import configure_web_search_tool
from ...utils.content_security import detect_prompt_injection
from ...utils.helpers import extract_speaker_tags, safe_get
from ...utils.logger import logger
from ...utils.path_security import sanitize_path_id
from ..config import AppConfig, ConfigLoader
from ..event_listeners import (
    CoreListeners,
    ErrorListeners,
    PersistenceListeners,
    SceneServiceListeners,
)
from ..events import Event, SystemEvent
from ..exceptions import AgentError
from .action_executor import ActionExecutor
from .action_planner import ActionPlanner
from .composition import AgentComposition
from .emotion import Emotion
from .initiative_coordinator import InitiativeCoordinator
from .lifecycle import LifecycleManager
from .message_builder import MessageBuilder
from .prompts import build_half_completion_context, build_roleplay_system_prompt
from .response_handler import STREAM_INTERRUPT_MARKER, ResponseHandler, strip_interrupt_marker
from .runtime_context import AgentDependencies, AgentLazyComponents
from .save_coordinator import SaveCoordinator
from .think_engine import ThinkEngine
from .types import HalfCompletionMessage, ProviderCapability, StreamChunk, UnifiedMessage

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class Agent:
    """AI 角色扮演 Agent - 事件驱动版"""

    def __init__(
        self,
        config: AppConfig | None = None,
        config_file: Path | None = None,
        character_file: Path | None = None,
        dependencies: AgentDependencies | None = None,
        setup_signal_handlers: bool = True,
        manage_initiative_timer: bool = True,
        label: str | None = None,
    ) -> None:
        # 可选依赖注入：多角色（World）模式下共享 ModelClient / resource_gates
        # 与稳定 actor_id / world_id；单角色模式为 None，保持自建行为。
        self._dependencies = dependencies
        # 日志租户标签（Runtime 多租户传 agent_id）：区分各租户刷屏的关闭日志
        self._log_label = label or ""
        # 是否注册进程级信号处理器。多 Actor 共存时同一事件循环每信号只保留
        # 一个处理器（last-wins）且其 shutdown 会直接 sys.exit，其余 Actor 的
        # 最终保存不会执行——World 装配 Actor 时应传 False，由 World 统一接管。
        self._setup_signal_handlers = setup_signal_handlers
        # 是否由本 Agent 管理主动定时器。主动定时器归属于「对话主循环」：
        # 单角色模式由 Agent 充当主循环（True）；World 模式的 Actor 只是演员，
        # 统一由 World 主循环持有定时器（False），避免多个角色各自抢话烧 token。
        # （阶段 7 的 DialogueLoop 抽象会补完 World 侧的计划与触发。）
        self._manage_initiative_timer = manage_initiative_timer
        # 响应中断时未说完的半截回复（中间状态）：下轮生成前注入提示词让角色
        # 接着说完，正常完成后清除；错误标记不进模型上下文。
        self._half_completion: HalfCompletionMessage | None = None
        self._init_config(config, config_file, character_file)
        self._init_infrastructure()
        self._init_core_components()
        self._init_memory_system()
        self._init_tool_system()
        self._init_session_system()
        self._init_message_components()
        self._init_event_listeners()
        self._inject_dependencies()
        self._init_lifecycle()
        self._init_think_engine()
        self._init_action_components()
        self._publish_started_event()

        logger.info(f"Agent 初始化完成，角色: {self.config.character.name}{self._tenant_suffix}")  # type: ignore

    # ==================== 初始化子方法 ====================

    def _init_config(self, config, config_file, character_file) -> None:
        loader = ConfigLoader()
        self.config_file = config_file
        self.config = config or loader.load(config_file)

        if character_file:
            self.config.character = loader.load_character(character_file)
        elif self.config.character_file:
            self.config.character = loader.load_character(self.config.character_file)

        if not self.config.character:
            raise AgentError("No character loaded")

        self.system_prompt = self._build_system_prompt()

    def _init_infrastructure(self) -> None:
        self._request_semaphore = asyncio.Semaphore(1)
        self._working_memory: WorkingMemoryManager | None = None
        self._generate_response_subscription_id: str | None = None
        self._stream_first_chunk_timeout = 30.0
        self._stream_idle_timeout = 30.0
        self._stream_total_timeout = 120.0

    def _init_core_components(self) -> None:
        self.character_name = safe_get(self.config, "character.name", "default")

    def _init_memory_system(self) -> None:
        bootstrap = AgentComposition(
            self.config, self.character_name, self._dependencies
        ).bootstrap()
        self.runtime_context = bootstrap.runtime_context
        self._lazy_components: AgentLazyComponents = bootstrap.lazy_components
        context = self.runtime_context
        self.event_bus = context.event_bus
        self._memory_base_path = context.memory_base_path
        self._semantic_memory_root = context.semantic_memory_root
        self._model_client = context.model_client
        self._semantic_memory: SemanticMemoryManager | None = None
        # 对外暴露 Actor 身份，供 World 编排与前端识别；单角色模式为默认值。
        self.actor_id = context.actor_id
        self.world_id = context.world_id

    def _init_tool_system(self) -> None:
        self.tool_registry = self.runtime_context.tool_registry
        self.tool_executor = self.runtime_context.tool_executor
        self.tool_build_service = self.runtime_context.tool_build_service
        self.external_tool_manager = self.runtime_context.external_tool_manager
        self.model_registry_service = self.runtime_context.model_registry_service

    def _init_session_system(self) -> None:
        self.session_manager = self.runtime_context.session_manager
        self.scene_manager = self.runtime_context.scene_manager
        # 把角色 begin_scene 指定的初始场景告知 SceneManager，用于会话起始场景解析
        begin_scene = getattr(self.config.character, "begin_scene", None)
        if begin_scene is not None:
            self.scene_manager.set_character_begin_scene(begin_scene.scene)

    def _init_message_components(self) -> None:
        self._message_builder = self._lazy_components.message_builder
        self._save_coordinator = self._lazy_components.save_coordinator
        self._response_handler = self._lazy_components.response_handler
        self._background_manager: BackgroundManager | None = None
        self._initiative_coordinator = InitiativeCoordinator(self)

    def _init_event_listeners(self) -> None:
        self.core_listeners = CoreListeners(self, self.event_bus)
        self.scene_service_listeners = SceneServiceListeners(self, self.event_bus)
        self.error_listeners = ErrorListeners(self.event_bus)
        self.persistence_listeners = PersistenceListeners(self, self.event_bus)
        logger.debug("所有事件监听器已注册")

    def _inject_dependencies(self) -> None:
        # 事件总线不再全局注入：ToolExecutor 在每次调用工具时按调用注入
        # （见 tools/tool_context.py），使多个 Agent 实例互不覆盖。
        configure_web_search_tool(self.config.tool)

    def _init_lifecycle(self) -> None:
        self.lifecycle = self._lazy_components.lifecycle or LifecycleManager(
            on_shutdown=self._on_shutdown
        )
        self._lazy_components.lifecycle = self.lifecycle
        if self._setup_signal_handlers:
            self.lifecycle.setup_signal_handlers()

    def _init_think_engine(self) -> None:
        self._think_engine = self._lazy_components.think_engine

    def _init_action_components(self) -> None:
        self._action_planner = self._lazy_components.action_planner
        self._action_executor = self._lazy_components.action_executor

    def _publish_started_event(self) -> None:
        self.event_bus.publish(
            Event(
                type=SystemEvent.AGENT_STARTED,
                source="agent",
                data={"character": self.config.character.name},  # type: ignore
            )
        )

    def _build_system_prompt(self) -> str:
        if not self.config.character:
            raise AgentError("No Character be roleplayed.")
        return build_roleplay_system_prompt(
            self.config.character.name, self.config.character.system_prompt
        )

    # ==================== 懒加载属性 ====================

    @property
    def semantic_memory(self) -> SemanticMemoryManager:
        if self._semantic_memory is None:
            current_session = self.session_manager.get_current_session()
            if not current_session:
                raise AgentError("No active session for semantic memory")

            memory_path = self._semantic_memory_root or (
                # 角色名直接来自角色卡 YAML（可分发内容），必须净化：
                # 防 ../../ 路径遍历，并与会话目录的净化规则保持一致。
                self._memory_base_path
                / sanitize_path_id(self.character_name)
                / "memory"
                / current_session.session_id
            )
            memory_path.mkdir(parents=True, exist_ok=True)

            self._semantic_memory = SemanticMemoryManager(
                self.config.memory, self.character_name, memory_path, self._model_client
            )
            logger.debug(f"语义记忆已初始化: {memory_path}")
        return self._semantic_memory

    @property
    def working_memory(self) -> WorkingMemoryManager:
        current_session = self.session_manager.get_current_session()
        if not current_session:
            raise AgentError("No active session")
        if not self._working_memory:
            self._working_memory = self.session_manager.get_working_memory(
                current_session.session_id
            )
        return self._working_memory

    @property
    def message_builder(self) -> MessageBuilder:
        if self._message_builder is None:
            self._message_builder = MessageBuilder(
                system_prompt=self.system_prompt,
                working_memory=self.working_memory,
                semantic_memory=self.semantic_memory,
                tool_registry=self.tool_registry,
                tool_enabled=self.config.tool.enabled,
                character_name=self.character_name,
                web_search_config=self.config.tool.web_search,
                model_config=self.config.model,
                # _build_tools() 是异步方法，不能在同步属性初始化中直接调用；
                # 每轮响应生成前会在 _on_generate_response() 中 await 后更新结果。
                tool_build_result=None,
                metadata=self.config.character.metadata if self.config.character else {},
                example_dialogue=(
                    self.config.character.example_dialogue if self.config.character else None
                ),
            )
            self._lazy_components.message_builder = self._message_builder
        return self._message_builder

    @property
    def _tenant_suffix(self) -> str:
        """日志租户后缀：Runtime 多租户下区分各租户刷屏的同款日志。"""
        return f" (租户: {self._log_label})" if self._log_label else ""

    @property
    def save_coordinator(self) -> SaveCoordinator:
        if self._save_coordinator is None:
            self._save_coordinator = SaveCoordinator(
                session_manager=self.session_manager,
                session_config=self.config.session,
                label=self._log_label or None,
            )
            if self._background_manager:
                self._save_coordinator.set_background_manager(self._background_manager)
            self._lazy_components.save_coordinator = self._save_coordinator
        return self._save_coordinator

    @property
    def response_handler(self) -> ResponseHandler:
        if self._response_handler is None:
            self._response_handler = ResponseHandler(
                config=self.config,
                working_memory=self.working_memory,
                tool_executor=self.tool_executor,
                model_client=self._model_client,
                message_builder=self.message_builder,
            )
            self._lazy_components.response_handler = self._response_handler
        return self._response_handler

    @property
    def is_shutting_down(self) -> bool:
        return self.lifecycle.is_shutting_down

    # ==================== 核心 API ====================

    async def send(
        self, user_input: str, system_contexts: list[str] | None = None
    ) -> UnifiedMessage | None:
        """发送消息（非流式）- 完全事件驱动。"""
        return await self._send_impl(
            user_input,
            system_contexts,
            timeout_log="等待响应超时",
            cancel_reason="send timeout",
        )

    async def send_multimodal(
        self,
        content_parts: list[dict[str, Any]],
        system_contexts: list[str] | None = None,
    ) -> UnifiedMessage | None:
        """发送多模态消息（非流式）。"""
        return await self._send_impl(
            content_parts,
            system_contexts,
            timeout_log="多模态响应超时",
            cancel_reason="send multimodal timeout",
        )

    async def _send_impl(
        self,
        user_input: str | list[dict[str, Any]],
        system_contexts: list[str] | None = None,
        *,
        timeout_log: str,
        cancel_reason: str,
        world_turn: bool = False,
        record_in_working_memory: bool = True,
    ) -> UnifiedMessage | None:
        """非流式发送共享实现；`user_input` 为文本或多模态 content parts。"""
        if self.is_shutting_down:
            return None

        async with self._request_semaphore:
            if self.is_shutting_down:
                return None
            if world_turn:
                # World 回合的触发来自对话主循环而非用户，不重置连续主动计数
                await self.discard_initiative_timer(reason="world_turn_received", source="world")
            else:
                await self.discard_initiative_timer(reason="user_message_received", source="user")
            # 铸造请求绑定 id：响应槽位（future/流式队列）凭它识别超时后的
            # 孤儿生成，旧生成的 feed/complete/记忆写入全部作废。
            request_id = uuid4().hex
            response_future = self._action_executor.prepare_response(request_id)  # type: ignore
            self._publish_message_received(
                user_input,
                system_contexts,
                world_turn=world_turn,
                record_in_working_memory=record_in_working_memory,
                request_id=request_id,
            )

            try:
                full_response = await asyncio.wait_for(response_future, timeout=60.0)
                if full_response:
                    return UnifiedMessage(
                        role="assistant",
                        content=full_response,
                        reasoning_content=self.response_handler.last_assistant_reasoning,
                    )
            except TimeoutError:
                logger.warning(timeout_log)
                self._action_executor.cancel_response(cancel_reason)  # type: ignore
                return UnifiedMessage(role="assistant", content="「唔…我有点走神了…」")

            return None

    async def send_stream(
        self, user_input: str, system_contexts: list[str] | None = None
    ) -> AsyncIterator[StreamChunk]:
        """发送消息（流式）- 完全事件驱动。"""
        async for chunk in self._send_stream_impl(
            user_input, system_contexts, timeout_log="流式响应超时"
        ):
            yield chunk

    async def send_multimodal_stream(
        self,
        content_parts: list[dict[str, Any]],
        system_contexts: list[str] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """发送多模态消息（流式）。"""
        async for chunk in self._send_stream_impl(
            content_parts, system_contexts, timeout_log="多模态流式响应超时"
        ):
            yield chunk

    async def _send_stream_impl(
        self,
        user_input: str | list[dict[str, Any]],
        system_contexts: list[str] | None = None,
        *,
        timeout_log: str,
        world_turn: bool = False,
        record_in_working_memory: bool = True,
    ) -> AsyncIterator[StreamChunk]:
        """流式发送共享实现；`user_input` 为文本或多模态 content parts。"""
        if self.is_shutting_down:
            return

        async with self._request_semaphore:
            if self.is_shutting_down:
                return
            if world_turn:
                # World 回合的触发来自对话主循环而非用户，不重置连续主动计数
                await self.discard_initiative_timer(reason="world_turn_received", source="world")
            else:
                await self.discard_initiative_timer(reason="user_message_received", source="user")
            request_id = uuid4().hex
            response_future = self._action_executor.prepare_response(request_id)  # type: ignore
            self._publish_message_received(
                user_input,
                system_contexts,
                world_turn=world_turn,
                record_in_working_memory=record_in_working_memory,
                request_id=request_id,
            )

            full_response = ""
            loop = asyncio.get_running_loop()
            started_at = loop.time()
            last_chunk_at = started_at
            saw_chunk = False
            try:
                while True:
                    timeout = self._next_stream_wait_timeout(
                        started_at=started_at,
                        last_chunk_at=last_chunk_at,
                        saw_chunk=saw_chunk,
                    )
                    if timeout <= 0:
                        raise TimeoutError("stream response timeout")

                    # 同时等待 chunk 和 response_future，避免 response_future 完成后
                    # 还要硬等 get_chunk 的 0.1s 超时
                    get_chunk_task = asyncio.create_task(
                        self._action_executor.get_chunk()  # type: ignore
                    )
                    try:
                        done, pending = await asyncio.wait(
                            [get_chunk_task, response_future],
                            return_when=asyncio.FIRST_COMPLETED,
                            timeout=min(0.1, timeout),
                        )
                    except Exception:
                        get_chunk_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await get_chunk_task
                        raise

                    if get_chunk_task in pending:
                        get_chunk_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await get_chunk_task

                    if response_future in done:
                        # response_future 完成时，已完成的 get_chunk_task 与队列里
                        # 可能还积着最后几个 chunk（生产快于 0.1s 轮询），
                        # 先全部排空再退出，避免丢失流尾。
                        if get_chunk_task in done and (tail_chunk := get_chunk_task.result()):
                            full_response += tail_chunk.content
                            yield tail_chunk
                        while (
                            tail_chunk := self._action_executor.get_chunk_nowait()  # type: ignore
                        ) is not None:
                            full_response += tail_chunk.content
                            yield tail_chunk
                        break

                    if get_chunk_task in done:
                        chunk = get_chunk_task.result()
                        if chunk:
                            saw_chunk = True
                            last_chunk_at = loop.time()
                            full_response += chunk.content
                            yield chunk
                        continue

                    # 超时：两个都没完成
                    if self._stream_timed_out(started_at, last_chunk_at, saw_chunk):
                        raise TimeoutError("stream response timeout")
                    continue
            except asyncio.CancelledError, GeneratorExit:
                self._action_executor.cancel_response("stream cancelled")  # type: ignore
                raise
            except TimeoutError as error:
                logger.warning(f"{timeout_log}: {error}")
                self._action_executor.cancel_response(str(error))  # type: ignore
                yield StreamChunk(
                    type="error",
                    content="\n[响应超时]\n",
                    error=str(error),
                    error_code="agent.stream.timeout",
                )
            finally:
                self._action_executor.complete_response(full_response, request_id=request_id)  # type: ignore

    # ==================== World 回合入口 ====================

    async def send_world_turn(
        self,
        trigger_text: str,
        system_contexts: list[str] | None = None,
        *,
        record_trigger: bool = False,
    ) -> UnifiedMessage | None:
        """World 回合（非流式）：由 World 对话主循环驱动一名 Actor 开口。

        `trigger_text` 是舞台触发（共享剧本/导演指令），默认不写入该 Actor 的
        私有工作记忆——它属于舞台而非角色私历；Actor 自己生成的回复仍会正常
        写入，保持角色自身延续性。`system_contexts` 承载本轮舞台信息（场景、
        在场角色、共享剧本、当前演员身份），并在工具 continuation 中保留。
        """
        return await self._send_impl(
            trigger_text,
            system_contexts,
            timeout_log="等待 world-turn 响应超时",
            cancel_reason="send world turn timeout",
            world_turn=True,
            record_in_working_memory=record_trigger,
        )

    async def send_world_turn_stream(
        self,
        trigger_text: str,
        system_contexts: list[str] | None = None,
        *,
        record_trigger: bool = False,
    ) -> AsyncIterator[StreamChunk]:
        """World 回合（流式）：语义同 `send_world_turn`。"""
        async for chunk in self._send_stream_impl(
            trigger_text,
            system_contexts,
            timeout_log="world-turn 流式响应超时",
            world_turn=True,
            record_in_working_memory=record_trigger,
        ):
            yield chunk

    def _publish_message_received(
        self,
        user_input: str | list[dict[str, Any]],
        system_contexts: list[str] | None = None,
        *,
        world_turn: bool = False,
        record_in_working_memory: bool = True,
        request_id: str | None = None,
    ) -> None:
        text_input = self._extract_text_from_content(user_input)
        report = detect_prompt_injection(text_input)
        contexts = list(system_contexts or [])
        # 情绪语气注入：让当前心情（含行为倾向）自然渗入本轮回复（不写入会话）
        if self._think_engine is not None and (
            emotion_context := self._think_engine.emotion_tone_context()
        ):
            contexts.append(emotion_context)
        data: dict[str, Any] = {
            "content": user_input,
            "system_contexts": contexts,
        }
        if request_id is not None:
            data["request_id"] = request_id
        if world_turn:
            data["world_turn"] = True
            data["actor_id"] = self.actor_id
        if not record_in_working_memory:
            data["record_in_working_memory"] = False
        if report.suspected:
            data["prompt_injection_suspected"] = True
            data["prompt_injection_report"] = report.to_dict()
            logger.warning(
                f"检测到疑似 prompt injection: {report.matched_patterns}, "
                f"风险分数: {report.risk_score}"
            )
            self.event_bus.publish(
                Event(
                    type=SystemEvent.SECURITY_PROMPT_INJECTION_DETECTED,
                    source="agent",
                    data={
                        "risk_score": report.risk_score,
                        "matched_patterns": report.matched_patterns,
                        "category": report.category,
                        "preview": text_input[:200],
                    },
                )
            )

        self.event_bus.publish(
            Event(
                type=SystemEvent.MESSAGE_RECEIVED,
                source="agent",
                data=data,
            )
        )

    @staticmethod
    def _extract_text_from_content(content: Any) -> str:
        """从字符串或多模态 content parts 中提取文本。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return " ".join(texts)
        return ""

    def _next_stream_wait_timeout(
        self,
        *,
        started_at: float,
        last_chunk_at: float,
        saw_chunk: bool,
    ) -> float:
        now = asyncio.get_running_loop().time()
        total_remaining = self._stream_total_timeout - (now - started_at)
        activity_deadline = (
            self._stream_idle_timeout if saw_chunk else self._stream_first_chunk_timeout
        )
        activity_remaining = activity_deadline - (now - last_chunk_at)
        return min(total_remaining, activity_remaining)

    def _stream_timed_out(self, started_at: float, last_chunk_at: float, saw_chunk: bool) -> bool:
        now = asyncio.get_running_loop().time()
        if now - started_at >= self._stream_total_timeout:
            return True
        activity_deadline = (
            self._stream_idle_timeout if saw_chunk else self._stream_first_chunk_timeout
        )
        return now - last_chunk_at >= activity_deadline

    # ==================== 会话管理 ====================

    def _reset_session_scoped_state(self) -> None:
        """会话切换后失效所有捕获了旧会话记忆实例的缓存状态。

        MessageBuilder/ResponseHandler 走懒加载重建（下次访问时用新会话的
        记忆实例重建）；ActionPlanner/ThinkEngine 持有事件订阅与后台任务，
        不能简单换新，就地更新其记忆引用。
        """
        self._working_memory = None
        self._semantic_memory = None
        self._message_builder = None
        self._response_handler = None
        # 半截回复是会话级中间状态，不随会话切换带过去
        self._half_completion = None
        self._lazy_components.message_builder = None
        self._lazy_components.response_handler = None
        if self._action_planner is not None:
            self._action_planner.update_memory_context(self.working_memory, self.semantic_memory)
        if self._think_engine is not None:
            self._think_engine.update_semantic_memory(self.semantic_memory)
            # 情绪是会话级状态：切会话回到角色卡基线，不把旧会话的愤怒/羞耻带入新对话
            self._think_engine.emotion_state.reset()

    async def create_session(self) -> SessionContext:
        session = await self.session_manager.create_session()
        self._reset_session_scoped_state()
        self.scene_manager.reset_for_session(None)
        self.event_bus.publish(
            Event(type=SystemEvent.SESSION_CREATED, source="agent", data={"session": session})
        )
        return session

    def resume_session(self, session_id: str) -> bool:
        current = self.session_manager.get_current_session()
        if current is not None and current.session_id == session_id:
            # 已是当前会话：幂等返回。Runtime 每条消息都会 activate 当前会话，
            # 若照常走重置会连带清掉蒸馏轮次计数与半截回复状态
            return True
        if self.session_manager.set_current_session(session_id):
            self._reset_session_scoped_state()
            session = self.session_manager.get_current_session()
            self.event_bus.publish(
                Event(type=SystemEvent.SESSION_RESUMED, source="agent", data={"session": session})
            )
            return True
        return False

    async def async_save(self) -> None:
        await self.save_coordinator.save_async(self.working_memory, force=True)

    def rollback(self, num: int = 1, mode: Literal["turns", "messages"] = "turns") -> None:
        wm = self.working_memory
        if mode == "turns":
            wm.rollback_turns(num)
        else:
            wm.rollback_messages(num)

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        await self.event_bus.start()
        await self._ensure_background_manager()

        # 启动思考引擎
        if self._think_engine is None and self.semantic_memory is not None:
            self._think_engine = ThinkEngine(
                semantic_memory=self.semantic_memory,
                model_client=self._model_client,
                event_bus=self.event_bus,
                character_name=self.character_name,
                config=self.config.think_engine,
                initiative_timer_config=self.config.initiative_timer,
                debug_silent_output=self.config.debug_silent_output,
                motivation_weights=(
                    self.config.character.motivation_weights
                    if self.config.character is not None
                    else None
                ),
                emotion_baseline=(
                    Emotion(**self.config.character.emotion_baseline)
                    if self.config.character is not None and self.config.character.emotion_baseline
                    else None
                ),
                log_label=self._log_label or None,
            )
            self._lazy_components.think_engine = self._think_engine
        if self._think_engine:
            await self._think_engine.start()

        # 初始化决策组件
        if self._action_planner is None:
            self._action_planner = ActionPlanner(
                character_name=self.character_name,
                model_client=self._model_client,
                working_memory=self.working_memory,
                semantic_memory=self.semantic_memory,
                event_bus=self.event_bus,
                think_engine=self._think_engine,
                debug_silent_output=self.config.debug_silent_output,
            )
            self._lazy_components.action_planner = self._action_planner
        if self._action_executor is None:
            self._action_executor = ActionExecutor(self, self.event_bus)
            self._lazy_components.action_executor = self._action_executor

        if self._generate_response_subscription_id is None:
            self._generate_response_subscription_id = self.event_bus.subscribe(
                SystemEvent.GENERATE_RESPONSE,
                self._on_generate_response,
            )

        # 加载场景库并同步当前会话的场景（覆盖 create/resume/set_current_session 各路径）
        if self.scene_manager.enabled:
            await self.scene_manager.load_library()
            await self._sync_scene_for_current_session()

        logger.info("Agent 已启动")

    async def _sync_scene_for_current_session(self) -> None:
        """根据当前会话 metadata 恢复当前场景，必要时落地默认场景。"""
        if not self.scene_manager.enabled:
            return
        session = self.session_manager.get_current_session()
        session_scene_id = None
        if session is not None:
            session_scene_id = session.metadata.get("current_scene_id")
        resolved = await self.scene_manager.resolve_initial_scene(session_scene_id)
        self.scene_manager.reset_for_session(resolved)
        if session is not None and resolved and resolved != session_scene_id:
            session.metadata["current_scene_id"] = resolved
            await self.session_manager.persistence.save_session_async(session)

    async def _build_tools(self) -> ToolBuildResult:
        """通过 ModelRegistryService + ToolBuildService 构建本轮工具 schema 与 instructions。"""
        model_info = await self.model_registry_service.get_model_info(self.config.model)
        capabilities = set(model_info.capabilities)
        if not capabilities:
            capabilities = {ProviderCapability.CHAT, ProviderCapability.STREAM}
        external_tool_manager = getattr(self, "external_tool_manager", None)
        external_tools = []
        external_tool_policy = None
        if external_tool_manager is not None:
            external_tools = await external_tool_manager.list_tools(refresh=True)
            external_tool_policy = external_tool_manager.policy
        result = self.tool_build_service.build(
            ToolBuildContext(
                tool_config=self.config.tool,
                model_config=self.config.model,
                model_capabilities=capabilities,
                character_name=self.character_name,
                external_tools=external_tools,
                **(
                    {"external_tool_policy": external_tool_policy}
                    if external_tool_policy is not None
                    else {}
                ),
            )
        )
        self._publish_tool_selected_event(result)
        return result

    def _publish_tool_selected_event(self, result: ToolBuildResult) -> None:
        """发布本轮工具选择结果，供 Runtime/客户端观测工具注入控制面。"""
        self.event_bus.publish(
            Event(
                type=SystemEvent.TOOL_CALL_SELECTED,
                source="tool_build_service",
                data={
                    "enabled_tool_names": list(result.enabled_tool_names),
                    "tool_count": len(result.tools),
                    "model_supports_tools": result.model_supports_tools,
                    "disabled_reasons": dict(result.disabled_reasons),
                    "has_instructions": bool(result.instructions),
                },
            )
        )

    async def _prepend_scene_context(self, system_contexts: list[str]) -> list[str]:
        """对话开始时把当前场景描述放到系统上下文最前面（每会话仅一次）。"""
        if not self.scene_manager.enabled:
            return system_contexts
        try:
            scene_context = await self.scene_manager.build_injection_context()
        except Exception as e:
            logger.error(f"注入场景上下文失败: {e}")
            return system_contexts
        if scene_context:
            return [scene_context, *system_contexts]
        return system_contexts

    async def _on_generate_response(self, event: Event) -> None:
        user_input = event.data.get("user_input", "")
        system_contexts = list(event.data.get("system_contexts", []) or [])
        world_turn = bool(event.data.get("world_turn"))
        request_id = event.data.get("request_id")
        # MessageBuilder 需要文本做检索/搜索提示；实际多模态内容已在工作记忆中
        text_input = self._extract_text_from_content(user_input)

        # 对话开始时注入一次当前场景（新会话首轮 / resume 后首轮）；
        # 之后不再每轮注入，模型遗忘时可主动调用 get_current_scene。
        system_contexts = await self._prepend_scene_context(system_contexts)

        # 上一轮响应中断留下的半截回复：注入提示词让角色本轮接着说完。
        # 错误标记不注入（用户已看到，模型只需要干净的半截正文）。
        if self._half_completion is not None:
            system_contexts = [
                *system_contexts,
                build_half_completion_context(self._half_completion.content),
            ]

        # World 回合的每轮上下文（舞台/在场角色/共享剧本/演员身份）必须在
        # 工具调用后的 continuation 中保留，否则 Actor 调完工具就丢失舞台；
        # 单角色路径维持原行为，不重复注入。
        continuation_contexts = system_contexts if world_turn else None
        # world-turn 触发文本不入私有工作记忆，以临时 user 消息注入本轮生成
        # 与 continuation（单角色输入已在工作记忆末尾，不需要）。
        continuation_input = text_input if world_turn else ""

        full_response = ""
        try:
            await self._ensure_background_manager()
            tool_build_result = await self._build_tools()
            self.message_builder.update_tool_build_result(tool_build_result)
            messages = self.message_builder.build(
                text_input, system_contexts, ephemeral_input=world_turn
            )
            tools = tool_build_result.tools or None

            async for chunk in self.response_handler.process_stream(
                messages,
                tools,
                continuation_contexts=continuation_contexts,
                continuation_input=continuation_input,
            ):
                if self.is_shutting_down:
                    break
                if chunk.content:
                    full_response += chunk.content
                await self._action_executor.feed_chunk(chunk, request_id=request_id)  # type: ignore

        except Exception as e:
            logger.error(f"生成响应异常: {e}")
            error_msg = "\n[出了点问题]\n"
            if not full_response:
                full_response = error_msg
                await self._action_executor.feed_chunk(  # type: ignore
                    StreamChunk(
                        type="error",
                        content=error_msg,
                        error=str(e),
                        error_code="agent.response.failed",
                    ),
                    request_id=request_id,
                )

        finally:
            # 过期请求（已超时/取消）的孤儿生成必须零副作用：
            # 不写私有记忆（MESSAGE_SENT）、不调度主动定时器、不解决新请求。
            is_current = self._action_executor.is_current_request(request_id)  # type: ignore
            if full_response and is_current and STREAM_INTERRUPT_MARKER in full_response:
                # 响应中断：半截正文计入 HalfCompletionMessage 中间状态，
                # 下轮注入提示词让角色接着说完；不发 MESSAGE_SENT（半截不入
                # 工作记忆），错误标记只投递给用户、不进模型上下文。
                if partial := strip_interrupt_marker(full_response):
                    self._half_completion = HalfCompletionMessage(content=partial)
                    logger.info(f"响应中断，半截回复已计入中间状态（{len(partial)} 字）")
            elif full_response and is_current:
                # 正常说完：清除可能存在的半截状态，本轮按普通消息处理。
                self._half_completion = None
                data = {"content": full_response}
                # 回复对象标记：触发消息带【昵称】前缀（nb2 群聊）时一并传给
                # 记忆写入，助手消息在工作记忆里带「（对 某人）」——多人快节奏
                # 对话中模型据此归因「我刚才那句话是回谁的」，防张冠李戴。
                if reply_to := extract_speaker_tags(text_input):
                    data["reply_to"] = reply_to
                # reasoning_content 对 DeepSeek thinking mode 是多轮协议状态，
                # 不是调试展示内容；是否显示仍由 UI/日志层的 debug_silent_output 控制。
                if reasoning := self.response_handler.last_assistant_reasoning:
                    data["reasoning_content"] = reasoning
                self.event_bus.publish(
                    Event(
                        type=SystemEvent.MESSAGE_SENT,
                        source="agent",
                        data=data,
                    )
                )
                # 后台调度主动定时器，不阻塞 complete_response 和用户输入；
                # World Actor 不各自持有定时器（对话主循环在 World 侧）
                if self._manage_initiative_timer:
                    asyncio.create_task(self._initiative_coordinator.schedule_bg(full_response))
                # 定期记忆蒸馏（§8.29）：挂在主动机制总闸上——
                # 元租户（enabled=False）与 World Actor（manage=False）天然不触发
                if (
                    self._manage_initiative_timer
                    and self.config.initiative_timer.enabled
                    and self._think_engine is not None
                ):
                    self._think_engine.note_turn_for_distillation(
                        self.working_memory.get_recent(12)
                    )

            # 🔑 无论如何都要把控制权还给用户（过期请求由 id 校验忽略）
            if self._action_executor:
                self._action_executor.complete_response(full_response, request_id=request_id)

    # ==================== 主动定时器（委托 InitiativeCoordinator） ====================

    async def schedule_initiative_timer(self, assistant_response: str) -> dict | None:
        return await self._initiative_coordinator.schedule(assistant_response)

    async def discard_initiative_timer(
        self, *, reason: str = "discarded", source: str = "system"
    ) -> dict | None:
        return await self._initiative_coordinator.discard(reason=reason, source=source)

    def current_initiative_timer(self) -> dict | None:
        return self._initiative_coordinator.current()

    async def update_initiative_timer(
        self,
        *,
        timer_id: str | None = None,
        delay_seconds: int | float | None = None,
        due_at: str | None = None,
        pending_summary: str | None = None,
        enabled: bool | None = None,
    ) -> dict:
        return await self._initiative_coordinator.update(
            timer_id=timer_id,
            delay_seconds=delay_seconds,
            due_at=due_at,
            pending_summary=pending_summary,
            enabled=enabled,
        )

    async def cancel_initiative_timer(
        self, *, timer_id: str | None = None, reason: str = "cancelled"
    ) -> dict:
        return await self._initiative_coordinator.cancel(timer_id=timer_id, reason=reason)

    async def trigger_initiative_timer(self, *, timer_id: str | None = None) -> dict:
        return await self._initiative_coordinator.trigger(timer_id=timer_id)

    async def _on_shutdown(self) -> None:
        if self._generate_response_subscription_id is not None:
            self.event_bus.unsubscribe(self._generate_response_subscription_id)
            self._generate_response_subscription_id = None
        self.event_bus.publish(Event(type=SystemEvent.AGENT_SHUTDOWN, source="agent"))

        # 关机后普通自动保存不再提交后台任务；最终保存由 save_immediately 直接落盘。
        self.save_coordinator.set_shutting_down(True)

        if self._think_engine:
            await self._think_engine.stop()

        await self._initiative_coordinator.shutdown()

        if self._background_manager:
            await self._background_manager.stop(wait=True)

        try:
            await self.save_coordinator.save_immediately(self.working_memory)
        except Exception as e:
            logger.error(f"关闭时保存数据出错: {e}")

        await self.event_bus.stop()
        logger.info(f"Agent 已关闭{self._tenant_suffix}")

    async def shutdown(self) -> None:
        await self.lifecycle.shutdown()

    # ==================== 私有辅助方法 ====================

    async def _ensure_background_manager(self) -> None:
        if self._background_manager is None:
            self._background_manager = self._create_background_manager()
            await self._background_manager.start()
            if self._save_coordinator:
                self._save_coordinator.set_background_manager(self._background_manager)

    def _create_background_manager(self) -> BackgroundManager:
        manager = BackgroundManager(max_workers=2, max_queue_size=50, label=self._log_label or None)
        manager.register_persistence_worker(PersistenceWorker(self.session_manager.persistence))
        return manager
