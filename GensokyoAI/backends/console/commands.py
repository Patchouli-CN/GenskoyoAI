# GensokyoAI/backends/console/commands.py

"""ConsoleBackend 的命令处理器"""

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...commands import CommandContext, CommandResult, CommandType, command
from ...utils.formatters import format_session_id
from ...utils.helpers import safe_get

if TYPE_CHECKING:
    from ._impl import ConsoleBackend


# ==================== 系统命令 ====================


@command(name="exit", cmd_type=CommandType.SYSTEM, aliases=["quit"], description="退出程序")
async def cmd_exit(ctx: CommandContext) -> CommandResult:
    """退出程序"""
    backend: ConsoleBackend = ctx.backend_inst
    backend._print_system_message("正在保存数据，再见！", style="info")
    backend._running = False
    return CommandResult.exit()


@command(name="back", cmd_type=CommandType.SYSTEM, description="回滚上一轮对话")
async def cmd_back(ctx: CommandContext) -> CommandResult:
    """回滚对话"""
    ctx.agent_inst.rollback(1)
    return CommandResult.success("back", "已回滚上一轮对话")


@command(name="new", cmd_type=CommandType.SYSTEM, description="创建新会话")
async def cmd_new(ctx: CommandContext) -> CommandResult:
    """创建新会话"""
    backend: ConsoleBackend = ctx.backend_inst
    session = ctx.agent_inst.create_session()
    backend._prompt_context.clear()
    session_id_short = format_session_id(session.session_id)

    if greeting := safe_get(ctx.agent_inst.config, "character.greeting"):
        backend._print_assistant_message(greeting)

    return CommandResult.success("new", f"已创建新会话: {session_id_short}")


@command(name="save", cmd_type=CommandType.SYSTEM, description="保存当前会话")
async def cmd_save(ctx: CommandContext) -> CommandResult:
    """保存会话"""
    # 强制同步保存
    await ctx.agent_inst.save_coordinator.save_async(ctx.agent_inst.working_memory, force=True)
    return CommandResult.success("save", "会话已保存")


@command(name="sessions", cmd_type=CommandType.SYSTEM, description="列出历史会话")
async def cmd_sessions(ctx: CommandContext) -> CommandResult:
    """列出会话"""
    backend: ConsoleBackend = ctx.backend_inst
    sessions = ctx.agent_inst.session_manager.list_sessions()
    backend._show_sessions_panel(sessions)
    return CommandResult.success("sessions", f"共 {len(sessions)} 个历史会话")


@command(name="stream", cmd_type=CommandType.SYSTEM, description="切换流式输出")
async def cmd_stream(ctx: CommandContext, mode: str = "toggle") -> CommandResult:
    """切换流式输出"""
    backend: ConsoleBackend = ctx.backend_inst

    if mode in ("on", "true", "1", "enable"):
        backend._use_stream = True
        return CommandResult.success("stream", "流式输出已开启")
    elif mode in ("off", "false", "0", "disable"):
        backend._use_stream = False
        return CommandResult.success("stream", "流式输出已关闭")
    elif mode == "toggle":
        backend._use_stream = not backend._use_stream
        status = "开启" if backend._use_stream else "关闭"
        return CommandResult.success("stream", f"流式输出已切换为: {status}")
    else:
        status = "开启" if backend._use_stream else "关闭"
        return CommandResult.success("stream", f"流式输出当前: {status}")


@command(name="clear", cmd_type=CommandType.SYSTEM, description="清空提示词上下文")
async def cmd_clear(ctx: CommandContext) -> CommandResult:
    """清空提示词上下文"""
    backend: ConsoleBackend = ctx.backend_inst
    count = len(backend._prompt_context)
    backend._prompt_context.clear()
    return CommandResult.success("clear", f"已清空 {count} 条提示词上下文")


