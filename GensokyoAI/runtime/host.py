"""进程内 Runtime 宿主：适配器的公共契约，直接驱动 RuntimeService 多租户路径。

适配器（QQ/Discord/任何平台）与 Runtime 同进程时，无需 HTTP/WS 绕路——以网络主体
上下文调用 `RuntimeService.handle()`（与 tests/test_runtime_multi_user.py 相同的
驱动方式），租户隔离（agent_id）、资源闸、幂等账本、revision 乐观锁全部保留；
主动消息经 `create_event_subscription` 返回的 asyncio.Queue 进程内「推送」，
每个租户一个队列，天然隔离，不需要任何帧路由。

这是 `GensokyoAI.adapters.RuntimeAdapter` 协议里 start() 收到的宿主对象；
其方法签名属于适配器公开契约，改动需保持向后兼容。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..core.agent.quota_health import BurnRateSmoother, compute_burn_rate
from ..core.config import ConfigLoader
from ..core.config_schema import AppConfig
from ..utils.logger import logger
from .auth import RuntimePrincipal, reset_current_principal, set_current_principal
from .resource_control import ResourceLimitError
from .rpc import RUNTIME_PROTOCOL_VERSION, RpcError
from .service import RuntimeService


def _package_version() -> str:
    """包版本（editable/源码运行拿不到元数据时回落 dev）。"""
    try:
        return importlib.metadata.version("GensokyoAI")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


class RuntimeRpcError(RuntimeError):
    """宿主调用 Runtime 方法失败的结构化错误，按 code 做稳定分支。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"Runtime 调用错误 [{code}]: {message}")
        self.code = code
        self.details = details or {}


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class RuntimeHost:
    """适配器的进程内 Runtime 宿主（每个外部会话/频道 = 一个 agent_id 租户）。"""

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
        self._meta_session_id: str | None = None  # 元租户（脱稿生成用）的会话 id
        self._started_at = time.monotonic()  # /status 运行时长基准
        # 全局日耗快升慢降平滑器（警戒时间）+ 是否见过成本样本
        # （见过后窗口清零不再回落静态阈值，而是平滑衰减到 0）
        self._cost_smoother = BurnRateSmoother()
        self._cost_has_samples = False
        # 适配器工具模板：(函数, 名称, 是否并行安全)，注入当前及后续租户的工具注册表
        self._adapter_tools: list[tuple[Callable[..., Any], str | None, bool]] = []

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
        self._apply_adapter_tools(agent_id)
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

    async def generate_meta_text(self, character: str, prompt: str) -> str:
        """用元租户（agent_id="nb2-meta"）做一次性脱稿生成（群友印象等）。

        元租户与用户会话完全隔离，生成内容不进任何用户的对话历史；
        会话 id 进程内缓存，revision 每次现取，重复调用不重建 Agent。
        """
        agent_id = "nb2-meta"
        if self._meta_session_id is None:
            session_id, _ = await self.ensure_agent(agent_id, character, disable_initiative=True)
            self._meta_session_id = session_id
        session_id = self._meta_session_id
        revision = await self.fetch_revision(agent_id, session_id)
        content, _ = await self.send_message(
            agent_id,
            session_id,
            revision,
            prompt,
            idempotency_key=f"nb2-meta:{uuid4().hex[:12]}",
        )
        return content

    async def register_adapter_tool(
        self, func: Callable[..., Any], *, name: str | None = None, parallel_safe: bool = False
    ) -> None:
        """把适配器工具注入当前及后续初始化的租户 Agent 工具注册表。

        工具 schema 由函数签名与文档串生成（tools.base 的 `tool()` 约定）；
        注入后下一轮回复即可被模型调用。写状态的工具应传 parallel_safe=False。
        """
        entry = (func, name, parallel_safe)
        if entry not in self._adapter_tools:
            self._adapter_tools.append(entry)
        # 已装配的租户即时生效（访问 service 租户表属 runtime 包内契约）
        for service in self._service._tenant_services.values():
            agent = service.state.agent
            if agent is not None:
                agent.tool_registry.register(func, name=name, parallel_safe=parallel_safe)

    def _tenant_agent(self, agent_id: str) -> Any:
        """取租户当前装配的 Agent（runtime 包内契约），未装配返回 None。"""
        service = self._service._tenant_services.get((self._principal.user_id, agent_id))
        return service.state.agent if service is not None else None

    def _apply_adapter_tools(self, agent_id: str) -> None:
        """把已登记的适配器工具注入刚初始化的租户 Agent。"""
        agent = self._tenant_agent(agent_id)
        if agent is None:
            return
        for func, name, parallel_safe in self._adapter_tools:
            agent.tool_registry.register(func, name=name, parallel_safe=parallel_safe)

    def get_app_config(self) -> AppConfig:
        """读取全局 AppConfig（优先取已装配租户 Agent 的配置，否则按兜底链现加载）。

        供适配器读取全局配置节（如 repeat_guard 复读烦躁阈值）；
        同一进程内所有租户共享同一份全局配置文件。
        """
        for service in self._service._tenant_services.values():
            agent = service.state.agent
            if agent is not None:
                return agent.config
        return ConfigLoader().load(self._service._fallback_config_path())

    def get_system_status(self) -> dict[str, Any]:
        """系统状态快照（nb2 /status 指令）：开户数、在途数、闸门用量、负载水位、延迟。

        闸门用量跨 root 与全部租户服务聚合（各租户各有一套同名闸，
        active/waiting 求和、max_concurrent 求和即系统总容量）。
        延迟统计借元租户的模型客户端（账户级共享 Provider，样本有代表性）；
        元租户未初始化时返回空延迟。
        """
        tenants = {"groups": 0, "users": 0, "meta": 0, "other": 0}
        for _, agent_id in self._service._tenant_services:
            if agent_id.startswith("qq-group-"):
                tenants["groups"] += 1
            elif agent_id.startswith("qq-user-"):
                tenants["users"] += 1
            elif agent_id == "nb2-meta":
                tenants["meta"] += 1
            else:
                tenants["other"] += 1
        # 内心戏样本记在各租户 Agent 的模型客户端上（不是元租户）——全租户聚合
        latency = self._collect_think_latency()

        gates = self._aggregate_gate_usage()
        return {
            "tenants": tenants,
            "active_operations": self._service._active_network_operations,
            "latency": latency,
            "gates": gates,
            "load_level": self._compute_load_level(gates, latency),
            "memory": self._collect_memory_totals(),
            "cost": self._collect_cost_stats(),
            "uptime_seconds": time.monotonic() - self._started_at,
            "version": {"package": _package_version(), "protocol": RUNTIME_PROTOCOL_VERSION},
        }

    def _collect_cost_stats(self) -> dict[str, Any]:
        """全租户消耗速率聚合（元/天），供额度健康动态阈值。

        消耗样本（带时间戳）分散在各租户 Agent 的 ModelClient 上，全局合并
        后由框架 quota_health.compute_burn_rate 统一折算日耗——不按租户
        分开算（用户 2026-08-02 定稿）；速率经 BurnRateSmoother 快升慢降
        （警戒时间：变慢不瞬间拉低阈值，全天静默也平滑衰减而非回落静态）。
        单价未配置（从未见过样本）时永远 {"count": 0}（调用方回落静态阈值）。
        """
        samples: list[tuple[float, float]] = []
        for service in self._service._tenant_services.values():
            agent = service.state.agent
            client = getattr(getattr(agent, "runtime_context", None), "model_client", None)
            if client is None:
                continue
            samples.extend(getattr(client, "_cost_samples", ()))
        stats = compute_burn_rate(samples)
        if stats["count"] == 0 and not self._cost_has_samples:
            return stats  # 从未见过样本：无数据，调用方回落静态阈值
        self._cost_has_samples = True
        raw = stats.get("burn_per_day", 0.0)
        return {
            "count": stats["count"],
            "total_cost": stats.get("total_cost", 0.0),
            "window_hours": stats.get("window_hours", 0.0),
            "raw_burn_per_day": raw,  # 未平滑的原始日耗（观测用）
            "burn_per_day": self._cost_smoother.update(raw),  # 阈值基准
        }

    def _collect_memory_totals(self) -> dict[str, int]:
        """全租户语义记忆规模聚合（话题数 / 记忆条数；未启用语义的租户自然跳过）。"""
        topics = 0
        memories = 0
        for service in self._service._tenant_services.values():
            memory = getattr(service.state.agent, "semantic_memory", None)
            if memory is None:
                continue
            topics += getattr(memory, "topic_count", 0)
            memories += getattr(memory, "memory_count", 0)
        return {"topics": topics, "memories": memories}

    def _collect_think_latency(self) -> dict[str, Any]:
        """聚合全部租户模型客户端的 ThinkEngine 内心戏延迟样本（滚动窗口）。

        内心戏（长期思考/说话前思考/对话欲评估）发生在各租户 Agent 的
        ThinkEngine 上，样本分散在各自 ModelClient；元租户只跑脱稿生成，
        没有内心戏样本。
        """
        samples: list[float] = []
        for service in self._service._tenant_services.values():
            agent = service.state.agent
            client = getattr(getattr(agent, "runtime_context", None), "model_client", None)
            if client is None:
                continue
            samples.extend(
                duration
                for context, duration in getattr(client, "_latency_samples", ())
                if context == "think_engine"
            )
        if not samples:
            return {"count": 0}
        ordered = sorted(samples)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        return {
            "count": len(samples),
            "median_ms": round(median, 1),
            "avg_ms": round(sum(samples) / len(samples), 1),
            "last_ms": round(samples[-1], 1),
            "max_ms": round(max(samples), 1),
        }

    def _aggregate_gate_usage(self) -> list[dict[str, Any]]:
        """汇总资源闸用量。

        runtime 闸只取 root（它是唯一系统入口闸，容量不随租户扩容）；
        其余闸（model/stream/tool…）为每租户一套：active/waiting 求和，
        max_concurrent 保留单实例上限并附 instances 实例数。
        """
        gates_by_name: dict[str, dict[str, Any]] = {}
        services = [self._service, *self._service._tenant_services.values()]
        for service in services:
            for gate in getattr(service, "_resource_gates", {}).values():
                snapshot = gate.snapshot()
                if snapshot["name"] == "runtime":
                    if service is self._service:
                        gates_by_name["runtime"] = {
                            "name": "runtime",
                            "max_concurrent": snapshot["max_concurrent"],
                            "active": snapshot["active"],
                            "waiting": snapshot["waiting"],
                            "instances": 1,
                        }
                    continue
                entry = gates_by_name.setdefault(
                    snapshot["name"],
                    {
                        "name": snapshot["name"],
                        "max_concurrent": snapshot["max_concurrent"],
                        "active": 0,
                        "waiting": 0,
                        "instances": 0,
                    },
                )
                entry["active"] += snapshot["active"]
                entry["waiting"] += snapshot["waiting"]
                entry["instances"] += 1
        return list(gates_by_name.values())

    def _compute_load_level(
        self, gates: list[dict[str, Any]], latency: dict[str, Any]
    ) -> dict[str, str]:
        """负载水位：healthy / warning / critical / unavailable（附一句原因）。

        临界：任一闸门满载或有排队；警告：最高利用率 ≥60% 或思考延迟中位 >15s；
        不可用：Runtime 正在排空（shutdown 进行中）。
        """
        if getattr(self._service, "_draining", False):
            return {"level": "unavailable", "reason": "Runtime 正在排空关闭"}
        worst = 0.0
        queued = 0
        for gate in gates:
            capacity = gate["max_concurrent"] * max(1, gate.get("instances", 1))
            if capacity > 0:
                worst = max(worst, gate["active"] / capacity)
            queued += gate["waiting"]
        if queued > 0 or worst >= 0.9:
            reason = f"闸门利用率最高 {worst:.0%}"
            if queued:
                reason += f"，{queued} 个请求排队中"
            return {"level": "critical", "reason": reason}
        median_ms = latency.get("median_ms", 0)
        if worst >= 0.6:
            return {"level": "warning", "reason": f"闸门利用率最高 {worst:.0%}"}
        if median_ms > 15000:
            return {"level": "warning", "reason": f"思考延迟偏高（中位 {median_ms / 1000:.1f}s）"}
        return {"level": "healthy", "reason": "运行正常"}

    async def get_quota(self, character: str) -> dict[str, Any] | None:
        """查询 Provider 账户额度（账户级；借元租户的模型客户端，不支持返回 None）。"""
        agent_id = "nb2-meta"
        if self._meta_session_id is None:
            session_id, _ = await self.ensure_agent(agent_id, character, disable_initiative=True)
            self._meta_session_id = session_id
        agent = self._tenant_agent(agent_id)
        client = getattr(getattr(agent, "runtime_context", None), "model_client", None)
        if client is None:
            return None
        return await client.get_quota()

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
                # 关闭历史回放（replay_limit=0）：主动消息是「当下想说」，
                # 重启后把历史补发到 QQ 只会刷屏
                replay_limit=0,
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
