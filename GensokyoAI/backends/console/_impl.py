"""控制台后端 - 集成 Rich 美化，主动消息实时显示"""

# GensokyoAI/backends/console/_impl.py

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aioconsole
from rich.console import Console as RichConsole
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...adapters import RuntimeAdapter
from ...commands import CommandContext, CommandExecutor, CommandResult, CommandStatus
from ...core.agent import Agent
from ...core.agent.prompts import build_begin_scene_context
from ...core.events import Event, EventPriority, SystemEvent
from ...utils.formatters import format_datetime, format_session_id
from ...utils.helpers import safe_get
from ...utils.logger import logger

if TYPE_CHECKING:
    from ...runtime.host import RuntimeHost

ART = r"""
   _____________   _______ ____  __ ____  ______
  / ____/ ____/ | / / ___// __ \/ //_/\ \/ / __ \
 / / __/ __/ /  |/ /\__ \/ / / / ,<    \  / / / /
/ /_/ / /___/ /|  /___/ / /_/ / /| |   / / /_/ /
\____/_____/_/ |_//____/\____/_/ |_|  /_/\____/
"""


class ConsoleAdapter(RuntimeAdapter):
    """控制台后端 - 主动消息实时显示，Prompt.ask 负责输入"""

    def __init__(self, agent: Agent):
        self.agent = agent
        self._stream_handler: Callable | None = None
        self._running = False
        self._use_stream = True
        self.console = RichConsole()
        self.cmd_executor = CommandExecutor(mode="smart")
        self._cmd_context = CommandContext[ConsoleAdapter](
            agent=agent, backend=self, source="console", issuer="User"
        )

        # 颜色配置
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

        # 流式输出完成事件（用于主动消息等待）
        self._streaming_done = asyncio.Event()
        self._streaming_done.set()

        # 主动消息流式显示状态
        self._initiative_streaming = False
        self._initiative_first_chunk = True
        self._initiative_streamed_displayed = False

        # 是否在等待用户输入（主动回复结束后需要补回输入提示）
        self._waiting_for_input = False

        # 订阅主动消息流式片段，按正常 assistant 样式逐字显示
        agent.event_bus.subscribe(
            SystemEvent.THINK_ENGINE_INITIATIVE_CHUNK,
            self._on_initiative_chunk,
            priority=EventPriority.LOW,
            filter_func=lambda event: event.source == "initiative_timer",
        )

        # 订阅真正的主动发送消息，非流式模式下兜底显示
        agent.event_bus.subscribe(
            SystemEvent.MESSAGE_SENT,
            self._on_initiative_message_sent,
            priority=EventPriority.LOW,
            filter_func=lambda event: (
                event.source == "initiative_timer" and event.data.get("initiative")
            ),
        )

        # 订阅场景切换，提示当前场景
        agent.event_bus.subscribe(
            SystemEvent.SCENE_SWITCHED,
            self._on_scene_switched,
            priority=EventPriority.LOW,
        )

        # 累积的提示词上下文
        self._prompt_context: list[str] = []

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

    # ==================== 提示词上下文 ====================

    def _build_system_contexts(self) -> list[str]:
        """构建系统上下文列表（用于传递给 Agent）"""
        if not self._prompt_context:
            return []
        return self._prompt_context[-5:]

    # ==================== 核心方法 ====================

    @property
    def _character_name(self) -> str:
        """角色名称"""
        return safe_get(self.agent.config, "character.name", "Assistant")

    def _write_character_prefix(self) -> None:
        """打印角色名前缀（流式/非流式共用）"""
        self.console.print(f"\n[{self.colors['assistant']}]{self._character_name}: [/]", end="")

    async def start(self, host: RuntimeHost | None = None) -> None:
        """启动（host 对控制台无意义：独立入口自行装配 Agent）"""
        await self.agent.start()
        self._running = True
        logger.info("控制台后端已启动")

        self._show_welcome_panel()

        # 恢复会话时不应再触发开场场景/问候语
        current_session = self.agent.session_manager.get_current_session()
        is_resumed_session = current_session is not None and current_session.total_turns > 0

        # 模型主动开场（begin_scene=True）：优先走场景开场，否则回退 greeting
        begin_scene = safe_get(self.agent.config, "character.begin_scene")
        if not is_resumed_session and self.agent.config.begin_scene:
            if begin_scene is not None and begin_scene.has_content:
                await self._send_begin_scene(begin_scene)
            elif greeting := safe_get(self.agent.config, "character.greeting"):
                self._print_assistant_message(greeting)
        # 用户主动开场（begin_scene=False）：只走静态 greeting，等用户先开口
        elif not is_resumed_session and (
            greeting := safe_get(self.agent.config, "character.greeting")
        ):
            self._print_assistant_message(greeting)

    async def _send_begin_scene(self, begin_scene) -> None:
        """以「场景 + 开场动作」触发角色主动开场，而非静态欢迎语。

        begin_scene.scene 指定的初始场景已由 SceneManager 在 agent.start() 时设为
        当前场景，其完整环境描述会在这首轮自动注入上下文（每会话一次）；
        本方法只需把 begin_scene.action（角色此刻在做什么）作为开场驱动。
        """
        action = (begin_scene.action or "").strip()

        # 向用户展示当前场景与开场动作（灰色小字，表示这不是人说的）
        scene = None
        if self.agent.scene_manager.enabled:
            scene = await self.agent.scene_manager.get_current_scene()
        if scene is not None and action:
            self.console.print(f"[dim]（场景：{scene.name} · {action}）[/]")
        elif scene is not None:
            self.console.print(f"[dim]（场景：{scene.name}）[/]")
        elif action:
            self.console.print(f"[dim]（场景：{action}）[/]")

        system_contexts = [build_begin_scene_context()]

        # 开场动作作为角色视角的当前状态；无动作时给一句中性提示
        opening = f"（{action}）" if action else "（你正做着自己的事。）"

        # 直接走 agent 流式/非流式，不走 console send 的命令解析层
        if self._use_stream and self.agent.config.model.stream:
            self._streaming_done.clear()
            self._write_character_prefix()
            try:
                full_response = ""
                async for chunk in self.agent.send_stream(
                    opening,
                    system_contexts=system_contexts,
                ):
                    if self.agent.is_shutting_down:
                        break
                    content = chunk.content if hasattr(chunk, "content") else str(chunk)
                    if content:
                        self.console.print(content, end="")
                        full_response += content
                self.console.print()
            finally:
                self._streaming_done.set()
        else:
            response = await self.agent.send(
                opening,
                system_contexts=system_contexts,
            )
            if response is not None:
                content = response.content if isinstance(response.content, str) else ""
                if content:
                    self._print_assistant_message(content)

    def _show_welcome_panel(self) -> None:
        """显示欢迎面板"""
        character_name = safe_get(self.agent.config, "character.name", "Unknown")

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
        info_text.append("✨ 幻想乡 AI 角色扮演引擎 ✨\n", style="bold magenta")
        info_text.append("🌸 当前角色: ", style="dim")
        info_text.append(f"{character_name}\n", style="bold cyan")
        info_text.append("─" * 40 + "\n", style="dim")
        info_text.append("\n")
        info_text.append("📌 提示词标签: ", style="dim")
        info_text.append("<know> ", style="bold #FF9999")
        info_text.append("<meta> ", style="bold #FF9999")
        info_text.append("<attention>\n", style="bold #FF9999")
        info_text.append("⌨️  输入 ", style="dim")
        info_text.append("<cmd>help</cmd> ", style="bold cyan")
        info_text.append("查看所有命令\n", style="dim")

        full_content = Text()
        full_content.append(art_text)
        full_content.append(info_text)

        self.console.print(
            Panel(
                full_content,
                title="☯ 幻想乡 ☯",
                subtitle="☯ 红白巫女为您服务 ☯",
                border_style="red",
                padding=(1, 2),
            )
        )

    def _show_sessions_panel(self, sessions: list) -> None:
        """显示会话列表面板"""
        if not sessions:
            self.console.print("[dim]没有历史会话[/]")
            return

        panel_content = Text()
        panel_content.append("历史会话\n", style="bold cyan")

        sorted_sessions = sorted(sessions, key=lambda s: s.created_at, reverse=True)

        for sess in sorted_sessions:
            session_id_short = format_session_id(sess.session_id)
            created_str = format_datetime(sess.created_at)
            status = "*" if sess.is_active else "-"
            status_color = "green" if sess.is_active else "dim"

            panel_content.append(f"  {status} ", style=status_color)
            panel_content.append(f"{session_id_short}", style="bold white")
            panel_content.append(f" - {created_str} ", style="dim")
            panel_content.append(f"({sess.total_turns} 轮)\n", style="yellow")

        self.console.print(Panel(panel_content, title="会话列表", border_style="cyan"))

    def _show_initiative_timer_panel(self, timer: dict[str, Any] | None) -> None:
        """显示主动定时器状态面板。"""
        if not timer:
            self.console.print(
                Panel("[dim]当前没有主动定时器[/]", title="主动定时器", border_style="yellow")
            )
            return

        content = Text()
        content.append("状态: ", style="dim")
        content.append(f"{timer.get('status', 'unknown')}\n", style="bold yellow")
        content.append("Timer ID: ", style="dim")
        content.append(f"{timer.get('timer_id', '')}\n", style="white")
        content.append("Generation: ", style="dim")
        content.append(f"{timer.get('generation', '')}\n", style="white")
        content.append("触发时间: ", style="dim")
        content.append(f"{timer.get('due_at', '')}\n", style="cyan")
        content.append("剩余秒数: ", style="dim")
        content.append(f"{timer.get('remaining_seconds', '')}\n", style="cyan")
        content.append("可编辑字段: ", style="dim")
        content.append(", ".join(timer.get("editable_fields", [])) or "无", style="green")
        content.append("\n")
        if timer.get("reason"):
            content.append("理由: ", style="dim")
            content.append(f"{timer.get('reason')}\n", style="magenta")
        if timer.get("pending_summary"):
            content.append("待表达意图摘要:\n", style="dim")
            content.append(str(timer.get("pending_summary")), style="bold white")

        self.console.print(Panel(content, title="主动定时器", border_style="yellow"))

    def _show_initiative_trigger_result(self, result: dict[str, Any] | None) -> None:
        """显示主动定时器立即触发结果。"""
        if not result:
            self.console.print(
                Panel(
                    "[dim]没有可触发的主动定时器[/]", title="主动定时器触发", border_style="yellow"
                )
            )
            return

        content = Text()
        content.append("Timer ID: ", style="dim")
        content.append(f"{result.get('timer_id', '')}\n", style="white")
        content.append("已发送: ", style="dim")
        content.append(f"{bool(result.get('sent'))}\n", style="green")
        if result.get("pending_summary"):
            content.append("摘要: ", style="dim")
            content.append(f"{result.get('pending_summary')}\n", style="yellow")
        if result.get("thought"):
            content.append("内部思考: ", style="dim")
            content.append(f"{result.get('thought')}\n", style="magenta")
        if result.get("message"):
            content.append("主动消息:\n", style="dim")
            content.append(str(result.get("message")), style=self.colors["assistant"])

        self.console.print(Panel(content, title="主动定时器触发结果", border_style="yellow"))

    def _show_history_messages_panel(
        self,
        messages: list[dict[str, Any]],
        *,
        session: Any = None,
        limit: int = 20,
    ) -> None:
        """显示历史消息列表。"""
        table = Table(title="历史消息", show_lines=True)
        table.add_column("#", justify="right", style="dim", no_wrap=True)
        table.add_column("role", style="bold cyan", no_wrap=True)
        table.add_column("content", style="white")

        start = max(0, len(messages) - max(1, limit))
        for index, message in enumerate(messages[start:], start=start):
            content = str(message.get("content", ""))
            preview = content.replace("\n", "\\n")
            if len(preview) > 160:
                preview = preview[:157] + "..."
            table.add_row(str(index), str(message.get("role", "?")), preview)

        caption_parts = [f"总数: {len(messages)}"]
        if session is not None:
            caption_parts.append(f"会话: {format_session_id(session.session_id)}")
            caption_parts.append(f"轮数: {getattr(session, 'total_turns', 0)}")
        table.caption = " | ".join(caption_parts)
        self.console.print(table)

    def _show_history_file_hint(self, path: Path, message: str) -> None:
        """显示历史消息文件操作提示。"""
        content = Text()
        content.append(f"{message}\n", style="green")
        content.append("文件: ", style="dim")
        content.append(str(path), style="bold cyan")
        self.console.print(Panel(content, title="历史消息文件", border_style="green"))

    def _show_regenerated_message(self, message: str) -> None:
        """显示从历史位置重新生成的助手回复。"""
        content = message or "[dim]未生成内容[/]"
        self.console.print(Panel(content, title="重新生成的助手回复", border_style="magenta"))

    async def send(self, message: str, system_contexts: list[str] | None = None) -> str:
        """发送消息并获取回复"""
        if not self._running or self.agent.is_shutting_down:
            return ""

        stripped = message.strip()
        if stripped.startswith("/image "):
            content_parts = self._parse_image_input(stripped)
            if content_parts:
                return await self._send_multimodal(content_parts, system_contexts)
            return ""

        results, clean_text = await self.cmd_executor.execute(message, self._cmd_context)

        if self._handle_command_results(results):
            return "__EXIT__"

        if not clean_text:
            return ""

        system_contexts = self._build_system_contexts()

        if self._use_stream and self.agent.config.model.stream:
            response = await self._send_stream(clean_text, system_contexts)
        else:
            response = await self._send_non_stream(clean_text, system_contexts)

        return response

    async def _send_multimodal(
        self, content_parts: list[dict[str, Any]], system_contexts: list[str] | None = None
    ) -> str:
        """发送多模态消息并获取回复"""
        system_contexts = system_contexts or self._build_system_contexts()
        if self._use_stream and self.agent.config.model.stream:
            full_response = ""
            self._streaming_done.clear()
            try:
                async for chunk in self.agent.send_multimodal_stream(
                    content_parts, system_contexts
                ):
                    if self.agent.is_shutting_down:
                        break
                    if chunk.is_tool_call and chunk.tool_info:
                        self._print_tool_call_indicator(chunk.tool_info)
                    else:
                        if not full_response:
                            self._write_character_prefix()
                        self.console.print(chunk.content, end="", style=self.colors["assistant"])
                        full_response += chunk.content
                        if self._stream_handler:
                            self._stream_handler(chunk.content)
            except asyncio.CancelledError:
                logger.debug("多模态流式输出被取消")
            except Exception as e:
                logger.error(f"多模态流式输出错误: {e}")
                self._print_error_message(str(e))
            if full_response:
                self.console.print()
            self._streaming_done.set()
            return full_response

        self._write_character_prefix()
        self.console.print("思考中...", style="dim", end="\r")
        response = await self.agent.send_multimodal(content_parts, system_contexts)
        content = ""
        if response and isinstance(response.content, str):
            content = response.content
        self._write_character_prefix()
        self.console.print(content, style=self.colors["assistant"])
        if self._stream_handler:
            self._stream_handler(content)
        return content

    def _parse_image_input(self, raw: str) -> list[dict[str, Any]] | None:
        """解析 '/image path [text]'，返回统一 content parts 列表。

        为节省工作记忆，图片只存本地路径（URL），发送时由 Provider 按需读取并转 base64。
        """
        parts = raw.split(maxsplit=2)
        if len(parts) < 2:
            self._print_error_message("用法: /image <图片路径> [可选文字]")
            return None
        image_path = Path(parts[1]).expanduser().resolve()
        if not image_path.exists():
            self._print_error_message(f"图片不存在: {image_path}")
            return None
        try:
            mime_type, _ = mimetypes.guess_type(str(image_path))
            mime_type = mime_type or "image/png"
            image_url = image_path.as_uri()  # file://...
        except Exception as e:
            self._print_error_message(f"处理图片路径失败: {e}")
            return None
        result: list[dict[str, Any]] = [
            {"type": "image", "image": {"url": image_url, "mime_type": mime_type}}
        ]
        if len(parts) > 2 and parts[2].strip():
            result.insert(0, {"type": "text", "text": parts[2].strip()})
        return result

    async def _send_stream(self, message: str, system_contexts: list[str]) -> str:
        """流式发送并显示"""
        full_response = ""
        first_chunk = True

        self._streaming_done.clear()

        try:
            async for chunk in self.agent.send_stream(message, system_contexts):
                if self.agent.is_shutting_down:
                    break
                if chunk.is_tool_call and chunk.tool_info:
                    self._print_tool_call_indicator(chunk.tool_info)
                else:
                    if first_chunk:
                        self._write_character_prefix()
                        first_chunk = False

                    self.console.print(chunk.content, end="", style=self.colors["assistant"])
                    full_response += chunk.content

                    if self._stream_handler:
                        self._stream_handler(chunk.content)

        except asyncio.CancelledError:
            logger.debug("流式输出被取消")
        except Exception as e:
            logger.error(f"流式输出错误: {e}")
            error_msg = f"[错误] {e}"
            self._print_error_message(error_msg)
            if not full_response:
                full_response = error_msg

        if not first_chunk:
            self.console.print()

        self._streaming_done.set()

        return full_response

    async def _send_non_stream(self, message: str, system_contexts: list[str]) -> str:
        """非流式发送并显示"""
        self._write_character_prefix()
        self.console.print("思考中...", style="dim", end="\r")

        response = await self.agent.send(message, system_contexts)

        if response:
            self._write_character_prefix()
            content = response.content if isinstance(response.content, str) else ""
            self.console.print(content, style=self.colors["assistant"])

            if self._stream_handler:
                self._stream_handler(content)

            return content

        return ""

    def _print_assistant_message(self, message: str) -> None:
        """打印助手消息"""
        self.console.print()
        self.console.print(f"[{self.colors['assistant']}]{self._character_name}: [/]{message}")

    def _print_system_message(self, message: str, style: str = "system") -> None:
        """打印系统消息"""
        self.console.print(f"[{self.colors.get(style, style)}]{message}[/]")

    def _print_success_message(self, message: str) -> None:
        """打印成功消息"""
        self._print_system_message(f"OK: {message}", style="success")

    def _print_error_message(self, message: str) -> None:
        """打印错误消息"""
        self._print_system_message(f"ERROR: {message}", style="error")

    def _print_tool_call_indicator(self, tool_info: dict) -> None:
        """打印工具调用指示器"""
        if (message := tool_info.get("message")) and hasattr(message, "tool_calls"):
            tool_names = [tc.function.name for tc in message.tool_calls if hasattr(tc, "function")]
            if tool_names:
                logger.info(f"调用工具: {', '.join(tool_names)}")

    # ==================== 生命周期 ====================

    async def stop(self) -> None:
        """停止"""
        self._running = False
        await self.agent.shutdown()
        logger.info("控制台后端已停止")

    def set_stream_handler(self, handler: Callable | None) -> None:
        """设置流式处理器"""
        self._stream_handler = handler

    def set_stream_mode(self, enabled: bool) -> None:
        """设置是否使用流式输出"""
        self._use_stream = enabled

    def set_color(self, element: str, color: str) -> None:
        """设置颜色主题"""
        if element in self.colors:
            self.colors[element] = color

    # ==================== 主动消息实时显示 ====================

    async def _on_initiative_chunk(self, event: Event) -> None:
        """主动消息流式片段：等当前回复输出完后，按正常 assistant 样式逐字显示。"""
        chunk = event.data.get("content", "")
        done = event.data.get("done", False)
        if not isinstance(chunk, str):
            return

        if not self._initiative_streaming:
            self._initiative_streaming = True
            self._initiative_first_chunk = True
            self._initiative_streamed_displayed = False

        await self._streaming_done.wait()

        if self._initiative_first_chunk:
            self._write_character_prefix()
            self._initiative_first_chunk = False
            self._initiative_streamed_displayed = True

        if chunk:
            self.console.print(chunk, end="", style=self.colors["assistant"])

        if done:
            self.console.print()
            self._initiative_streaming = False
            self._initiative_first_chunk = True
            if self._waiting_for_input:
                self.console.print(f"[{self.colors['user']}]你: [/]", end="")

    async def _on_initiative_message_sent(self, event: Event) -> None:
        """主动消息已完整发送；若已通过流式片段显示，则避免重复输出。"""
        if self._initiative_streamed_displayed:
            self._initiative_streamed_displayed = False
            return

        message = event.data.get("content", "")
        if not isinstance(message, str) or not message.strip():
            return

        await self._streaming_done.wait()

        self.console.print()
        self.console.print(
            f"[{self.colors['assistant']}]{self._character_name}: {message.strip()}[/]"
        )
        if self._waiting_for_input:
            self.console.print(f"[{self.colors['user']}]你: [/]", end="")

    async def _on_scene_switched(self, event: Event) -> None:
        """场景切换后提示当前场景（灰色小字，风格同开场场景）。"""
        name = event.data.get("name", "")
        if not name:
            return
        await self._streaming_done.wait()
        self.console.print(f"[dim]（当前场景：{name}）[/]")
        if self._waiting_for_input:
            self.console.print(f"[{self.colors['user']}]你: [/]", end="")

    # ==================== 交互式主循环 ====================

    async def run_interactive(self) -> None:
        await self.start()

        self.console.print("[dim]Tip: 输入 [/][bold cyan]<cmd>help</cmd>[/] [dim]查看所有命令[/]")
        self.console.print("[dim]Tip: 按 Ctrl+C 安全退出（会自动保存）[/]\n")

        exited_normally = False

        try:
            while self._running and not self.agent.is_shutting_down:
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
                self._print_system_message("正在保存会话数据...", style="info")
            await self.stop()
            self._print_success_message("数据已保存，再见！")


class ConsoleBackendBuilder:
    """控制台后端构建器 - 用于链式配置"""

    def __init__(self, agent: Agent):
        self._backend = ConsoleAdapter(agent)

    def with_stream_mode(self, enabled: bool = True) -> ConsoleBackendBuilder:
        self._backend.set_stream_mode(enabled)
        return self

    def with_stream_handler(self, handler: Callable) -> ConsoleBackendBuilder:
        self._backend.set_stream_handler(handler)
        return self

    def with_color_theme(self, theme: dict[str, str]) -> ConsoleBackendBuilder:
        for element, color in theme.items():
            self._backend.set_color(element, color)
        return self

    def build(self) -> ConsoleAdapter:
        return self._backend
