"""GensokyoWorld 主类：多角色编排的状态机与对话主循环。

World 是「导演 + 舞台」的拥有者：
- 一个共享 ModelClient（显式绑 World 总线，模型层事件不污染 Actor 私有总线）
  与共享 resource gates；
- 每个 Actor 是独立 Agent（独立 EventBus/Session/Memory，不抢进程信号、不各自
  持有主动定时器）；
- WorldStage 管理在场位置，SharedTranscript 承载公开剧本，Director 决定每轮
  谁开口；
- 整段自动表演持有单一 turn lock，不允许两个用户请求同时推进戏。

对话真相源仍是各 Actor 的 Agent（私有 working memory / session / 语义记忆）；
World 只叠加多角色专属编排状态，单角色路径零改动。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import msgspec

from ..core.agent import Agent
from ..core.agent.model_client import ModelClient
from ..core.agent.runtime_context import AgentDependencies
from ..core.config import AppConfig, ConfigLoader, WorldActorConfig, WorldConfig
from ..core.events import Event, EventBus, SystemEvent
from ..runtime.resource_control import ResourceGate, build_resource_gates
from ..scene.manager import SceneManager
from ..utils.logger import logger
from ..utils.path_security import sanitize_path_id
from .director import Director
from .events import WorldActorTurnPayload, WorldSceneMovedPayload
from .initiative import WorldInitiativeLoop
from .memory_paths import build_world_memory_root
from .memory_projector import WorldMemoryProjector
from .persistence import WorldPersistence
from .stage import WorldStage
from .transcript import SharedTranscript
from .types import (
    USER_OCCUPANT_ID,
    ActorBrief,
    DirectorAction,
    DirectorContext,
    DirectorPhase,
    SpeakerKind,
    WorldPersistenceDiagnostic,
    WorldSessionRecord,
    WorldStateSnapshot,
    WorldTurn,
)

# 场景系统关闭或解析失败时使用的合成场景：所有占位同场，退化为单空间舞台
DEFAULT_SCENE_ID = "world_default"

# 开场/连轴转时给演员的通用舞台提示（首个回应者的触发用用户原文）
_OPENING_CUE = "（幕布拉开。请以你的身份、结合当前场景主动开场。）"
_NEXT_TURN_CUE = "（剧情继续，轮到你开口。）"

# 流式事件类型名（对齐设计文档 §8.1 协议）
STREAM_ACTOR_STARTED = "world.actor.started"
STREAM_ACTOR_CHUNK = "world.actor.chunk"
STREAM_ACTOR_COMPLETED = "world.actor.completed"
STREAM_WAITING_USER = "world.waiting_user"


class WorldAssemblyError(Exception):
    """World 装配失败；``diagnostics`` 携带结构化原因。"""

    def __init__(self, diagnostics: list[WorldPersistenceDiagnostic]) -> None:
        self.diagnostics = diagnostics
        message = "；".join(d.message for d in diagnostics)
        super().__init__(f"World 装配失败: {message}")


def _diag(code: str, message: str, actor_id: str | None = None) -> WorldPersistenceDiagnostic:
    return WorldPersistenceDiagnostic(
        code=code, severity="error", message=message, actor_id=actor_id
    )


class GensokyoWorld:
    """多角色编排主类（World 即对话主循环）。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        world_bus: EventBus,
        model_client: ModelClient,
        scene_library: SceneManager,
        actors: dict[str, Agent],
        briefs: dict[str, ActorBrief],
        stage: WorldStage,
        transcript: SharedTranscript,
        director: Director,
        persistence: WorldPersistence | None,
        session_record: WorldSessionRecord | None,
    ) -> None:
        self._config = config
        self._world_config: WorldConfig = config.world
        self._world_bus = world_bus
        self._model_client = model_client
        self._scene_library = scene_library
        self._actors = actors
        self._briefs = briefs
        self._stage = stage
        self._transcript = transcript
        self._director = director
        self._persistence = persistence
        self._session_record = session_record

        self._turn_lock = asyncio.Lock()
        self._waiting_for_user = True
        self._current_actor_id: str | None = None
        self._started = False
        self._shutdown = False
        # resume() 恢复时的 roster/会话诊断（create 时为空）
        self.resume_diagnostics: list[WorldPersistenceDiagnostic] = []

        # 记忆投影：段落结束（wait_user）后把公开表演投影为各在场角色
        # 的私有视角记忆（config.project_perspective_memories 关闭时停用）。
        # 游标记录每个场景已投影到第几条，后台任务不阻塞用户回复。
        self._projector = (
            WorldMemoryProjector(model_client)
            if self._world_config.project_perspective_memories
            else None
        )
        self._projection_cursors: dict[str, int] = {}
        self._projection_tasks: set[asyncio.Task] = set()

        # World 对话主循环的主动循环：整个世界只有一个主动定时器，
        # 段落结束统一规划，到点由 Director initiative 选角（阶段 7）。
        # 由 create() 在 initiative_timer.enabled 时安装。
        self._initiative_loop: WorldInitiativeLoop | None = None

    # ==================== 装配 ====================

    @classmethod
    async def create(cls, config: AppConfig) -> GensokyoWorld:
        """装配 World：共享大脑 + 每 Actor 独立总线/会话/记忆。

        硬失败（world 未启用、无 enabled actor、角色文件缺失、净化后角色名碰撞）
        抛 :class:`WorldAssemblyError`；场景 id 未知等软问题降级并记日志。
        """
        world_config = config.world
        enabled_actors = cls._validate_world_config(world_config)
        protagonist = world_config.protagonist
        world_bus, resource_gates, model_client, scene_library = await cls._build_infra(config)

        actors: dict[str, Agent] = {}
        briefs: dict[str, ActorBrief] = {}
        actor_sessions: dict[str, str] = {}
        loader = ConfigLoader()
        sanitized_names: dict[str, str] = {}  # 净化后角色名 -> actor_id（记忆根碰撞检查）
        try:
            for actor_cfg in enabled_actors:
                agent, brief, _resumed = await cls._assemble_actor(
                    actor_cfg,
                    config,
                    world_config,
                    model_client,
                    resource_gates,
                    loader,
                    sanitized_names,
                )
                actors[actor_cfg.id] = agent
                briefs[actor_cfg.id] = brief
                session = agent.session_manager.get_current_session()
                if session is not None:
                    actor_sessions[actor_cfg.id] = session.session_id
        except Exception:
            # 装配失败必须关停已启动的 Actor 与总线，避免泄漏后台任务
            for agent in actors.values():
                with contextlib.suppress(Exception):
                    await agent.shutdown()
            with contextlib.suppress(Exception):
                await world_bus.stop()
            raise

        stage = cls._build_initial_stage(config, world_config, enabled_actors, actors)
        transcript = SharedTranscript(world_config.transcript.max_entries_per_scene)
        director = Director(model_client, world_config.director, event_bus=world_bus)

        persistence: WorldPersistence | None = None
        session_record: WorldSessionRecord | None = None
        if world_config.persistence.enabled:
            persistence = WorldPersistence(world_config.persistence.save_path)
            session_record = await persistence.create_async(
                world_config.id,
                protagonist=protagonist,
                metadata={"source": "gensokyo_world"},
            )
            session_record.roster = {aid: brief.display_name for aid, brief in briefs.items()}
            session_record.actor_sessions = actor_sessions
            session_record.stage = stage.snapshot()
            await persistence.save_async(session_record)

        world = cls(
            config=config,
            world_bus=world_bus,
            model_client=model_client,
            scene_library=scene_library,
            actors=actors,
            briefs=briefs,
            stage=stage,
            transcript=transcript,
            director=director,
            persistence=persistence,
            session_record=session_record,
        )
        # 桥接每个 Actor 的场景切换：更新 WorldStage 并广播到 World 总线
        for actor_id, agent in actors.items():
            agent.event_bus.subscribe(
                SystemEvent.SCENE_SWITCHED, world._make_scene_bridge(actor_id)
            )
        # World 对话主循环的主动定时器（Actor 不各自持有定时器）
        if config.initiative_timer.enabled:
            world._initiative_loop = WorldInitiativeLoop(world)
        logger.info(
            f"🌸 [World] 装配完成: world={world_config.id}, "
            f"actors={list(actors)}, protagonist={protagonist}"
        )
        return world

    @classmethod
    def _validate_world_config(cls, world_config: WorldConfig) -> list[WorldActorConfig]:
        """world 配置硬校验（create/resume 共用），返回 enabled actor 列表。"""
        if not world_config.enabled:
            raise WorldAssemblyError(
                [_diag("world.disabled", "world.enabled 为 false，无法装配 World")]
            )
        enabled_actors = [actor for actor in world_config.actors if actor.enabled]
        if not enabled_actors:
            raise WorldAssemblyError(
                [_diag("world.no_actors", "world.enabled 为 true 时至少需要一个 enabled 的 actor")]
            )
        protagonist = world_config.protagonist
        if protagonist != USER_OCCUPANT_ID and protagonist not in {
            actor.id for actor in enabled_actors
        }:
            raise WorldAssemblyError(
                [
                    _diag(
                        "world.protagonist_unknown",
                        f"protagonist '{protagonist}' 不在 enabled roster 内",
                    )
                ]
            )
        return enabled_actors

    @classmethod
    async def _build_infra(
        cls, config: AppConfig
    ) -> tuple[EventBus, dict[str, ResourceGate], ModelClient, SceneManager]:
        """装配 World 级基础设施：World 总线、共享 gates、共享 ModelClient（绑
        World 总线）、共享场景库（create/resume 共用）。"""
        world_bus = EventBus(enable_trace=config.event_trace_enabled)
        await world_bus.start()
        resource_gates: dict[str, ResourceGate] = build_resource_gates(config.resource_control)
        # 共享 ModelClient 显式绑 World 总线：MODEL_* 事件汇到 World，Actor 私有
        # 总线不承载模型层事件
        model_client = ModelClient(
            config.model,
            event_bus=world_bus,
            embedding_config=config.embedding,
            resource_gates=resource_gates,
        )
        # 场景库仅作共享场景定义/渲染服务；多角色位置真相源是 WorldStage
        scene_library = SceneManager(config.scene)
        await scene_library.load_library()
        return world_bus, resource_gates, model_client, scene_library

    @classmethod
    async def resume(cls, config: AppConfig, session_id: str) -> GensokyoWorld:
        """从 World 存档恢复：还原舞台/共享剧本/各 Actor 私有会话。

        roster 差异经诊断呈现（`world.resume_diagnostics`）：配置中缺失的
        actor 不装配（绝不静默串角色）；缺失的 actor 私有会话降级为新建并
        给 warning 诊断。恢复后原存档作为活动存档继续保存。
        """
        world_config = config.world
        enabled_actors = cls._validate_world_config(world_config)
        if not world_config.persistence.enabled:
            raise WorldAssemblyError(
                [_diag("world.persistence_disabled", "persistence 已禁用，无法恢复 World 存档")]
            )
        persistence = WorldPersistence(world_config.persistence.save_path)
        result = await persistence.resume_async(
            world_config.id,
            session_id,
            available_actor_ids={actor.id for actor in enabled_actors},
        )
        if result is None:
            raise WorldAssemblyError(
                [_diag("world.session_not_found", f"World session 不存在: {session_id}")]
            )
        record = result.record
        diagnostics = list(result.diagnostics)

        world_bus, resource_gates, model_client, scene_library = await cls._build_infra(config)
        actors: dict[str, Agent] = {}
        briefs: dict[str, ActorBrief] = {}
        actor_sessions: dict[str, str] = {}
        loader = ConfigLoader()
        sanitized_names: dict[str, str] = {}
        try:
            for actor_cfg in enabled_actors:
                stored_session_id = record.actor_sessions.get(actor_cfg.id)
                agent, brief, resumed = await cls._assemble_actor(
                    actor_cfg,
                    config,
                    world_config,
                    model_client,
                    resource_gates,
                    loader,
                    sanitized_names,
                    resume_session_id=stored_session_id,
                )
                if stored_session_id and not resumed:
                    diagnostics.append(
                        WorldPersistenceDiagnostic(
                            code="world.persistence.actor_session_missing",
                            severity="warning",
                            actor_id=actor_cfg.id,
                            message=(
                                f"actor '{actor_cfg.id}' 的存档私有会话 "
                                f"{stored_session_id} 不存在，已新建会话"
                            ),
                        )
                    )
                actors[actor_cfg.id] = agent
                briefs[actor_cfg.id] = brief
                session = agent.session_manager.get_current_session()
                if session is not None:
                    actor_sessions[actor_cfg.id] = session.session_id
        except Exception:
            for agent in actors.values():
                with contextlib.suppress(Exception):
                    await agent.shutdown()
            with contextlib.suppress(Exception):
                await world_bus.stop()
            raise

        # 舞台：初始布置兜底，存档记录的位置覆盖（幽灵占位已被诊断过滤——
        # 只还原当前 roster 内 actor 与用户的位置）
        stage = cls._build_initial_stage(config, world_config, enabled_actors, actors)
        for occupant, scene_id in record.stage.items():
            if occupant == USER_OCCUPANT_ID or occupant in actors:
                stage.set_location(occupant, scene_id)

        # 共享剧本按场景分片还原
        transcript = SharedTranscript(world_config.transcript.max_entries_per_scene)
        for scene_entries in record.transcript.values():
            for entry in scene_entries:
                transcript.append(entry)

        director = Director(model_client, world_config.director, event_bus=world_bus)
        # 原存档作为活动存档继续保存；roster/actor_sessions 可能因增删 actor 变化
        record.roster = {aid: brief.display_name for aid, brief in briefs.items()}
        record.actor_sessions = actor_sessions
        record.stage = stage.snapshot()

        world = cls(
            config=config,
            world_bus=world_bus,
            model_client=model_client,
            scene_library=scene_library,
            actors=actors,
            briefs=briefs,
            stage=stage,
            transcript=transcript,
            director=director,
            persistence=persistence,
            session_record=record,
        )
        world._current_actor_id = (
            record.current_actor_id if record.current_actor_id in actors else None
        )
        world._waiting_for_user = record.waiting_for_user
        # 已投影过的旧剧本不重复投影：游标直接放到各场景末尾
        world._projection_cursors = {
            scene_id: len(entries) for scene_id, entries in record.transcript.items()
        }
        world.resume_diagnostics = diagnostics
        for actor_id, agent in actors.items():
            agent.event_bus.subscribe(
                SystemEvent.SCENE_SWITCHED, world._make_scene_bridge(actor_id)
            )
        if config.initiative_timer.enabled:
            world._initiative_loop = WorldInitiativeLoop(world)
        await persistence.save_async(record)
        logger.info(
            f"🌸 [World] 恢复完成: world={world_config.id}, session={session_id}, "
            f"actors={list(actors)}, diagnostics={len(diagnostics)}"
        )
        return world

    @classmethod
    async def _assemble_actor(
        cls,
        actor_cfg: WorldActorConfig,
        config: AppConfig,
        world_config: WorldConfig,
        model_client: ModelClient,
        resource_gates: dict[str, ResourceGate],
        loader: ConfigLoader,
        sanitized_names: dict[str, str],
        resume_session_id: str | None = None,
    ) -> tuple[Agent, ActorBrief, bool]:
        """装配单个 Actor：加载角色卡、注入 world 依赖、创建/恢复会话并启动。

        `resume_session_id` 提供时尝试恢复该私有会话，失败则新建；
        返回的第三个元素标记是否成功恢复。
        """
        if actor_cfg.character_file is None or not actor_cfg.character_file.exists():
            raise WorldAssemblyError(
                [
                    _diag(
                        "world.actor_file_missing",
                        f"actor '{actor_cfg.id}' 的角色文件不存在: {actor_cfg.character_file}",
                        actor_cfg.id,
                    )
                ]
            )
        character = loader.load_character(actor_cfg.character_file)
        safe_name = sanitize_path_id(character.name)
        if safe_name in sanitized_names:
            raise WorldAssemblyError(
                [
                    _diag(
                        "world.actor_name_collision",
                        f"actor '{actor_cfg.id}' 与 '{sanitized_names[safe_name]}' 的角色名"
                        f"净化后相同（'{character.name}'），会共享长期记忆根互踩——"
                        "请在角色卡中使用可区分的显示名",
                        actor_cfg.id,
                    )
                ]
            )
        sanitized_names[safe_name] = actor_cfg.id

        actor_config = msgspec.structs.replace(config, character=character)
        deps = AgentDependencies(
            model_client=model_client,
            resource_gates=resource_gates,
            actor_id=actor_cfg.id,
            world_id=world_config.id,
            semantic_memory_root=build_world_memory_root(
                config.session.save_path, world_config.id, character.name
            ),
        )
        agent = Agent(
            config=actor_config,
            dependencies=deps,
            setup_signal_handlers=False,
            manage_initiative_timer=False,
        )
        resumed = bool(resume_session_id) and agent.resume_session(resume_session_id)
        if not resumed:
            agent.create_session()
        await agent.start()
        brief = ActorBrief(
            actor_id=actor_cfg.id,
            display_name=character.name,
            summary=str(character.metadata.get("description", "")),
        )
        return agent, brief, resumed

    @classmethod
    def _build_initial_stage(
        cls,
        config: AppConfig,
        world_config: WorldConfig,
        enabled_actors: list[WorldActorConfig],
        actors: dict[str, Agent],
    ) -> WorldStage:
        """布置初始位置：begin_scene.scene > actor.initial_scene >
        world.user_initial_scene > scene.default_scene > 合成场景。"""
        stage = WorldStage()
        scene_enabled = config.scene.enabled
        for actor_cfg in enabled_actors:
            scene_id = None
            if scene_enabled:
                character = actors[actor_cfg.id].config.character
                begin_scene = getattr(character, "begin_scene", None)
                candidates = [
                    getattr(begin_scene, "scene", None),
                    actor_cfg.initial_scene,
                    world_config.user_initial_scene,
                    config.scene.default_scene,
                ]
                scene_id = next((sid for sid in candidates if sid), None)
            stage.set_location(actor_cfg.id, scene_id or DEFAULT_SCENE_ID)

        # 用户位置：protagonist 是角色则跟到其场景；否则按配置解析
        protagonist = world_config.protagonist
        user_scene: str | None = None
        if protagonist != USER_OCCUPANT_ID:
            user_scene = stage.scene_of(protagonist)
        elif scene_enabled:
            user_scene = world_config.user_initial_scene or config.scene.default_scene
        if user_scene is None and enabled_actors:
            # 未配置用户初始场景：落到第一个 actor 所在场景，避免开场无人同场
            user_scene = stage.scene_of(enabled_actors[0].id)
            logger.warning(f"[World] 用户初始场景未配置，落到 {user_scene}")
        stage.set_location(USER_OCCUPANT_ID, user_scene or DEFAULT_SCENE_ID)
        return stage

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        """开场：protagonist 是角色则主动开场并进入导演调度；是用户则等待。"""
        if self._started or self._shutdown:
            return
        self._started = True
        protagonist = self._world_config.protagonist
        async with self._turn_lock:
            if protagonist != USER_OCCUPANT_ID and protagonist in self._actors:
                self._waiting_for_user = False
                begin_action = self._begin_action_of(protagonist)
                trigger = begin_action or _OPENING_CUE
                async for _event in self._run_actor_turn_stream(protagonist, trigger, turn_index=1):
                    pass  # 开场不流式输出；正文已进入共享剧本
                async for _event in self._dialogue_events(
                    phase=DirectorPhase.AFTER_ACTOR,
                    trigger_text=_NEXT_TURN_CUE,
                    current=protagonist,
                    auto_count=1,
                    same_count=1,
                ):
                    pass
            else:
                # protagonist 是用户：只布置舞台，不生成虚假欢迎词
                self._waiting_for_user = True
                await self._publish_waiting_user()
        self._publish(
            SystemEvent.WORLD_STARTED,
            {
                "world_id": self._world_config.id,
                "session_id": self.session_id,
                "roster": self.roster_names,
                "stage": self._stage.snapshot(),
            },
        )
        await self._save_record()

    async def shutdown(self) -> None:
        """关闭 World：保存存档、关停所有 Actor、停止 World 总线。"""
        if self._shutdown:
            return
        self._shutdown = True
        self._publish(SystemEvent.WORLD_SHUTDOWN, {"world_id": self._world_config.id})
        # 先停掉世界主动循环，再等未完成的记忆投影落笔，最后保存存档、关停 Actor
        if self._initiative_loop is not None:
            await self._initiative_loop.shutdown()
        await self.flush_projections()
        await self._save_record()
        for actor_id, agent in self._actors.items():
            try:
                await agent.shutdown()
            except Exception as error:
                logger.warning(f"[World] Actor {actor_id} 关停异常: {error}")
        await self._world_bus.stop()

    # ==================== 用户回合 ====================

    async def send_message(self, user_input: str) -> list[WorldTurn]:
        """用户发言（非流式）：返回本段自动表演中各 Actor 的发言记录。"""
        turns: list[WorldTurn] = []
        async for event in self.send_message_stream(user_input):
            if event["type"] == STREAM_ACTOR_COMPLETED:
                turns.append(
                    WorldTurn(
                        actor_id=event["actor_id"],
                        actor_name=event["actor_name"],
                        scene_id=event["scene_id"],
                        content=event["content"],
                    )
                )
        return turns

    async def send_message_stream(self, user_input: str) -> AsyncIterator[dict[str, Any]]:
        """用户发言（流式）：产出 world.actor.* / world.waiting_user 事件。"""
        async with self._turn_lock:
            # 用户消息优先于世界主动：取消/作废尚未触发的世界主动意图
            await self._cancel_initiative("user_message")
            self._waiting_for_user = False
            user_scene = self._stage.scene_of(USER_OCCUPANT_ID) or DEFAULT_SCENE_ID
            self._transcript.add(
                scene_id=user_scene,
                speaker_kind=SpeakerKind.USER,
                speaker_id=USER_OCCUPANT_ID,
                speaker_name="用户",
                content=user_input,
            )
            async for event in self._dialogue_events(
                phase=DirectorPhase.AFTER_USER,
                trigger_text=user_input,
                current=self._current_actor_id,
                auto_count=0,
                same_count=0,
            ):
                yield event
            await self._save_record()

    # ==================== 对话主循环（导演调度状态机） ====================

    async def _dialogue_events(
        self,
        *,
        phase: DirectorPhase,
        trigger_text: str,
        current: str | None,
        auto_count: int,
        same_count: int,
        initiative_summary: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """自动表演段：导演决策 → 演员回合 → 再决策，直到 wait_user/熔断。"""
        decision = await self._decide(
            phase, current, auto_count, same_count, initiative_summary=initiative_summary
        )
        turn_index = auto_count
        while decision.action is not DirectorAction.WAIT_USER:
            speaker = (
                current if decision.action is DirectorAction.CONTINUE else (decision.next_actor_id)
            )
            if speaker is None or speaker not in self._actors:
                # Director 已校验过，这里是防御双保险：绝不死循环
                logger.warning(f"[World] 决策指向不可用演员 {speaker}，强制等待用户")
                break
            async for event in self._run_actor_turn_stream(
                speaker, trigger_text, turn_index=turn_index + 1
            ):
                yield event
            same_count = same_count + 1 if speaker == current else 1
            current = speaker
            auto_count += 1
            turn_index = auto_count
            trigger_text = _NEXT_TURN_CUE
            decision = await self._decide(
                DirectorPhase.AFTER_ACTOR,
                current,
                auto_count,
                same_count,
                initiative_summary=initiative_summary,
            )
        self._current_actor_id = current
        self._waiting_for_user = True
        # 一段自动表演结束：后台投影各在场角色的私有视角记忆，不阻塞用户
        self._schedule_projection()
        # World 主循环统一规划下一次世界主动（Actor 不各自持有定时器）
        await self._plan_initiative_after_segment()
        await self._publish_waiting_user()
        yield {"type": STREAM_WAITING_USER}

    async def _run_actor_turn_stream(
        self, actor_id: str, trigger_text: str, *, turn_index: int
    ) -> AsyncIterator[dict[str, Any]]:
        """驱动一名 Actor 开口：注入舞台上下文、流出正文、写入共享剧本。"""
        agent = self._actors[actor_id]
        brief = self._briefs[actor_id]
        scene_id = self._stage.scene_of(actor_id) or DEFAULT_SCENE_ID
        # 回合开始即登记当前演员：scene_switch 的用户跟随、导演上下文都依赖它
        self._current_actor_id = actor_id
        contexts = await self._build_actor_contexts(actor_id, scene_id)
        started = WorldActorTurnPayload(
            actor_id=actor_id,
            actor_name=brief.display_name,
            scene_id=scene_id,
            turn_index=turn_index,
        )
        self._publish(SystemEvent.WORLD_ACTOR_TURN_STARTED, started)
        yield {
            "type": STREAM_ACTOR_STARTED,
            "actor_id": actor_id,
            "actor_name": brief.display_name,
            "scene_id": scene_id,
        }

        content_parts: list[str] = []
        async for chunk in agent.send_world_turn_stream(trigger_text, contexts):
            # 错误块（超时/失败）不进演出与剧本；工具调用块属演员私域，不转发
            if chunk.type == "error" or not chunk.content:
                continue
            content_parts.append(chunk.content)
            self._publish(
                SystemEvent.WORLD_ACTOR_TURN_CHUNK,
                {"actor_id": actor_id, "content": chunk.content},
            )
            yield {
                "type": STREAM_ACTOR_CHUNK,
                "actor_id": actor_id,
                "content": chunk.content,
            }
        content = "".join(content_parts)

        # 回合中演员可能用 scene_switch 移动——以其最终所在场景落剧本
        final_scene = self._stage.scene_of(actor_id) or scene_id
        if content.strip():
            self._transcript.add(
                scene_id=final_scene,
                speaker_kind=SpeakerKind.CHARACTER,
                speaker_id=actor_id,
                speaker_name=brief.display_name,
                content=content,
            )
        completed = WorldActorTurnPayload(
            actor_id=actor_id,
            actor_name=brief.display_name,
            scene_id=final_scene,
            content=content,
            turn_index=turn_index,
        )
        self._publish(SystemEvent.WORLD_ACTOR_TURN_COMPLETED, completed)
        yield {
            "type": STREAM_ACTOR_COMPLETED,
            "actor_id": actor_id,
            "actor_name": brief.display_name,
            "scene_id": final_scene,
            "content": content,
        }

    async def _decide(
        self,
        phase: DirectorPhase,
        current: str | None,
        auto_count: int,
        same_count: int,
        *,
        initiative_summary: str = "",
    ):
        """现算同场候选并调用导演（每次决策前重算，绝不选中已离场角色）。"""
        user_scene = self._stage.scene_of(USER_OCCUPANT_ID) or DEFAULT_SCENE_ID
        candidate_ids = [
            aid for aid in self._stage.characters_in(user_scene) if aid in self._actors
        ]
        context = DirectorContext(
            phase=phase,
            scene_id=user_scene,
            candidates=[self._briefs[aid] for aid in candidate_ids],
            current_actor_id=current if current in candidate_ids else None,
            transcript_text=self._transcript.render_for_scene(
                user_scene, limit=self._world_config.transcript.context_entries
            ),
            scene_description=await self._render_scene(user_scene),
            auto_turn_count=auto_count,
            same_actor_turn_count=same_count,
            initiative_summary=initiative_summary,
        )
        return await self._director.decide(context)

    # ==================== 舞台上下文与场景联动 ====================

    async def _build_actor_contexts(self, actor_id: str, scene_id: str) -> list[str]:
        """为演员回合组装系统上下文：场景、在场、共享剧本、身份与禁代言规则。"""
        contexts: list[str] = []
        scene_render = await self._render_scene(scene_id)
        if scene_render:
            contexts.append(f"【当前场景】\n{scene_render}")
        others = [
            brief.display_name
            for aid in self._stage.characters_in(scene_id)
            if aid != actor_id and (brief := self._briefs.get(aid))
        ]
        if self._stage.scene_of(USER_OCCUPANT_ID) == scene_id:
            others.append("用户")
        contexts.append(f"【在场】{'、'.join(others) if others else '只有你自己'}")
        script = self._transcript.render_for_scene(
            scene_id, limit=self._world_config.transcript.context_entries
        )
        if script:
            contexts.append(f"【共享剧本（公开场合发生的对话与事件）】\n{script}")
        brief = self._briefs[actor_id]
        contexts.append(
            f"【你的身份】你是 {brief.display_name}。在这个多角色舞台上，你只能以 "
            f"{brief.display_name} 的身份说话与行动；禁止替其他角色或用户代言、"
            "描写他们的言行举止。"
        )
        return contexts

    def _make_scene_bridge(self, actor_id: str):
        """生成订阅某 Actor SCENE_SWITCHED 的闭包：更新 WorldStage + 用户跟随。"""

        async def _on_scene_switched(event: Event) -> None:
            data = event.data or {}
            to_scene = data.get("scene_id")
            if not to_scene:
                return
            from_scene = data.get("from_scene_id") or self._stage.scene_of(actor_id)
            user_moved = False
            user_scene = self._stage.scene_of(USER_OCCUPANT_ID)
            if (
                self._world_config.user_follows_current_actor
                and actor_id == self._current_actor_id
                and user_scene is not None
                and user_scene == from_scene
            ):
                # 用户跟随当前演员：同一原子步内落到新场景
                await self._stage.move_together([actor_id, USER_OCCUPANT_ID], to_scene)
                user_moved = True
            else:
                await self._stage.move(actor_id, to_scene)

            brief = self._briefs[actor_id]
            from_name = await self._scene_name(from_scene)
            to_name = await self._scene_name(to_scene)
            if user_moved:
                content = f"{brief.display_name}和你从{from_name}来到{to_name}"
            else:
                content = f"{brief.display_name}从{from_name}来到{to_name}"
            # 公开过渡事件写入目的地场景：移动是公开事实，不是秘密
            self._transcript.add(
                scene_id=to_scene,
                speaker_kind=SpeakerKind.SYSTEM,
                speaker_id="system",
                speaker_name="",
                content=content,
            )
            self._publish(
                SystemEvent.WORLD_SCENE_MOVED,
                WorldSceneMovedPayload(
                    occupant_id=actor_id,
                    from_scene_id=from_scene,
                    to_scene_id=to_scene,
                    user_moved=user_moved,
                ),
            )
            await self._save_record()

        return _on_scene_switched

    async def move_user(self, scene_id: str) -> None:
        """把用户移动到指定场景（RPC/控制台入口；校验场景存在）。"""
        if (
            scene_id != DEFAULT_SCENE_ID
            and self._scene_library.enabled
            and await self._scene_library.get_scene(scene_id) is None
        ):
            raise ValueError(f"未知场景: {scene_id}")
        async with self._turn_lock:
            from_scene = self._stage.scene_of(USER_OCCUPANT_ID)
            await self._stage.move(USER_OCCUPANT_ID, scene_id)
            from_name = await self._scene_name(from_scene)
            to_name = await self._scene_name(scene_id)
            self._transcript.add(
                scene_id=scene_id,
                speaker_kind=SpeakerKind.SYSTEM,
                speaker_id="system",
                speaker_name="",
                content=f"你从{from_name}来到{to_name}",
            )
            self._publish(
                SystemEvent.WORLD_SCENE_MOVED,
                WorldSceneMovedPayload(
                    occupant_id=USER_OCCUPANT_ID,
                    from_scene_id=from_scene,
                    to_scene_id=scene_id,
                    user_moved=True,
                ),
            )
            await self._save_record()

    # ==================== 查询 ====================

    @property
    def world_id(self) -> str:
        return self._world_config.id

    @property
    def session_id(self) -> str | None:
        return self._session_record.session_id if self._session_record else None

    @property
    def roster_names(self) -> dict[str, str]:
        """actor_id -> 显示名。"""
        return {aid: brief.display_name for aid, brief in self._briefs.items()}

    @property
    def waiting_for_user(self) -> bool:
        return self._waiting_for_user

    @property
    def model_streaming(self) -> bool:
        """底层模型是否开启流式输出（前端显示策略用）。"""
        return bool(self._config.model.stream)

    @property
    def event_bus(self) -> EventBus:
        """World 自有事件总线（Runtime/控制台订阅 world.* 事件的入口）。"""
        return self._world_bus

    def transcript_history(self, scene_id: str, limit: int | None = None) -> list:
        """某场景的公开剧本记录（Runtime `world.transcript` 与控制台用）。"""
        return self._transcript.history(scene_id, limit)

    def state_snapshot(self) -> WorldStateSnapshot:
        """当前状态只读快照，供 Runtime/控制台查询。"""
        return WorldStateSnapshot(
            world_id=self._world_config.id,
            session_id=self.session_id,
            protagonist=self._world_config.protagonist,
            current_actor_id=self._current_actor_id,
            waiting_for_user=self._waiting_for_user,
            stage=self._stage.snapshot(),
            roster=self.roster_names,
            transcript_counts=self._transcript.counts(),
        )

    # ==================== 内部工具 ====================

    async def _run_initiative_turn(self, plan, *, cue: str) -> None:
        """世界主动触发：由 Director initiative 从当下在场角色中选角开口。"""
        self._waiting_for_user = False
        async for _event in self._dialogue_events(
            phase=DirectorPhase.INITIATIVE,
            trigger_text=cue,
            current=self._current_actor_id,
            auto_count=0,
            same_count=0,
            initiative_summary=plan.summary,
        ):
            pass  # 主动表演不挂流式输出；world.* 事件已广播到 World 总线

    async def _plan_initiative_after_segment(self) -> None:
        """段落结束后让 World 主循环统一规划下一次世界主动。"""
        if self._initiative_loop is None or self._shutdown:
            return
        await self._initiative_loop.plan_after_segment()

    async def _cancel_initiative(self, reason: str) -> None:
        """取消当前世界主动计划（用户发言/关机等）。"""
        if self._initiative_loop is not None:
            await self._initiative_loop.cancel(reason)

    async def _render_scene(self, scene_id: str) -> str:
        """渲染场景描述（场景库关闭或合成场景时为空）。"""
        if not self._scene_library.enabled or scene_id == DEFAULT_SCENE_ID:
            return ""
        scene = await self._scene_library.get_scene(scene_id)
        return scene.render() if scene else ""

    async def _scene_name(self, scene_id: str | None) -> str:
        """场景 id 翻译为显示名；未知时回退 id 本身。"""
        if scene_id is None:
            return "某处"
        if self._scene_library.enabled and scene_id != DEFAULT_SCENE_ID:
            scene = await self._scene_library.get_scene(scene_id)
            if scene is not None:
                return scene.name
        return scene_id

    def _begin_action_of(self, actor_id: str) -> str:
        """取 protagonist 角色卡的 begin_scene.action 作为开场动作。"""
        character = self._actors[actor_id].config.character
        begin_scene = getattr(character, "begin_scene", None)
        return getattr(begin_scene, "action", "") or ""

    def _publish(self, event_type: SystemEvent, data: Any) -> None:
        """发布 World 事件；发布失败不影响主流程。"""
        try:
            self._world_bus.publish(Event(type=event_type, source="gensokyo.world", data=data))
        except Exception as error:
            logger.warning(f"[World] 事件发布失败 {event_type.value}: {error}")

    async def _publish_waiting_user(self) -> None:
        self._publish(SystemEvent.WORLD_WAITING_USER, {"world_id": self._world_config.id})

    async def _save_record(self) -> None:
        """把当前舞台状态写入 World 存档（完整恢复编排在阶段 8 接入）。"""
        if self._persistence is None or self._session_record is None:
            return
        try:
            record = self._session_record
            record.stage = self._stage.snapshot()
            record.current_actor_id = self._current_actor_id
            record.waiting_for_user = self._waiting_for_user
            record.transcript = {
                scene_id: self._transcript.history(scene_id)
                for scene_id in self._transcript.scene_ids()
            }
            await self._persistence.save_async(record)
        except Exception as error:
            logger.warning(f"[World] 存档保存失败（不阻塞对话）: {error}")

    # ==================== 记忆投影 ====================

    def _schedule_projection(self) -> None:
        """段落结束后后台启动一次记忆投影（不阻塞用户回复）。"""
        if self._projector is None or self._shutdown:
            return
        task = asyncio.create_task(self._project_segment())
        self._projection_tasks.add(task)
        task.add_done_callback(self._projection_tasks.discard)

    async def flush_projections(self) -> None:
        """等待所有未完成的记忆投影（关机与测试用）。"""
        if self._projection_tasks:
            await asyncio.gather(*self._projection_tasks, return_exceptions=True)

    async def _project_segment(self) -> None:
        """把本段新增公开剧本投影为各在场角色的私有视角记忆。"""
        if self._projector is None:
            return
        try:
            scene_id = self._stage.scene_of(USER_OCCUPANT_ID) or DEFAULT_SCENE_ID
            entries = self._transcript.history(scene_id)
            cursor = self._projection_cursors.get(scene_id, 0)
            new_entries = entries[cursor:]
            self._projection_cursors[scene_id] = len(entries)
            if not new_entries:
                return

            # 只给亲历/在场角色写入：本段发言者 ∪ 当前在场角色
            participant_ids = {
                entry.speaker_id
                for entry in new_entries
                if entry.speaker_kind is SpeakerKind.CHARACTER
            } | set(self._stage.characters_in(scene_id))
            participants = [
                self._briefs[aid] for aid in sorted(participant_ids) if aid in self._actors
            ]
            if not participants:
                return

            lines = []
            for entry in new_entries:
                if entry.speaker_kind is SpeakerKind.SYSTEM:
                    lines.append(f"（{entry.content}）")
                else:
                    lines.append(f"{entry.speaker_name}：{entry.content}")
            memories = await self._projector.project(
                scene_id=scene_id,
                scene_name=await self._scene_name(scene_id),
                transcript_text="\n".join(lines),
                participants=participants,
            )
            for memory in memories:
                agent = self._actors.get(memory.actor_id)
                if agent is None:
                    continue
                try:
                    await agent.semantic_memory.add_async(
                        content=memory.summary,
                        importance=memory.importance,
                        emotional_valence=memory.emotional_valence,
                        topic_name=memory.topic_name or None,
                    )
                except Exception as error:
                    logger.warning(f"[World] 记忆投影写入失败 {memory.actor_id}: {error}")
        except Exception as error:
            logger.warning(f"[World] 记忆投影失败（不阻塞对话）: {error}")
