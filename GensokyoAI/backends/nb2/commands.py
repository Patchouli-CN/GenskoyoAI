"""nb2 bot 指令系统：四级权限模型与指令注册表。

nonebot 无关的纯逻辑层（可单测）：指令以 BotCommand 注册，声明所需权限级；
插件层只负责取消息、解析群成员角色，然后按 `can_execute` 放行。
新增指令 = 在 COMMANDS 里加一行。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from ...runtime.host import RuntimeHost


class PermissionLevel(IntEnum):
    """四级权限：数值越大权限越高；指令放行要求 用户等级 >= 指令等级。"""

    VISITOR = 0  # 无法核实身份（名片查询失败）——最低信任级
    USER = 1  # 普通群成员 / 私聊用户
    ADMIN = 2  # QQ 群管理 / 群主
    OWNER = 3  # bot 主人（GSK_NB2_OWNER_QQ）


def resolve_level(
    user_qq: int | None, owner_qq: frozenset[int], member_role: str | None
) -> PermissionLevel:
    """解析用户权限等级。

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


@dataclass(frozen=True)
class CommandContext:
    """指令执行上下文：宿主、配置、调用者与发消息回调。"""

    host: RuntimeHost
    config: Any
    member_qq: int | None
    level: PermissionLevel
    send: Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class BotCommand:
    """一条 bot 指令：名称、所需权限、说明（/help 展示）、处理器、别名。"""

    name: str
    permission: PermissionLevel
    description: str
    handler: Callable[[CommandContext], Awaitable[None]]
    aliases: tuple[str, ...] = ()


def can_execute(command: BotCommand, level: PermissionLevel) -> bool:
    """权限判定：用户等级 >= 指令所需等级。"""
    return level >= command.permission


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


async def _handle_quota(ctx: CommandContext) -> None:
    try:
        data = await ctx.host.get_quota(ctx.config.character)
    except Exception:
        await ctx.send("额度查询失败了……稍后再试吧。")
        return
    await ctx.send(_format_quota(data))


async def _handle_help(ctx: CommandContext) -> None:
    lines = ["可用指令："]
    for command in COMMANDS:
        if not can_execute(command, ctx.level):
            continue
        names = " / ".join(f"/{alias}" for alias in (command.name, *command.aliases))
        lines.append(f"{names} - {command.description}")
    await ctx.send("\n".join(lines))


COMMANDS: tuple[BotCommand, ...] = (
    BotCommand("help", PermissionLevel.VISITOR, "显示可用指令列表", _handle_help, aliases=("帮助",)),
    BotCommand(
        "quota", PermissionLevel.USER, "查询当前 Provider 账户额度", _handle_quota, aliases=("额度",)
    ),
)

_ALIAS_INDEX: dict[str, BotCommand] = {
    alias: command for command in COMMANDS for alias in (command.name, *command.aliases)
}


def find_command(name: str) -> BotCommand | None:
    """按名称或别名查指令；未注册返回 None。"""
    return _ALIAS_INDEX.get(name.lower())
