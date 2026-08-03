#!/usr/bin/env python3
"""GensokyoAI CLI 入口 - 极简版"""

import argparse
import asyncio
from pathlib import Path

from rich.console import Console

from GensokyoAI.backends.console import ConsoleBackendBuilder
from GensokyoAI.core.agent import Agent
from GensokyoAI.core.config import ConfigLoader
from GensokyoAI.core.config_env import ensure_local_config
from GensokyoAI.utils.exec_hook import set_exechook
from GensokyoAI.utils.request_utils import close_client_session

# 灵梦，这是异变啊！
# 灵梦：嗯？让我看看，这也不是没啥事吗？（喝茶）
set_exechook()

console = Console()


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="GensokyoAI - 幻想乡 AI 角色扮演引擎")

    parser.add_argument("--new-session", action="store_true", help="创建新会话")
    parser.add_argument("--resume", type=str, metavar="SESSION_ID", help="恢复指定会话")
    parser.add_argument("--character", "-c", type=str, help="角色名称或角色配置文件路径")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="配置文件路径（默认 config/local.yaml，首次启动自动从模板生成）",
    )
    parser.add_argument("--list-sessions", action="store_true", help="列出所有会话")
    parser.add_argument("--no-stream", action="store_true", help="禁用流式输出")
    parser.add_argument(
        "--world",
        action="store_true",
        help="进入多角色 World 模式（config world.enabled 为 true 时自动启用）",
    )

    return parser.parse_args()


# 控制台与 World 控制台共用的配色主题
_COLOR_THEME = {
    "user": "bold #f5e6d3",
    "assistant": "bold #ffb7c5",
    "system": "dim #c9b1d4",
    "error": "bold #c41e3a",
    "success": "bold #98d8a8",
    "info": "#a4cde0",
    "cmd": "bold #d4a0d4",
    "prompt": "bold #ffe4a0",
}


def _ensure_local_config() -> Path:
    """首次启动：从发行模板生成本地配置（用户只改它，不碰 tmp/ 模板）。"""
    local_path, created = ensure_local_config()
    if created:
        console.print(
            f"[green]✓ 已生成本地配置: {local_path}（后续请修改它，不要改 tmp/ 模板）[/]"
        )
    return local_path


def find_character_file(name: str) -> Path:
    """查找角色配置文件。

    支持三种输入：
    1. 显式文件路径，例如 ``characters/zh_cn/HakureiReimu.yaml``。
    2. 项目根目录 ``characters`` 下的角色名，例如 ``example``。
    3. 内置中文角色目录 ``characters/zh_cn`` 下的角色名，例如 ``HakureiReimu``。
    """

    explicit_path = Path(name).expanduser()
    if explicit_path.exists():
        if explicit_path.is_file():
            return explicit_path
        raise FileNotFoundError(f"角色配置路径不是文件: {name}")

    project_root = Path(__file__).resolve().parents[2]
    characters_dir = project_root / "characters"
    candidates = [
        characters_dir / f"{name}.yaml",
        characters_dir / f"{name}.yml",
        characters_dir / "zh_cn" / f"{name}.yaml",
        characters_dir / "zh_cn" / f"{name}.yml",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"找不到角色配置: {name}；已搜索: {searched}")


