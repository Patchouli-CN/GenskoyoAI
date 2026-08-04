"""World 控制台后端 - 多角色动态发言者，主动剧情实时显示"""

# GensokyoAI/backends/console/world_backend.py

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import aioconsole
from msgspec import to_builtins
from rich.console import Console as RichConsole
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...adapters import RuntimeAdapter
from ...commands import (
    CommandContext,
    CommandExecutor,
    CommandResult,
    CommandStatus,
    CommandType,
    command,
)
from ...core.events import Event, EventPriority, SystemEvent
from ...utils.logger import logger
from ...world.types import USER_OCCUPANT_ID
from ...world.world import GensokyoWorld
from ._impl import ART

if TYPE_CHECKING:
    from ...runtime.host import RuntimeHost

# World 模式下不可用的单角色会话命令（World 的存档管理走 /world 族命令）
_AGENT_ONLY_COMMANDS = {"back", "new", "save", "sessions", "history", "timer"}


def _event_dict(data: Any) -> dict[str, Any]:
    """World 事件载荷统一转 dict（载荷是 msgspec Struct 或 dict）。"""
    if isinstance(data, dict):
        return data
    converted = to_builtins(data)
    return converted if isinstance(converted, dict) else {}


class WorldConsoleBackend(RuntimeAdapter):
    """World 控制台后端 - 每个角色用自己的名字发言，世界主动推进实时显示"""

    # /help 中隐藏的单角色专属命令（共享 help 实现读取此属性过滤）
    hidden_command_names = frozenset(_AGENT_ONLY_COMMANDS)

    def __init__(self, world: GensokyoWorld):
        self.world = world
        self._stream_handler: Callable | None = None
        self._running = False
        self._use_stream = True
        self.console = RichConsole()
        self.cmd_executor = CommandExecutor(mode="smart")
        self._cmd_context = CommandContext[WorldConsoleBackend](
            agent=None, backend=self, source="console", issuer="User"
        )

        self.colors = {
            "user": "bold green",
            "assistant": "bold yellow",
            "system": "dim",
            "error": "bold red",
            "success": "bold green",
            "info": "cyan",
            "cmd": "bold cyan",
            "prompt": "bold magenta",
            "initiative": "italic yellow",
        }

        # 发言者前缀是否处于打开状态（流式逐字显示中）
        self._speaker_open = False

        # 是否在等待用户输入（主动输出结束后补回输入提示）
        self._waiting_for_input = False

        # 累积的提示词上下文（/know /meta /attention 共用）
        self._prompt_context: list[str] = []

        # 显示单一通道：用户回合与主动剧情都只经 World 总线事件显示。
        # send 用非流式 send_message 驱动回合，显示完全由下列订阅承担，
        # 不存在 stream/bus 双通道重复问题（turn lock 保证回合不交错）。
        bus = world.event_bus
        bus.subscribe(
            SystemEvent.WORLD_ACTOR_TURN_STARTED,
            self._on_world_actor_started,
            priority=EventPriority.LOW,
        )
        bus.subscribe(
            SystemEvent.WORLD_ACTOR_TURN_CHUNK,
            self._on_world_actor_chunk,
            priority=EventPriority.LOW,
        )
        bus.subscribe(
            SystemEvent.WORLD_ACTOR_TURN_COMPLETED,
            self._on_world_actor_completed,
            priority=EventPriority.LOW,
        )
        bus.subscribe(
            SystemEvent.WORLD_SCENE_MOVED,
            self._on_world_scene_moved,
            priority=EventPriority.LOW,
        )

    # ==================== 命令结果处理 ====================

    def _handle_command_results(self, results: list[CommandResult]) -> bool:
        """处理命令执行结果，返回 True 表示应该退出"""
        for result in results:
            if result.status == CommandStatus.SUCCESS:
                if result.message:
                    self._print_success_message(result.message)
            elif (
                result.status == CommandStatus.FAILURE or result.status == CommandStatus.NO_HANDLER
            ) and result.message:
                self._print_error_message(result.message)

            if result.should_exit:
                self._running = False
                return True

        return False

    # ==================== 打印辅助 ====================

    def _write_speaker_prefix(self, name: str) -> None:
        """打印发言角色名前缀"""
        self.console.print(f"\n[{self.colors['assistant']}]{name}: [/]", end="")

    def _print_system_message(self, message: str, style: str = "system") -> None:
        self.console.print(f"[{self.colors.get(style, style)}]{message}[/]")

    def _print_success_message(self, message: str) -> None:
        self._print_system_message(f"OK: {message}", style="success")

    def _print_error_message(self, message: str) -> None:
        self._print_system_message(f"ERROR: {message}", style="error")

    def _print_info_message(self, message: str) -> None:
        self._print_system_message(f"ℹ {message}", style="info")

    # ==================== 生命周期 ====================

    async def start(self, host: RuntimeHost | None = None) -> None:
        """启动：先显示欢迎面板，再让 World 开场（面板不被开场台词顶上去）"""
        self._show_welcome_panel()
        await self.world.start()
        self._running = True
        logger.info("World 控制台后端已启动")

    async def stop(self) -> None:
        """停止：保存存档并关停 World"""
        self._running = False
        await self.world.shutdown()
        logger.info("World 控制台后端已停止")

    def set_stream_handler(self, handler: Callable | None) -> None:
        self._stream_handler = handler

    def set_stream_mode(self, enabled: bool) -> None:
        self._use_stream = enabled

    def set_color(self, element: str, color: str) -> None:
        if element in self.colors:
            self.colors[element] = color

    def _show_welcome_panel(self) -> None:
        """显示 World 欢迎面板（roster 全员）"""
        snapshot = self.world.state_snapshot()
        names = "、".join(snapshot.roster.values()) or "（空 roster）"

        art_text = Text()
        lines = ART.strip("\n").split("\n")
        for i, line in enumerate(lines):
            if i < 3:
                art_text.append(line + "\n", style="bold red")
            elif i < 5:
                art_text.append(line + "\n", style="bold #FF6666")
            else:
                art_text.append(line + "\n", style="bold white")
        art_text.append(" ☯", style="bold yellow")

        info_text = Text()
        info_text.append("\n")
        info_text.append("✨ 幻想乡 · 多角色世界 ✨\n", style="bold magenta")
        info_text.append("🌸 世界: ", style="dim")
        info_text.append(f"{snapshot.world_id}\n", style="bold cyan")
        info_text.append("🎭 登场角色: ", style="dim")
        info_text.append(f"{names}\n", style="bold cyan")
        if snapshot.session_id:
            info_text.append("💾 存档: ", style="dim")
            info_text.append(f"{snapshot.session_id[:8]}...\n", style="dim")
        info_text.append("─" * 40 + "\n", style="dim")
        info_text.append("⌨️  输入 ", style="dim")
        info_text.append("<cmd>help</cmd> ", style="bold cyan")
        info_text.append("查看所有命令；", style="dim")
        info_text.append("/world /roster /stage /transcript", style="bold cyan")
        info_text.append(" 查看世界状态\n", style="dim")

        full_content = Text()
        full_content.append(art_text)
        full_content.append(info_text)

        self.console.print(
            Panel(
                full_content,
                title="☯ 幻想乡 ☯",
                subtitle="☯ 众神眷恋的幻想乡 ☯",
                border_style="red",
                padding=(1, 2),
            )
        )

    # ==================== World 状态面板 ====================

    def _show_world_panel(self) -> None:
        snapshot = self.world.state_snapshot()
        content = Text()
        content.append("世界: ", style="dim")
        content.append(f"{snapshot.world_id}\n", style="bold cyan")
        content.append("存档: ", style="dim")
        content.append(f"{snapshot.session_id or '（未启用持久化）'}\n", style="white")
        content.append("当前发言: ", style="dim")
        current = snapshot.current_actor_id
        content.append(
            f"{snapshot.roster.get(current, current) if current else '（无）'}\n", style="yellow"
        )
        content.append("等待用户: ", style="dim")
        content.append(f"{snapshot.waiting_for_user}\n", style="green")
        diagnostics = self.world.resume_diagnostics
        if diagnostics:
            content.append("恢复诊断: ", style="dim")
            content.append(f"{len(diagnostics)} 条\n", style="yellow")
            for diagnostic in diagnostics[:5]:
                content.append(f"  • [{diagnostic.severity}] {diagnostic.message}\n", style="dim")
        self.console.print(Panel(content, title="World 状态", border_style="cyan"))

    def _show_roster_panel(self) -> None:
        snapshot = self.world.state_snapshot()
        table = Table(title="登场角色", show_lines=False)
        table.add_column("actor_id", style="bold cyan", no_wrap=True)
        table.add_column("名字", style="white")
        table.add_column("所在场景", style="yellow")
        table.add_column("当前", justify="center")
        for actor_id, name in snapshot.roster.items():
            table.add_row(
                actor_id,
                name,
                snapshot.stage.get(actor_id, "?"),
                "*" if actor_id == snapshot.current_actor_id else "",
            )
        self.console.print(table)

    def _show_stage_panel(self) -> None:
        snapshot = self.world.state_snapshot()
        table = Table(title="舞台", show_lines=False)
        table.add_column("成员", style="bold cyan")
        table.add_column("场景", style="yellow")
        for occupant_id, scene_id in snapshot.stage.items():
            label = (
                "你"
                if occupant_id == USER_OCCUPANT_ID
                else snapshot.roster.get(occupant_id, occupant_id)
            )
            table.add_row(label, scene_id)
        self.console.print(table)

    def _show_transcript_panel(self, limit: int = 20) -> None:
        snapshot = self.world.state_snapshot()
        scene_id = snapshot.stage.get(USER_OCCUPANT_ID)
        entries = self.world.transcript_history(scene_id, limit) if scene_id else []
        table = Table(title=f"共享剧本（{scene_id or '无场景'}）", show_lines=True)
        table.add_column("发言者", style="bold cyan", no_wrap=True)
        table.add_column("内容", style="white")
        for entry in entries:
            speaker = "你" if entry.speaker_id == USER_OCCUPANT_ID else entry.speaker_name or "·"
            content = entry.content.replace("\n", "\\n")
            if len(content) > 160:
                content = content[:157] + "..."
            table.add_row(speaker, content)
        table.caption = f"共 {len(entries)} 条"
        self.console.print(table)

    # ==================== 发送 ====================

    async def send(self, message: str, system_contexts: list[str] | None = None) -> str:
        """发送用户消息并驱动一段自动表演"""
        if not self._running:
            return ""

        stripped = message.strip()
        if not stripped:
            return ""

        # 单角色会话命令在 World 模式不可用，给出友好提示而非内部报错
        first_word = stripped.lstrip("/").split(maxsplit=1)[0].lower()
        if first_word in _AGENT_ONLY_COMMANDS:
            self._print_info_message(
                f"/{first_word} 在 World 模式下不可用（多角色世界没有单一角色会话；"
                "存档管理见 /world 族命令）"
            )
            return ""

        results, clean_text = await self.cmd_executor.execute(message, self._cmd_context)
        if self._handle_command_results(results):
            return "__EXIT__"
        if not clean_text:
            return ""

        system_contexts = system_contexts or self._build_system_contexts()
        return await self._send_world_turn(clean_text, system_contexts)

    def _build_system_contexts(self) -> list[str]:
        """构建系统上下文列表（/know /meta /attention 累积内容，最近 5 条）"""
        if not self._prompt_context:
            return []
        return self._prompt_context[-5:]

    async def _send_world_turn(self, message: str, system_contexts: list[str] | None = None) -> str:
        """驱动一段用户回合；显示完全交给 World 总线订阅（单一通道）。

        流式与非流式共用同一驱动：差异只在 chunk 回调是否逐字打印。
        """
        try:
            turns = await self.world.send_message(message, system_contexts=system_contexts)
        except asyncio.CancelledError:
            logger.debug("World 回合被取消")
            return ""
        except Exception as error:
            logger.error(f"World 回合错误: {error}")
            self._print_error_message(str(error))
            return ""
        return "".join(turn.content for turn in turns)

    # ==================== World 总线事件（唯一显示通道） ====================

    async def _on_world_actor_started(self, event: Event) -> None:
        """回合开始：流式模式下打印发言者前缀"""
        if not self._use_stream:
            return
        data = _event_dict(event.data)
        self._write_speaker_prefix(data.get("actor_name") or "?")
        self._speaker_open = True

    async def _on_world_actor_chunk(self, event: Event) -> None:
        """流式片段逐字显示"""
        if not self._use_stream:
            return
        data = _event_dict(event.data)
        chunk = data.get("content", "")
        if not isinstance(chunk, str) or not chunk:
            return
        if not self._speaker_open:
            # 错过 started 的兜底：以 chunk 的角色名开前缀
            self._write_speaker_prefix(data.get("actor_name") or "?")
            self._speaker_open = True
        self.console.print(chunk, end="", style=self.colors["assistant"])
        if self._stream_handler:
            self._stream_handler(chunk)

    async def _on_world_actor_completed(self, event: Event) -> None:
        """回合完成：流式换行；非流式整段补显；补回输入提示"""
        data = _event_dict(event.data)
        if self._use_stream:
            if self._speaker_open:
                self.console.print()
                self._speaker_open = False
        else:
            content = data.get("content", "")
            if isinstance(content, str) and content.strip():
                self._write_speaker_prefix(data.get("actor_name") or "?")
                self.console.print(content, style=self.colors["assistant"])
                if self._stream_handler:
                    self._stream_handler(content)
        if self._waiting_for_input:
            self.console.print(f"[{self.colors['user']}]你: [/]", end="")

    async def _on_world_scene_moved(self, event: Event) -> None:
        """舞台移动提示（灰色小字）"""
        data = _event_dict(event.data)
        to_scene = data.get("to_scene_id")
        if not to_scene:
            return
        occupant = data.get("occupant_id")
        snapshot = self.world.state_snapshot()
        if occupant == USER_OCCUPANT_ID:
            name = "你"
        elif isinstance(occupant, str):
            name = snapshot.roster.get(occupant) or occupant
        else:
            name = "?"
        self.console.print(f"[dim]（{name}来到了 {to_scene}）[/]")
        if self._waiting_for_input:
            self.console.print(f"[{self.colors['user']}]你: [/]", end="")

    # ==================== 交互式主循环 ====================

    async def run_interactive(self) -> None:
        await self.start()

        self.console.print("[dim]Tip: 输入 [/][bold cyan]<cmd>help</cmd>[/] [dim]查看所有命令[/]")
        self.console.print("[dim]Tip: 按 Ctrl+C 安全退出（会自动保存）[/]\n")

        exited_normally = False

        try:
            while self._running:
                try:
                    self.console.print(f"[{self.colors['user']}]你: [/]", end="")
                    self._waiting_for_input = True
                    user_input = await aioconsole.ainput()
                    self._waiting_for_input = False

                    if not user_input.strip():
                        continue

                    result = await self.send(user_input)

                    if result == "__EXIT__":
                        exited_normally = True
                        break

                except KeyboardInterrupt:
                    self._waiting_for_input = False
                    self.console.print("\n")
                    self._print_system_message("收到中断信号...", style="info")
                    break
                except EOFError:
                    break

        finally:
            if not exited_normally:
                self._print_system_message("正在保存世界数据...", style="info")
            await self.stop()
            self._print_success_message("数据已保存，再见！")


