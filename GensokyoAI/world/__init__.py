"""GensokyoWorld 多角色编排层。

在现有单角色 `Agent`（演员）之上增加「导演 + 舞台」：由 Director 决定每轮谁发言，
WorldStage 管理角色在场位置，SharedTranscript 承载全场景可见的共享剧本。

当前已落地数据层/持久化（阶段 2）与 Director 选角（阶段 4）；
World 主类与调度状态机在后续阶段接入。
"""

from .director import Director
from .memory_paths import build_world_memory_root
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
)

__all__ = [
    "USER_OCCUPANT_ID",
    "ActorBrief",
    "Director",
    "DirectorAction",
    "DirectorContext",
    "DirectorDecision",
    "DirectorPhase",
    "SpeakerKind",
    "TranscriptEntry",
    "WorldLoadResult",
    "WorldPersistence",
    "WorldPersistenceDiagnostic",
    "WorldPersistenceError",
    "WorldSessionRecord",
    "WorldStage",
    "WorldStateSnapshot",
    "SharedTranscript",
    "build_world_memory_root",
]
