"""nb2 bot 指令：框架 commands 体系注册（本地注册表 + 四级权限）。

指令解析、执行、权限闸门与 Minecraft 风格执行日志统一走框架
`CommandExecutor`（见 plugin._dispatch_command）；本模块只声明指令本身。
本地注册表 NB2_COMMANDS 不写入框架全局注册表——避免与 console 的同名
指令（如 /help）在共享进程里互相覆盖；新增指令 = 加一个 @command 函数。

handler 约定：签名带 `ctx`（框架 CommandContext）；QQ 侧依赖（宿主/配置/
发消息回调）经 ctx.metadata 传入；返回 CommandResult（message 进执行日志，
QQ 回复由 handler 自己经 send 回调发送）。
"""

from __future__ import annotations

import time
from typing import Any

from ...commands import (
    CommandContext,
    CommandDefinition,
    CommandResult,
    PermissionLevel,
    command,
)
from ...core.agent.quota_health import QuotaHealth, QuotaLevel, compute_quota_health

# nb2 指令本地注册表（CommandExecutor(registry=...) 消费）
NB2_COMMANDS: dict[str, CommandDefinition] = {}


def resolve_level(
    user_qq: int | None, owner_qq: frozenset[int], member_role: str | None
) -> PermissionLevel:
    """QQ 身份 → 四级权限。

    `member_role`：QQ 群成员角色（owner/admin/member）；私聊由调用方传 "member"；
    传 None 表示无法核实身份，落到 VISITOR。
    """
    if user_qq is not None and user_qq in owner_qq:
        return PermissionLevel.OWNER
    if member_role in {"owner", "admin"}:
        return PermissionLevel.ADMIN
    if member_role == "member":
        return PermissionLevel.USER
    return PermissionLevel.VISITOR


def _format_quota(data: dict[str, Any] | None) -> str:
    """格式化额度信息为单行文本（Moonshot 形态优先，其余原样展示）。"""
    if data is None:
        return "当前 Provider 不支持额度查询。"
    available = data.get("available_balance")
    if available is not None:
        details = []
        for label, key in (("现金", "cash_balance"), ("代金券", "voucher_balance")):
            value = data.get(key)
            if isinstance(value, int | float):
                details.append(f"{label} ¥{value:.2f}")
        suffix = f"（{'，'.join(details)}）" if details else ""
        shown = f"¥{available:.2f}" if isinstance(available, int | float) else str(available)
        return f"当前额度：{shown}{suffix}"
    return f"额度信息：{data}"


@command(
    name="help",
    aliases=["帮助"],
    description="显示可用指令列表",
    permission=PermissionLevel.VISITOR,
    registry=NB2_COMMANDS,
)
async def cmd_help(ctx: CommandContext) -> CommandResult:
    visible = [cmd for cmd in _unique_commands() if cmd.permission <= ctx.permission]
    lines = ["可用指令："]
    for cmd in visible:
        names = " / ".join(f"/{alias}" for alias in (cmd.name, *cmd.aliases))
        lines.append(f"{names} - {cmd.description}")
    await ctx.metadata["send"]("\n".join(lines))
    return CommandResult.success("help", f"列出 {len(visible)} 条可用指令")


@command(
    name="quota",
    aliases=["额度"],
    description="查询当前 Provider 账户额度",
    permission=PermissionLevel.USER,
    registry=NB2_COMMANDS,
)
async def cmd_quota(ctx: CommandContext) -> CommandResult:
    host = ctx.metadata["host"]
    try:
        data = await host.get_quota(ctx.metadata["config"].character)
    except Exception:
        await ctx.metadata["send"]("额度查询失败了……稍后再试吧。")
        return CommandResult.failure("quota", "Provider 额度接口异常")
    text = _format_quota(data)
    await ctx.metadata["send"](text)
    return CommandResult.success("quota", text)


_LOAD_LEVEL_DISPLAY = {
    "healthy": "🟢 健康",
    "warning": "🟡 警告",
    "critical": "🔴 临界",
    "unavailable": "⚫ 不可用",
}