@command(name="errors", cmd_type=CommandType.SYSTEM, description="查看最近错误")
async def cmd_errors(ctx: CommandContext) -> CommandResult:
    """查看系统错误状态"""
    backend: ConsoleBackend = ctx.backend_inst
    agent = ctx.agent_inst

    backend.console.print("[bold red]📊 错误统计[/]")

    if hasattr(agent, "error_listeners"):
        stats = agent.error_listeners.get_error_stats()
        backend.console.print(f"总错误数: {stats['total']}")

        if stats["counts"]:
            backend.console.print("\n[bold]按类型:[/]")
            for key, count in stats["counts"].items():
                backend.console.print(f"  • {key}: {count}")
        else:
            backend.console.print("  [dim]暂无错误记录[/]")

        if stats["recent"]:
            backend.console.print("\n[bold]最近错误:[/]")
            for err in stats["recent"][-5:]:
                ts = err["timestamp"].strftime("%H:%M:%S")
                status = f"[{err.get('status_code', 'N/A')}]" if err.get("status_code") else ""
                error_preview = (
                    err["error"][:50] + "..." if len(err["error"]) > 50 else err["error"]
                )
                backend.console.print(f"  • {ts} {status} {error_preview}")
    else:
        backend.console.print("[dim]错误监听器未初始化[/]")

    # 显示 EventBus 状态
    if hasattr(agent, "event_bus"):
        stats = agent.event_bus.stats
        backend.console.print("\n[bold]EventBus 状态:[/]")
        backend.console.print(
            f"  已发布: {stats['published']}, 已投递: {stats['delivered']}, 错误: {stats['errors']}"
        )

    return CommandResult.success("errors", "错误已显示")


@command(name="help", cmd_type=CommandType.SYSTEM, description="显示帮助信息")
async def cmd_help(ctx: CommandContext) -> CommandResult:
    """显示帮助"""
    backend: ConsoleBackend = ctx.backend_inst

    commands = backend.cmd_executor.list_commands()
    # World 等模式可声明本模式不可用的命令，帮助里不再展示（避免误导）
    hidden = frozenset(getattr(backend, "hidden_command_names", ()))

    help_text = """
[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]
[bold cyan]  可用命令列表[/]
[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]

[bold yellow]系统命令:[/]
"""
    for cmd in commands:
        if cmd.type == CommandType.SYSTEM and cmd.name not in hidden:
            aliases = f" ({', '.join(cmd.aliases)})" if cmd.aliases else ""
            help_text += f"  • <cmd>{cmd.name}</cmd>, /{cmd.name}{aliases} - {cmd.description}\n"
            help_text += f"    [dim]用法: {cmd.usage}[/]\n"

    help_text += "\n[bold magenta]提示词命令 (会传递给 AI):[/]\n"
    for cmd in commands:
        if cmd.type == CommandType.PROMPT:
            aliases = f" ({', '.join(cmd.aliases)})" if cmd.aliases else ""
            help_text += f"  • <{cmd.name}>内容</{cmd.name}>{aliases} - {cmd.description}\n"

    help_text += "\n[bold green]聊天命令 (仅本地显示):[/]\n"
    for cmd in commands:
        if cmd.type == CommandType.CHAT:
            help_text += f"  • <{cmd.name}>内容</{cmd.name}> - {cmd.description}\n"

    help_text += f"\n[dim]当前提示词上下文: {len(backend._prompt_context)} 条[/]"
    help_text += "\n[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]"

    backend.console.print(help_text)

    return CommandResult.success("help", "帮助信息已显示")


# ==================== 主动定时器命令 ====================


def _split_command_content(content: str) -> list[str]:
    """按 shell 风格拆分命令内容，并保留 Windows 路径反斜杠。"""
    try:
        parts = shlex.split(content, posix=False)
    except ValueError:
        parts = content.split()
    return [
        part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"} else part
        for part in parts
    ]


