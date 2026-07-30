"""进程内 Runtime 宿主：直接驱动 RuntimeService 的多租户路径。

适配器与 Runtime 同仓库同进程，无需 HTTP/WS 绕路——以网络主体上下文调用
`RuntimeService.handle()`（与 tests/test_runtime_multi_user.py 相同的驱动方式），
租户隔离（agent_id）、资源闸、幂等账本、revision 乐观锁全部保留；
主动消息经 `create_event_subscription` 返回的 asyncio.Queue 进程内「推送」，
每个租户一个队列，天然隔离，不需要任何帧路由。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ...runtime.auth import RuntimePrincipal, reset_current_principal, set_current_principal
from ...runtime.resource_control import ResourceLimitError
from ...runtime.rpc import RpcError
from ...runtime.service import RuntimeService
from ...utils.logger import logger


class RuntimeRpcError(RuntimeError):
    """宿主调用 Runtime 方法失败的结构化错误，按 code 做稳定分支。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"Runtime 调用错误 [{code}]: {message}")
        self.code = code
        self.details = details or {}


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class RuntimeHost:
    """nb2 适配器的进程内 Runtime 宿主（每个 QQ 群/私聊 = 一个 agent_id 租户）。"""

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        user_id: str = "nb2",
        service: RuntimeService | None = None,
    ) -> None:
        # service 参数供测试注入预置租户的 RuntimeService；生产留空即可
        self._service = service or (
            RuntimeService(root_dir) if root_dir is not None else RuntimeService()
        )
        self._principal = RuntimePrincipal(
            user_id=user_id,
            roles=frozenset({"read", "chat", "admin"}),
            auth_type="nb2-local",
        )
        self._event_subs: dict[str, tuple[asyncio.Task[None], str]] = {}

    # ==================== 基础调用 ====================

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        token = set_current_principal(self._principal)
        try:
            return await self._service.handle(method, params or {})
        except (ResourceLimitError, RpcError) as error:
            raise self._translate(error) from error
        finally:
            reset_current_principal(token)

    @staticmethod
    def _translate(error: Exception) -> RuntimeRpcError:
        if isinstance(error, ResourceLimitError):
            return RuntimeRpcError(
                "resource.limit_exceeded", str(error), details=error.to_details()
            )
        if isinstance(error, RpcError):
            return RuntimeRpcError(
                str(getattr(error, "code", None) or "unknown"),
                str(error),
                details=getattr(error, "details", None),
            )
        return RuntimeRpcError("internal_error", str(error))

    # ==================== 租户会话 ====================

    async def ensure_agent(
        self,
        agent_id: str,
        character: str,
        session_id: str | None = None,
        *,
        disable_initiative: bool = True,
    ) -> tuple[str, int]:
        """初始化（或恢复）租户 Agent，返回 (session_id, revision)。

        `disable_initiative=True` 时初始化后停用该租户主动定时器——没有主动
        消息投递通道的接入方必须如此，否则角色会产生「看不见的主动发言」，
        既空烧 token 又污染上下文。启用主动投递（subscribe_events）的调用方
        传 False。
        """
        params: dict[str, Any] = {"agent_id": agent_id, "character": character, "start": True}
        if session_id:
            params["session_id"] = session_id
        result = await self._call("agent.init", params)
        session = (result or {}).get("session") or {}
        sid = str(session.get("session_id") or "")
        if not sid:
            raise RuntimeRpcError("agent.init_failed", "agent.init 响应缺少 session_id")
        revision = session.get("revision")
        if revision is None:
            revision = await self.fetch_revision(agent_id, sid)
        if disable_initiative:
            await self._call(
                "initiative_timer.update",
                {"agent_id": agent_id, "session_id": sid, "enabled": False},
            )
        return sid, int(revision)

    async def fetch_revision(self, agent_id: str, session_id: str) -> int:
        """读取会话当前 revision（revision 冲突后刷新重试用）。"""
        result = await self._call(
            "session.messages", {"agent_id": agent_id, "session_id": session_id, "limit": 1}
        )
        return int((result or {}).get("revision") or 0)

    async def send_message(
        self,
        agent_id: str,
        session_id: str,
        revision: int,
        text: str,
        *,
        idempotency_key: str,
        system_contexts: list[str] | None = None,
    ) -> tuple[str, int]:
        """发送一条用户消息，返回 (角色回复, 新 revision)；revision 冲突自动刷新重试一次。

        `system_contexts` 透传 RPC 同名字段：随本轮消息注入的附加上下文
        （如 QQ 聊天风格要求），只影响本轮回复，不写入会话。
        """
        params: dict[str, Any] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "expected_revision": int(revision),
            "idempotency_key": idempotency_key,
            "message": text,
        }
        if system_contexts:
            params["system_contexts"] = list(system_contexts)
        try:
            result = await self._call("agent.send_message", params)
        except RuntimeRpcError as error:
            if error.code != "session.revision_conflict":
                raise
            params["expected_revision"] = await self.fetch_revision(agent_id, session_id)
            result = await self._call("agent.send_message", params)
        content = str((result or {}).get("content") or "")
        new_revision = int(((result or {}).get("session") or {}).get("revision") or revision)
        return content, new_revision

    # ==================== 主动消息事件（进程内队列推送） ====================

    async def subscribe_events(
        self, agent_id: str, on_event: EventCallback, event_types: list[str] | None = None
    ) -> None:
        """订阅租户事件泵；create_event_subscription 直接返回 asyncio.Queue。

        重复订阅同一租户会先停掉旧泵并关闭旧订阅，防止重复投递。
        """
        await self.cancel_events(agent_id)
        token = set_current_principal(self._principal)
        try:
            subscription = await self._service.create_event_subscription(
                event_types=list(event_types or ["message.sent"]),
                agent_id=agent_id,
            )
        except (ResourceLimitError, RpcError) as error:
            raise self._translate(error) from error
        finally:
            reset_current_principal(token)
        queue = subscription["queue"]
        subscription_id = str(subscription["subscription_id"])

        async def pump() -> None:
            while True:
                payload = await queue.get()
                try:
                    await on_event(agent_id, payload)
                except Exception:
                    logger.exception(f"[nb2] 处理事件失败（{agent_id}）")
                finally:
                    queue.task_done()

        self._event_subs[agent_id] = (asyncio.create_task(pump()), subscription_id)

    async def cancel_events(self, agent_id: str) -> None:
        """停掉租户事件泵并关闭服务端订阅；未订阅时静默返回。"""
        entry = self._event_subs.pop(agent_id, None)
        if entry is None:
            return
        task, subscription_id = entry
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        token = set_current_principal(self._principal)
        try:
            await self._service.close_event_subscription(subscription_id)
        except Exception as error:
            logger.debug(f"[nb2] 关闭事件订阅失败（{agent_id}）: {error}")
        finally:
            reset_current_principal(token)

    async def close(self) -> None:
        """停掉全部事件泵并优雅关闭 Runtime（保存所有租户会话）。"""
        for agent_id in list(self._event_subs):
            await self.cancel_events(agent_id)
        await self._service.shutdown()