class WorldConsoleBackendBuilder:
    """World 控制台后端构建器 - 链式配置"""

    def __init__(self, world: GensokyoWorld):
        self._backend = WorldConsoleBackend(world)

    def with_stream_mode(self, enabled: bool = True) -> WorldConsoleBackendBuilder:
        self._backend.set_stream_mode(enabled)
        return self

    def with_stream_handler(self, handler: Callable) -> WorldConsoleBackendBuilder:
        self._backend.set_stream_handler(handler)
        return self

    def with_color_theme(self, theme: dict[str, str]) -> WorldConsoleBackendBuilder:
        for element, color in theme.items():
            self._backend.set_color(element, color)
        return self

    def build(self) -> WorldConsoleBackend:
        return self._backend


# ==================== World 命令 ====================


@command(name="world", cmd_type=CommandType.SYSTEM, description="显示 World 状态")
async def cmd_world(ctx: CommandContext) -> CommandResult:
    backend: WorldConsoleBackend = ctx.backend_inst
    if not isinstance(backend, WorldConsoleBackend):
        return CommandResult.failure("world", "当前不是 World 模式")
    backend._show_world_panel()
    return CommandResult.success("world", "World 状态已显示")


@command(name="roster", cmd_type=CommandType.SYSTEM, description="显示登场角色名单")
async def cmd_roster(ctx: CommandContext) -> CommandResult:
    backend: WorldConsoleBackend = ctx.backend_inst
    if not isinstance(backend, WorldConsoleBackend):
        return CommandResult.failure("roster", "当前不是 World 模式")
    backend._show_roster_panel()
    return CommandResult.success("roster", "登场角色已显示")


@command(name="stage", cmd_type=CommandType.SYSTEM, description="显示舞台成员位置")
async def cmd_stage(ctx: CommandContext) -> CommandResult:
    backend: WorldConsoleBackend = ctx.backend_inst
    if not isinstance(backend, WorldConsoleBackend):
        return CommandResult.failure("stage", "当前不是 World 模式")
    backend._show_stage_panel()
    return CommandResult.success("stage", "舞台状态已显示")


@command(
    name="transcript",
    cmd_type=CommandType.SYSTEM,
    description="显示当前场景共享剧本",
    usage="/transcript [条数]",
)
async def cmd_transcript(ctx: CommandContext, cmd=None) -> CommandResult:
    backend: WorldConsoleBackend = ctx.backend_inst
    if not isinstance(backend, WorldConsoleBackend):
        return CommandResult.failure("transcript", "当前不是 World 模式")
    content = (cmd.content if cmd is not None else "").strip()
    limit = int(content) if content.isdigit() else 20
    backend._show_transcript_panel(limit=limit)
    return CommandResult.success("transcript", f"已显示最近 {limit} 条共享剧本")