# 额度查询缓存：/status 是全员指令，不每次都打余额 API
_quota_cache: tuple[float, dict[str, Any] | None] | None = None
_QUOTA_CACHE_TTL_SECONDS = 300.0


async def _get_quota_cached(host: Any, character: str) -> dict[str, Any] | None:
    """额度查询（5 分钟缓存）；查询失败返回 None，不阻断 /status 主体。"""
    global _quota_cache
    now = time.monotonic()
    if _quota_cache is not None and now - _quota_cache[0] < _QUOTA_CACHE_TTL_SECONDS:
        return _quota_cache[1]
    try:
        data = await host.get_quota(character)
    except Exception:
        data = None
    _quota_cache = (now, data)
    return data


def _format_uptime(seconds: float) -> str:
    """运行时长人性化：天/小时/分钟取最高两级。"""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分钟"


def _format_quota_health(data: dict[str, Any] | None, *, warn: float, crit: float) -> str:
    """额度健康行（静态阈值回落路径）：无消耗样本时使用 env 阈值。

    动态路径（有消耗中位数）见 _format_quota_dynamic——阈值由框架
    quota_health 按「余额还能撑多少次典型调用」统一计算。
    """
    if data is None:
        return "额度：暂不可用（Provider 不支持或查询失败）"
    available = data.get("available_balance")
    if not isinstance(available, int | float):
        return f"额度信息：{data}"
    if available <= 0:
        return f"额度：🟣 耗尽（余额 ¥{available:.2f}）"
    index = min(100, max(0, round(available / warn * 100))) if warn > 0 else 100
    emoji = "🟢" if available >= warn else ("🟡" if available >= crit else "🔴")
    details = []
    for label, key in (("现金", "cash_balance"), ("代金券", "voucher_balance")):
        value = data.get(key)
        if isinstance(value, int | float):
            details.append(f"{label} ¥{value:.2f}")
    suffix = f"（{'，'.join(details)}）" if details else ""
    return f"额度：{emoji} 健康指数 {index}（余额 ¥{available:.2f}{suffix}）"


_QUOTA_LEVEL_DISPLAY = {
    QuotaLevel.HEALTHY: "🟢",
    QuotaLevel.WARNING: "🟡",
    QuotaLevel.CRITICAL: "🔴",
    QuotaLevel.DEPLETED: "🟣",
}


def _format_quota_dynamic(health: QuotaHealth) -> str:
    """额度健康行（动态阈值路径）：阈值 = 消耗中位成本 × 基准调用次数。"""
    emoji = _QUOTA_LEVEL_DISPLAY[health.level]
    return (
        f"额度：{emoji} 健康指数 {health.index}"
        f"（余额 ¥{health.balance:.2f}，中位单次 ¥{health.median_cost:.4f}，"
        f"约可再聊 {health.remaining_calls:.0f} 次）"
    )


