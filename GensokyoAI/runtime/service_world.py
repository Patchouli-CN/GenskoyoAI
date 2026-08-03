"""RuntimeService 的 World 编排区块（mixin 抽取，原样自 service.py 搬移）。

`dispatch_rpc` 经 `getattr(service, handler_name)` 分发，因此将 world_* 方法
收进本 mixin 后所有调用方（tests / http_adapter / host）零改动。
对 `self.*` 的依赖（锁、状态、资源闸、幂等账本助手等）由 RuntimeService 提供。

pyright 对 mixin 模式的 self 属性访问无法静态推断，按文件定向关闭该诊断
（其余类型诊断保持开启；行为由 world 测试矩阵锁定）。
"""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast
from uuid import uuid4

from msgspec import to_builtins

from GensokyoAI.core.config import ConfigLoader
from GensokyoAI.runtime.auth import current_principal
from GensokyoAI.runtime.operation_store import RuntimeOperationStore
from GensokyoAI.runtime.rpc import RpcError, runtime_error_to_dict
from GensokyoAI.world.persistence import WorldPersistence
from GensokyoAI.world.types import USER_OCCUPANT_ID
from GensokyoAI.world.world import GensokyoWorld, WorldAssemblyError


class WorldOpsMixin:
    """World 多角色编排（装配/消息/流式/状态/存档/生命周期）。"""

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
                or self._fallback_config_path()
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
            await self._begin_message_operation(
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
                await self._succeed_message_operation(ledger_id, idempotency_key, result)
                return result
            except asyncio.CancelledError:
                await self._cancel_message_operation(ledger_id, idempotency_key)
                raise
            except Exception as error:
                await self._fail_message_operation(ledger_id, idempotency_key, error)
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
                service._tenant_operation_scope(),
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
            await self._begin_message_operation(
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
                await self._cancel_message_operation(ledger_id, idempotency_key)
                raise
            except asyncio.CancelledError:
                await self._cancel_message_operation(ledger_id, idempotency_key)
                yield {
                    "type": "cancelled",
                    "generation_id": resolved_generation_id,
                    "turns": turns,
                }
                raise
            except Exception as error:
                await self._fail_message_operation(ledger_id, idempotency_key, error)
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
            await self._succeed_message_operation(ledger_id, idempotency_key, result)
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
