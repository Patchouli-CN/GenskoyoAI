"""SharedTranscript - 共享剧本，按场景分片。

这是防穿帮的核心：角色只看到「自己当前场景」最近 N 条公开记录，
魔法森林里说的话不会泄漏给红魔馆里的角色。共享剧本不写进任何 Actor 的私有
working memory——它是舞台层的独立数据，切场景时只注入对应场景的片段。
"""

from __future__ import annotations

from .types import SpeakerKind, TranscriptEntry


class SharedTranscript:
    """按 scene_id 分片存储公开发言/动作/场景事件。"""

    def __init__(self, max_entries_per_scene: int = 500) -> None:
        self._max_entries_per_scene = max_entries_per_scene
        self._by_scene: dict[str, list[TranscriptEntry]] = {}
        # 每场景单调递增的追加总数（截断不减）——投影游标按它追踪才截断安全
        self._total_appended: dict[str, int] = {}

    def append(self, entry: TranscriptEntry) -> TranscriptEntry:
        """追加一条记录到其所属场景分片，超限时从头截断。"""
        bucket = self._by_scene.setdefault(entry.scene_id, [])
        bucket.append(entry)
        self._total_appended[entry.scene_id] = self._total_appended.get(entry.scene_id, 0) + 1
        if len(bucket) > self._max_entries_per_scene:
            # 保留最近 max 条，丢弃最旧的
            del bucket[: len(bucket) - self._max_entries_per_scene]
        return entry

    def new_entries_since(self, scene_id: str, cursor: int) -> tuple[list[TranscriptEntry], int]:
        """返回 (游标之后的新记录, 新游标)。

        游标按「追加总数」计而非当前分片下标：分片截断后绝对下标整体前移，
        按下标追踪会永久停摆（M2：饱和场景的记忆投影静默死亡）。
        """
        bucket = self._by_scene.get(scene_id, [])
        total = self._total_appended.get(scene_id, 0)
        trimmed = total - len(bucket)
        start = max(0, cursor - trimmed)
        return list(bucket[start:]), total

    def add(
        self,
        *,
        scene_id: str,
        speaker_kind: SpeakerKind,
        speaker_id: str,
        speaker_name: str,
        content: str,
        metadata: dict | None = None,
    ) -> TranscriptEntry:
        """构造并追加一条记录，返回该记录。"""
        return self.append(
            TranscriptEntry(
                scene_id=scene_id,
                speaker_kind=speaker_kind,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                content=content,
                metadata=dict(metadata or {}),
            )
        )

    def history(self, scene_id: str, limit: int | None = None) -> list[TranscriptEntry]:
        """返回某场景的记录；limit 指定时只取最近若干条。"""
        bucket = self._by_scene.get(scene_id, [])
        if limit is not None and limit >= 0:
            return list(bucket[-limit:])
        return list(bucket)

    def render_for_scene(self, scene_id: str, limit: int | None = None) -> str:
        """渲染某场景最近记录为可注入模型的共享剧本文本。"""
        entries = self.history(scene_id, limit)
        if not entries:
            return ""
        lines: list[str] = []
        for entry in entries:
            if entry.speaker_kind is SpeakerKind.SYSTEM:
                lines.append(f"（{entry.content}）")
            else:
                lines.append(f"{entry.speaker_name}：{entry.content}")
        return "\n".join(lines)

    def counts(self) -> dict[str, int]:
        """返回各场景当前记录条数，用于状态快照。"""
        return {scene_id: len(bucket) for scene_id, bucket in self._by_scene.items()}

    def scene_ids(self) -> list[str]:
        """返回已有记录的场景 id 列表。"""
        return list(self._by_scene.keys())
