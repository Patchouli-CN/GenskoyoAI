"""GensokyoWorld 多角色编排层。

在现有单角色 `Agent`（演员）之上增加「导演 + 舞台」：由 Director 决定每轮谁发言，
WorldStage 管理角色在场位置，SharedTranscript 承载全场景可见的共享剧本，
GensokyoWorld 作为对话主循环驱动整台戏。

当前已落地数据层/持久化（阶段 2）、Director 选角（阶段 4）、
GensokyoWorld 主类/状态机（阶段 5）、私有记忆投影（阶段 6）、
DialogueLoop 抽象与对话欲（阶段 7）、持久化恢复（阶段 8）与
Runtime/Console 接入（阶段 9）。
"""

from .director import Director
from .events import WorldActorTurnPayload, WorldSceneMovedPayload
from .initiative import WorldInitiativeLoop
from .memory_paths import build_world_memory_root
from .memory_projector import PerspectiveMemory, WorldMemoryProjector
from .persistence import WorldPersistence, WorldPersistenceError
from .stage import WorldStage
from .transcript import SharedTranscript
from .types import (
    USER_OCCUPANT_ID,
    ActorBrief,
    DirectorAction,
    DirectorContext,
    DirectorDecision,
    DirectorPhase,
    SpeakerKind,
    TranscriptEntry,
    WorldLoadResult,
    WorldPersistenceDiagnostic,
    WorldSessionRecord,
    WorldStateSnapshot,
    WorldTurn,
)
from .world import DEFAULT_SCENE_ID, GensokyoWorld, WorldAssemblyError

__all__ = [
    "USER_OCCUPANT_ID",
    "DEFAULT_SCENE_ID",
    "ActorBrief",
    "Director",
    "DirectorAction",
    "DirectorContext",
    "DirectorDecision",
    "DirectorPhase",
    "GensokyoWorld",
    "PerspectiveMemory",
    "SpeakerKind",
    "TranscriptEntry",
    "WorldActorTurnPayload",
    "WorldAssemblyError",
    "WorldInitiativeLoop",
    "WorldLoadResult",
    "WorldMemoryProjector",
    "WorldPersistence",
    "WorldPersistenceDiagnostic",
    "WorldPersistenceError",
    "WorldSceneMovedPayload",
    "WorldSessionRecord",
    "WorldStage",
    "WorldStateSnapshot",
    "WorldTurn",
    "SharedTranscript",
    "build_world_memory_root",
]
