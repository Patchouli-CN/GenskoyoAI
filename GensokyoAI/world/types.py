"""GensokyoWorld 核心数据类型。

纯数据结构（msgspec Struct / Enum），不含编排逻辑，便于独立单元测试与序列化。
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from msgspec import Struct, field

# 用户在舞台上的固定占位 id；同时用作 protagonist 哨兵（主角是用户时）。
USER_OCCUPANT_ID = "__user__"


class DirectorAction(StrEnum):
    """导演每轮的调度动作。"""

    CONTINUE = "continue"  # 当前角色继续说
    SWITCH = "switch"  # 换一个角色上场
    WAIT_USER = "wait_user"  # 把话筒交还用户


class SpeakerKind(StrEnum):
    """共享剧本中一条发言的来源类别。"""

    USER = "user"
    CHARACTER = "character"
    SYSTEM = "system"  # 公开场景事件，如"魔理沙从魔法森林来到红魔馆"


class TranscriptEntry(Struct):
    """共享剧本中的一条记录（舞台上可被看到/听到的内容）。

    只承载公开信息；导演 reason、模型推理、私有记忆结果绝不写入。
    """

    scene_id: str
    speaker_kind: SpeakerKind
    speaker_id: str  # 角色 actor_id / USER_OCCUPANT_ID / "system"
    speaker_name: str
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class DirectorDecision(Struct):
    """导演一次决策的结构化结果。"""

    action: DirectorAction
    next_actor_id: str | None = None  # action=SWITCH 时的目标 actor_id
    reason: str = ""  # 调度理由，仅调试/日志可见，不进剧本
    confidence: float = 0.0
    fallback_applied: bool = False  # 是否因非法决策/解析失败触发了降级


class DirectorPhase(StrEnum):
    """导演决策的触发时机。"""

    AFTER_USER = "after_user"  # 用户刚发言，选择首个回应者
    AFTER_ACTOR = "after_actor"  # 某角色刚说完，决定继续/换人/交还用户
    INITIATIVE = "initiative"  # 沉默后世界主动推动剧情（阶段 7 接入完整链路）


class ActorBrief(Struct, frozen=True):
    """Director 可见的候选角色公开摘要。

    只承载 id、显示名与公开简介/metadata；绝不注入角色的完整私有 prompt 或记忆。
    """

    actor_id: str
    display_name: str
    summary: str = ""  # 公开层角色简介（一两句人设/metadata）


class DirectorContext(Struct):
    """一次导演决策的输入快照。

    由 World 在每次决策前现算：候选角色必须是「用户当前场景内在场且 enabled」的
    角色集合（ WorldStage 移动后重新计算），保证导演绝不选中已离场角色。
    """

    phase: DirectorPhase
    scene_id: str  # 用户当前所在场景
    candidates: list[ActorBrief]  # 同场候选角色（不含用户）
    current_actor_id: str | None = None  # 当前发言角色（等待首个回应者时为 None）
    transcript_text: str = ""  # 当前场景最近共享剧本（渲染后文本）
    scene_description: str = ""  # 当前场景环境描述（可选）
    auto_turn_count: int = 0  # 本段自动表演已连续进行的轮数
    same_actor_turn_count: int = 0  # 当前角色已连续发言的轮数
    initiative_summary: str = ""  # 待表达的世界意图摘要（phase=INITIATIVE 时使用）


class WorldStateSnapshot(Struct):
    """World 当前状态的只读快照，供前端 / Runtime 查询。"""

    world_id: str
    session_id: str | None = None
    protagonist: str = USER_OCCUPANT_ID
    current_actor_id: str | None = None
    waiting_for_user: bool = True
    # occupant_id -> scene_id（含 USER_OCCUPANT_ID）
    stage: dict[str, str] = field(default_factory=dict)
    # actor_id -> 显示名
    roster: dict[str, str] = field(default_factory=dict)
    # scene_id -> 该场景剧本条数
    transcript_counts: dict[str, int] = field(default_factory=dict)


class WorldTurn(Struct):
    """一段自动表演中一名 Actor 的发言记录（非流式接口的返回单位）。"""

    actor_id: str
    actor_name: str
    scene_id: str
    content: str


class WorldSessionRecord(Struct):
    """可独立持久化的 World 会话记录。

    Director 与 World initiative 尚未落地，因此对应状态先使用可序列化映射保留扩展
    边界；后续阶段由实际组件负责与其强类型状态互转，不在本数据层猜测业务字段。
    """

    world_id: str
    session_id: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    protagonist: str = USER_OCCUPANT_ID
    current_actor_id: str | None = None
    waiting_for_user: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    roster: dict[str, str] = field(default_factory=dict)
    # actor_id -> 该 Actor 的私有 session_id；完整恢复编排在阶段 8 接入。
    actor_sessions: dict[str, str] = field(default_factory=dict)
    stage: dict[str, str] = field(default_factory=dict)
    transcript: dict[str, list[TranscriptEntry]] = field(default_factory=dict)
    director_state: dict[str, Any] = field(default_factory=dict)
    initiative_state: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """更新最后修改时间。"""
        self.updated_at = time.time()


class WorldPersistenceDiagnostic(Struct, frozen=True):
    """恢复 World 存档时返回的结构化诊断。"""

    code: str
    severity: str
    message: str
    actor_id: str | None = None


class WorldLoadResult(Struct):
    """World 存档及其 roster 兼容性诊断。"""

    record: WorldSessionRecord
    diagnostics: list[WorldPersistenceDiagnostic] = field(default_factory=list)


class WorldActorTurnPayload(Struct):
    """演员回合事件载荷（started/completed；chunk 事件复用 actor_id+content）。

    事件名已对齐设计文档 §8.1 的流式协议（`world.actor.started` / `world.actor.chunk` /
    `world.actor.completed` / `world.scene.moved` / `world.waiting_user`），阶段 9 的
    Runtime 桥接可直接映射。原独立模块 world/events.py 仅含此两结构体，并入本文件。
    """

    actor_id: str
    actor_name: str
    scene_id: str
    content: str = ""  # completed 时的完整正文；started 为空
    turn_index: int = 0  # 本段自动表演中的回合序号（从 1 起）


class WorldSceneMovedPayload(Struct):
    """舞台位置变化事件载荷。"""

    occupant_id: str  # 移动者：actor_id 或 USER_OCCUPANT_ID
    from_scene_id: str | None
    to_scene_id: str
    user_moved: bool = False  # 本次移动是否携带了用户跟随