def _format_status(
    status: dict[str, Any],
    *,
    quota: dict[str, Any] | None = None,
    quota_fetched: bool = False,
    quota_warn: float = 20.0,
    quota_crit: float = 5.0,
    repeat_guard: Any = None,
) -> str:
    """格式化系统状态（负载水位 / 额度 / 开户数 / 处理中 / 思考延迟 / 闸门 / 复读防护 / 记忆 / 版本）。"""
    level = status.get("load_level") or {}
    level_text = _LOAD_LEVEL_DISPLAY.get(level.get("level", ""), level.get("level", "未知"))
    lines = [f"系统状态：{level_text}（{level.get('reason', '—')}）"]

    if quota_fetched:
        # 动态阈值优先：有消耗中位数时按「余额还能撑多少次典型调用」算；
        # 无样本（单价未配置/尚无调用）回落 env 静态阈值
        balance = (quota or {}).get("available_balance")
        median_cost = (status.get("cost") or {}).get("median_cost")
        health = (
            compute_quota_health(balance, median_cost)
            if isinstance(balance, int | float) and median_cost
            else None
        )
        if health is not None:
            lines.append(_format_quota_dynamic(health))
        else:
            lines.append(_format_quota_health(quota, warn=quota_warn, crit=quota_crit))

    tenants = status["tenants"]
    # 元租户（印象/判定等后台设施）不计入会话数，避免「私聊 1 个却显示 2」的误解
    total = tenants["groups"] + tenants["users"] + tenants["other"]
    lines.append(f"开户：{tenants['groups']} 群 / {tenants['users']} 私聊（共 {total} 个会话租户）")
    lines.append(f"处理中：{status['active_operations']} 个会话正在生成")

    # 闸门用量：runtime（root 入口闸）常驻，其余只显示非空闲的（active/waiting > 0）
    gate_parts = []
    for gate in status.get("gates", []):
        if gate["name"] == "runtime" or gate["active"] or gate["waiting"]:
            waiting = f"（排队 {gate['waiting']}）" if gate["waiting"] else ""
            instances = gate.get("instances", 1)
            capacity = (
                f"{gate['max_concurrent']}×{instances}"
                if instances > 1
                else str(gate["max_concurrent"])
            )
            gate_parts.append(f"{gate['name']} {gate['active']}/{capacity}{waiting}")
    lines.append(f"闸门：{' · '.join(gate_parts) if gate_parts else '全部空闲'}")

    latency = status.get("latency") or {}
    if latency.get("count"):
        lines.append(
            f"思考延迟：中位 {latency['median_ms'] / 1000:.1f}s"
            f"（近 {latency['count']} 次内心思考，峰值 {latency['max_ms'] / 1000:.1f}s）"
        )
    else:
        lines.append("思考延迟：预计中…")

    if repeat_guard is not None:
        guard_stats = repeat_guard.stats()
        if guard_stats["muted"] or guard_stats["watching"]:
            lines.append(
                f"复读防护：{guard_stats['muted']} 人冷却中 · {guard_stats['watching']} 人观察中"
            )
        else:
            lines.append("复读防护：全员平静")

    memory = status.get("memory") or {}
    if "topics" in memory:
        lines.append(f"记忆：{memory['topics']} 个话题 / {memory.get('memories', 0)} 条珍贵记忆")

    version = status.get("version") or {}
    uptime_seconds = status.get("uptime_seconds")
    if version or uptime_seconds is not None:
        parts = []
        if version:
            parts.append(f"v{version.get('package', '?')}（协议 {version.get('protocol', '?')}）")
        if uptime_seconds is not None:
            parts.append(f"已运行 {_format_uptime(float(uptime_seconds))}")
        lines.append(f"版本：{' · '.join(parts)}")
    return "\n".join(lines)


@command(
    name="status",
    aliases=["状态"],
    description="查看系统状态（开户数/处理中/思考延迟）",
    permission=PermissionLevel.USER,
    registry=NB2_COMMANDS,
)
async def cmd_status(ctx: CommandContext) -> CommandResult:
    host = ctx.metadata["host"]
    try:
        status = host.get_system_status()
        quota = await _get_quota_cached(host, ctx.metadata["config"].character)
    except Exception:
        await ctx.metadata["send"]("状态查询失败了……稍后再试吧。")
        return CommandResult.failure("status", "系统状态读取异常")
    config = ctx.metadata["config"]
    text = _format_status(
        status,
        quota=quota,
        quota_fetched=True,
        quota_warn=getattr(config, "quota_warn_yuan", 20.0),
        quota_crit=getattr(config, "quota_crit_yuan", 5.0),
        repeat_guard=ctx.metadata.get("repeat_guard"),
    )
    await ctx.metadata["send"](text)
    return CommandResult.success("status", "ok")


def _unique_commands() -> list[CommandDefinition]:
    """本地注册表去重（别名共享同一 CommandDefinition），按注册顺序。"""
    seen: set[str] = set()
    result = []
    for cmd in NB2_COMMANDS.values():
        if cmd.name not in seen:
            seen.add(cmd.name)
            result.append(cmd)
    return result