def _parse_timer_update_args(args: list[str]) -> tuple[int | None, str | None]:
    delay_seconds: int | None = None
    due_at: str | None = None

    if not args:
        raise ValueError("用法: /timer update delay <秒数> 或 /timer update due <ISO时间>")

    key = args[0].lower()
    value = " ".join(args[1:]).strip()
    if "=" in args[0]:
        key, value = args[0].split("=", 1)
        key = key.lower().strip()
        value = value.strip()
    if not value:
        raise ValueError("缺少要更新的值")

    if key in {"delay", "delay_seconds", "seconds", "sec"}:
        delay_seconds = int(value)
        if delay_seconds < 1:
            raise ValueError("delay_seconds 必须大于 0")
    elif key in {"due", "due_at", "time", "at"}:
        due_at = value
    else:
        raise ValueError("支持字段: delay/delay_seconds 或 due/due_at")

    return delay_seconds, due_at


@command(
    name="timer",
    cmd_type=CommandType.SYSTEM,
    aliases=["initiative", "itimer"],
    description="管理 AI 主动定时器",
    usage="/timer [update|summary|cancel|trigger] ...",
)
async def cmd_timer(ctx: CommandContext, cmd=None) -> CommandResult:
    """查看、修改、取消或立即触发主动定时器。"""
    backend: ConsoleBackend = ctx.backend_inst
    agent = ctx.agent_inst
    content = (cmd.content if cmd is not None else "").strip()
    parts = _split_command_content(content)
    action = parts[0].lower() if parts else "show"
    args = parts[1:]

    if action in {"show", "current", "status", "list"}:
        timer = agent.current_initiative_timer()
        backend._show_initiative_timer_panel(timer)
        if timer is None:
            return CommandResult.success("timer", "当前没有主动定时器")
        return CommandResult.success("timer", "主动定时器已显示")

    if action == "update":
        delay_seconds, due_at = _parse_timer_update_args(args)
        timer = await agent.update_initiative_timer(delay_seconds=delay_seconds, due_at=due_at)
        backend._show_initiative_timer_panel(timer)
        return CommandResult.success("timer", "主动定时器已更新")

    if action in {"summary", "edit", "set-summary"}:
        summary = " ".join(args).strip()
        if not summary:
            raise ValueError("用法: /timer summary <新的 pending_summary>")
        timer = await agent.update_initiative_timer(pending_summary=summary)
        backend._show_initiative_timer_panel(timer)
        return CommandResult.success("timer", "主动定时器摘要已更新")

    if action == "cancel":
        reason = " ".join(args).strip() or "console_cancelled"
        timer = await agent.cancel_initiative_timer(reason=reason)
        backend._show_initiative_timer_panel(timer)
        return CommandResult.success("timer", "主动定时器已取消")

    if action in {"trigger", "fire", "now"}:
        result = await agent.trigger_initiative_timer()
        backend._show_initiative_trigger_result(result)
        return CommandResult.success("timer", "主动定时器已立即触发")

    raise ValueError(
        "未知 timer 子命令。可用: /timer, /timer update delay <秒数>, "
        "/timer update due <ISO时间>, /timer summary <摘要>, /timer cancel, /timer trigger"
    )


# ==================== 历史消息编辑命令 ====================


_ALLOWED_HISTORY_ROLES = {"system", "user", "assistant", "tool"}


def _current_session_and_messages(ctx: CommandContext) -> tuple[Any, str, list[dict[str, Any]]]:
    manager = ctx.agent_inst.session_manager
    session = manager.get_current_session()
    if session is None:
        raise ValueError("当前没有活动会话")
    messages = manager.persistence.load_messages(session.session_id)
    return session, session.session_id, [dict(message) for message in messages]


def _normalize_history_messages(messages: Any) -> list[dict[str, Any]]:
    if isinstance(messages, dict):
        messages = messages.get("messages")
    if not isinstance(messages, list):
        raise ValueError("历史文件必须是消息数组，或包含 messages 数组的对象")

    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"第 {index} 条消息不是对象")
        role = message.get("role")
        content = message.get("content")
        if role not in _ALLOWED_HISTORY_ROLES:
            raise ValueError(f"第 {index} 条消息 role 不合法: {role}")
        if not isinstance(content, str):
            raise ValueError(f"第 {index} 条消息 content 必须是字符串")
        normalized.append(dict(message))
    return normalized