async def main():
    """主函数"""
    args = parse_args()

    # 加载配置（未显式指定时：本地配置优先，首次启动从模板生成）
    config_file = Path(args.config) if args.config else _ensure_local_config()
    loader = ConfigLoader()
    config = loader.load(config_file)

    # 多角色 World 模式：--world 显式开启，或 config world.enabled 自动选择
    if args.world or config.world.enabled:
        await _run_world(args, config)
        return

    # 加载角色
    character_file = None
    if args.character:
        character_file = find_character_file(args.character)

    # 创建 Agent（会自动创建或加载会话）
    agent = Agent(config, config_file, character_file)

    # 会话管理
    if args.list_sessions:
        sessions = agent.session_manager.list_sessions()
        if sessions:
            console.print("[bold]历史会话:[/]")
            for sess in sessions:
                status = "●" if sess.is_active else "○"
                status_color = "green" if sess.is_active else "dim"
                console.print(
                    f"  [{status_color}]{status}[/] {sess.session_id[:8]}... - "
                    f"{sess.created_at.strftime('%Y-%m-%d %H:%M')} "
                    f"[yellow]({sess.total_turns} 轮)[/]"
                )
        else:
            console.print("[dim]没有历史会话[/]")
        return

    # 处理会话恢复/创建
    if args.resume:
        if agent.resume_session(args.resume):
            console.print(f"[green]✓ 已恢复会话: {args.resume[:8]}...[/]")
        else:
            console.print(f"[red]✗ 会话不存在: {args.resume}[/]")
            return
    elif args.new_session:
        # --new-session 强制创建新会话，不复用
        session = agent.create_session()
        console.print(f"[green]✓ 已创建新会话: {session.session_id[:8]}...[/]")
    else:
        # 无参数：尝试恢复最近会话，否则创建新会话
        sessions = agent.session_manager.list_sessions()
        if sessions:
            latest = max(sessions, key=lambda s: s.last_active)
            agent.session_manager.set_current_session(latest.session_id)  # 👈 显式设置
            console.print(
                f"[green]✓ 已恢复历史会话: {latest.session_id[:8]}... ({latest.total_turns} 轮)[/]"
            )
        else:
            session = agent.create_session()  # 👈 内部会设置
            console.print(f"[green]✓ 新会话已就绪: {session.session_id[:8]}...[/]")

    # 构建并运行控制台后端
    backend = (
        ConsoleBackendBuilder(agent)
        .with_stream_mode(not args.no_stream)
        .with_color_theme(_COLOR_THEME)
        .build()
    )

    try:
        await backend.run_interactive()
    finally:
        # 清理：关闭全局 HTTP session，释放连接池
        await close_client_session()


async def _run_world(args, config) -> None:
    """多角色 World 模式入口：装配/恢复 World 并运行 World 控制台。"""
    from datetime import datetime

    from GensokyoAI.backends.console.world_backend import WorldConsoleBackendBuilder
    from GensokyoAI.world import GensokyoWorld, WorldPersistence
    from GensokyoAI.world.world import WorldAssemblyError

    if not config.world.enabled:
        console.print("[red]✗ world.enabled 为 false：请在配置中启用 world 节后再使用 --world[/]")
        return

    persistence = (
        WorldPersistence(config.world.persistence.save_path)
        if config.world.persistence.enabled
        else None
    )

    if args.list_sessions:
        records = persistence.list(config.world.id) if persistence is not None else []
        if records:
            console.print("[bold]World 历史存档:[/]")
            for record in sorted(records, key=lambda item: item.updated_at, reverse=True):
                created = datetime.fromtimestamp(record.created_at).strftime("%Y-%m-%d %H:%M")
                console.print(
                    f"  ○ {record.session_id[:8]}... - {created} "
                    f"[yellow]({len(record.roster)} 角色)[/]"
                )
        else:
            console.print("[dim]没有 World 历史存档[/]")
        return

    try:
        if args.resume:
            if persistence is None:
                console.print("[red]✗ world.persistence 已禁用，无法恢复存档[/]")
                return
            world = await GensokyoWorld.resume(config, args.resume)
            console.print(f"[green]✓ 已恢复 World 存档: {args.resume[:8]}...[/]")
        elif args.new_session or persistence is None:
            world = await GensokyoWorld.create(config)
            console.print("[green]✓ 新 World 已就绪[/]")
        else:
            records = persistence.list(config.world.id)
            if records:
                latest = max(records, key=lambda item: item.updated_at)
                world = await GensokyoWorld.resume(config, latest.session_id)
                console.print(f"[green]✓ 已恢复最近 World 存档: {latest.session_id[:8]}...[/]")
            else:
                world = await GensokyoWorld.create(config)
                console.print("[green]✓ 新 World 已就绪[/]")
    except WorldAssemblyError as error:
        console.print(f"[red]✗ {error}[/]")
        return

    backend = (
        WorldConsoleBackendBuilder(world)
        .with_stream_mode(not args.no_stream)
        .with_color_theme(_COLOR_THEME)
        .build()
    )

    try:
        await backend.run_interactive()
    finally:
        # 清理：关闭全局 HTTP session，释放连接池
        await close_client_session()


def cli_main() -> None:
    """同步命令行入口，用于 pyproject scripts。"""

    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
