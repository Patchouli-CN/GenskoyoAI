"""World 事件载荷类型。

World 自有事件总线上发布的结构化载荷；事件名已对齐设计文档 §8.1 的流式
协议（`world.actor.started` / `world.actor.chunk` / `world.actor.completed` /
`world.scene.moved` / `world.waiting_user`），阶段 9 的 Runtime 桥接可直接映射。
"""

from __future__ import annotations

from msgspec import Struct


class WorldActorTurnPayload(Struct):
    """演员回合事件载荷（started/completed；chunk 事件复用 actor_id+content）。"""

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