def _replace_current_session_messages(ctx: CommandContext, messages: list[dict[str, Any]]) -> None:
    manager = ctx.agent_inst.session_manager
    session = manager.get_current_session()
    if session is None:
        raise ValueError("当前没有活动会话")
    if not manager.replace_messages(session.session_id, _normalize_history_messages(messages)):
        raise ValueError(f"会话不存在: {session.session_id}")


def _history_export_path(session_id: str, raw_path: str | None = None) -> Path:
    if raw_path:
        return Path(raw_path).expanduser()
    return Path(f"session_history_{format_session_id(session_id)}.json")


@command(
    name="history",
    cmd_type=CommandType.SYSTEM,
    aliases=["hist", "messages"],
    description="查看和编辑当前会话历史消息",
    usage="/history [export|import|delete|insert|regen] ...",
)
async def cmd_history(ctx: CommandContext, cmd=None) -> CommandResult:
    """查看、导出、导入、删除、插入历史消息，或从指定位置重生成。"""
    backend: ConsoleBackend = ctx.backend_inst
    content = (cmd.content if cmd is not None else "").strip()
    parts = _split_command_content(content)
    action = parts[0].lower() if parts else "show"
    args = parts[1:]

    session, session_id, messages = _current_session_and_messages(ctx)

    if action in {"show", "list", "view"}:
        limit = int(args[0]) if args else 20
        backend._show_history_messages_panel(messages, session=session, limit=limit)
        return CommandResult.success(
            "history", f"已显示 {min(limit, len(messages))}/{len(messages)} 条历史消息"
        )

    if action == "export":
        path = _history_export_path(session_id, args[0] if args else None)
        payload = {
            "session_id": session_id,
            "message_count": len(messages),
            "messages": messages,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        backend._show_history_file_hint(
            path, "历史消息已导出，可编辑 messages 后用 /history import 导入"
        )
        return CommandResult.success("history", f"已导出 {len(messages)} 条消息到 {path}")

    if action == "import":
        if not args:
            raise ValueError("用法: /history import <json文件路径>")
        path = Path(args[0]).expanduser()
        data = json.loads(path.read_text(encoding="utf-8"))
        normalized = _normalize_history_messages(data)
        _replace_current_session_messages(ctx, normalized)
        backend._show_history_messages_panel(normalized, session=session, limit=20)
        return CommandResult.success("history", f"已从 {path} 导入并替换 {len(normalized)} 条消息")

    if action == "delete":
        if not args:
            raise ValueError("用法: /history delete <消息索引>")
        index = int(args[0])
        if index < 0 or index >= len(messages):
            raise ValueError("消息索引超出范围")
        removed = messages.pop(index)
        _replace_current_session_messages(ctx, messages)
        backend._show_history_messages_panel(messages, session=session, limit=20)
        return CommandResult.success("history", f"已删除 #{index} {removed.get('role', '?')} 消息")

    if action == "insert":
        if len(args) < 3:
            raise ValueError("用法: /history insert <索引> <role> <content>")
        index = int(args[0])
        role = args[1]
        insert_content = " ".join(args[2:])
        if role not in _ALLOWED_HISTORY_ROLES:
            raise ValueError(f"role 必须是: {', '.join(sorted(_ALLOWED_HISTORY_ROLES))}")
        if index < 0 or index > len(messages):
            raise ValueError("插入索引超出范围")
        messages.insert(index, {"role": role, "content": insert_content})
        _replace_current_session_messages(ctx, messages)
        backend._show_history_messages_panel(messages, session=session, limit=20)
        return CommandResult.success("history", f"已在 #{index} 插入 {role} 消息")

    if action in {"regen", "regenerate"}:
        if not args:
            raise ValueError("用法: /history regen <消息索引>")
        index = int(args[0])
        if index < 0 or index >= len(messages):
            raise ValueError("消息索引超出范围")
        user_index = None
        for pos in range(index, -1, -1):
            if messages[pos].get("role") == "user":
                user_index = pos
                break
        if user_index is None:
            raise ValueError("指定位置之前没有 user 消息，无法重生成")
        user_content = messages[user_index].get("content")
        if not isinstance(user_content, str) or not user_content:
            raise ValueError("目标 user 消息内容为空，无法重生成")
        _replace_current_session_messages(ctx, messages[:user_index])
        response = await ctx.agent_inst.send(user_content, backend._build_system_contexts())
        new_messages = ctx.agent_inst.session_manager.persistence.load_messages(session_id)
        response_content = response.content if response else ""
        content = response_content if isinstance(response_content, str) else str(response_content)
        backend._show_regenerated_message(content)
        backend._show_history_messages_panel(new_messages, session=session, limit=20)
        return CommandResult.success("history", f"已从 #{user_index} 重新生成")

    raise ValueError(
        "未知 history 子命令。可用: /history, /history export [path], /history import <path>, "
        "/history delete <index>, /history insert <index> <role> <content>, /history regen <index>"
    )


# ==================== 聊天命令 ====================

_CHAT_ICONS = {
    "think": "💭",
    "whisper": "🤫 悄悄话:",
    "ooc": "🎭 OOC:",
    "describe": "📖",
    "action": "⚡",
}

_CHAT_COLORS = {
    "think": "dim italic",
    "whisper": "dim",
    "ooc": "yellow",
    "describe": "cyan",
    "action": "green",
}


def _make_chat_handler(cmd_name: str):
    @command(name=cmd_name, cmd_type=CommandType.CHAT, description=f"{cmd_name} 命令")
    async def handler(ctx: CommandContext, content: str = "") -> CommandResult:
        if content:
            backend: ConsoleBackend = ctx.backend_inst
            icon = _CHAT_ICONS.get(cmd_name, "")
            color = _CHAT_COLORS.get(cmd_name, "white")
            backend.console.print(f"[{color}]{icon} {content}[/]")
        return CommandResult.success(cmd_name)

    return handler


# 注册聊天命令
cmd_think = _make_chat_handler("think")
cmd_whisper = _make_chat_handler("whisper")
cmd_ooc = _make_chat_handler("ooc")
cmd_describe = _make_chat_handler("describe")
cmd_action = _make_chat_handler("action")


# ==================== 提示词命令 ====================

_PROMPT_PREFIXES = {
    "know": "【参考资料】",
    "meta": "【场景设定】",
    "attention": "【重要提醒】",
}

_PROMPT_ICONS = {
    "know": "📚",
    "meta": "🎬",
    "attention": "⚠️",
}


def _make_prompt_handler(cmd_name: str):
    aliases_map = {
        "know": ["knowledge"],
        "meta": ["metadata"],
        "attention": ["tips"],
    }

    @command(
        name=cmd_name,
        cmd_type=CommandType.PROMPT,
        aliases=aliases_map.get(cmd_name, []),
        description=f"提供{cmd_name}信息给 AI",
    )
    async def handler(ctx: CommandContext, content: str = "") -> CommandResult:
        if content:
            backend: ConsoleBackend = ctx.backend_inst
            prefix = _PROMPT_PREFIXES.get(cmd_name, "")
            icon = _PROMPT_ICONS.get(cmd_name, "")
            backend._prompt_context.append(f"{prefix}\n{content}")

            preview = content[:100] + ("..." if len(content) > 100 else "")
            backend.console.print(f"[dim]{icon} {preview}[/]")

        return CommandResult.success(cmd_name, f"已添加{cmd_name}上下文 ({len(content)} 字符)")

    return handler


# 注册提示词命令
cmd_know = _make_prompt_handler("know")
cmd_meta = _make_prompt_handler("meta")
cmd_attention = _make_prompt_handler("attention")
