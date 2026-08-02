"""NoneBot2 适配器配置（环境变量驱动）。

GensokyoAI 的 config/local.yaml 不接受未知顶层键，因此适配器配置全部走
环境变量；启动入口（__main__）会先用 python-dotenv 加载 .env，
所以这些键与 NoneBot 自身配置（HOST/PORT/DRIVER 等）写在同一个 .env 即可。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...core.config_dirs import adapter_config_dir
from ...utils.logger import logger


def resolve_env_file(root_dir: Path | None = None) -> tuple[Path | None, bool]:
    """解析 nb2 的 dotenv 路径：适配器私有目录 `config/nb2/` 下的
    `local.env` / `.env` 优先（local.* 与框架 local.yaml 同风格），
    项目根 `.env` 兜底（迁移期，用完会打迁移提示）。

    返回 (路径或 None, 是否走了根目录兜底)。都不存在返回 (None, False)。
    框架只约定目录（core.config_dirs.adapter_config_dir），格式与加载
    归适配器自己——nb2 现行为 dotenv。
    """
    base = adapter_config_dir("nb2", root_dir)
    for name in ("local.env", ".env"):
        candidate = base / name
        if candidate.exists():
            return candidate, False
    fallback = (root_dir or Path.cwd()) / ".env"
    if fallback.exists():
        return fallback, True
    return None, False


def seed_local_env(root_dir: Path | None = None) -> Path:
    """首次运行：从发行模板播种 `config/nb2/local.env`（只播种一次，绝不覆盖）。

    模板缺失时返回目标路径但不报错（适配器按缺省继续）。
    """
    base = adapter_config_dir("nb2", root_dir)
    base.mkdir(parents=True, exist_ok=True)
    target = base / "local.env"
    template = (root_dir or Path.cwd()) / "tmp" / "nb2.env.example"
    if template.exists():
        shutil.copyfile(template, target)
        logger.info(f"[nb2] 首次运行已生成配置: {target}（请修改它，不要改 tmp/ 模板）")
    return target


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "是"}


def _parse_float(raw: str | None, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _parse_int(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


# QQ 聊天场景的默认风格要求（群友反馈：话少一点、按行分段、不要动作描写）；
# 可用 GSK_NB2_EXTRA_PROMPT 覆盖
DEFAULT_EXTRA_PROMPT = (
    "你在 QQ 群聊/私聊里聊天，不是在写角色扮演小说：回复要简短、口语化、像真人发消息；"
    "每句话单独占一行（系统会按行拆成多条消息依次发送）；"
    "不要用 *星号* 或括号描写动作、表情、心理活动；不要用「」等台词引号，直接写说的话；"
    "一次回复最多三句话。"
    "群聊消息每行开头的【昵称】是说话人标记（多人接连发言时会一行一人），让你分清谁在说话（私聊没有这个标记）；"
    "（引用 昵称：…）是说话人引用回复的上一条消息内容；"
    "你可以用昵称称呼对方，但自己的回复不要带【】标记。"
)


@dataclass(frozen=True)
class Nb2Config:
    """适配器运行配置；字段默认值即开箱默认值。"""

    character: str = "KirisameMarisa"
    data_dir: Path = Path("nb2_data")
    root_dir: Path | None = None  # GensokyoAI 项目根（characters/config 解析基准）；None=cwd
    group_whitelist: frozenset[int] = frozenset()  # 空 = 响应所有群
    owner_qq: frozenset[int] = frozenset()  # 指令白名单（额度查询等）；空 = 全部禁用
    initiative: bool = True  # 角色主动发言（事件订阅队列进程内投递到群/私聊）
    extra_prompt: str = DEFAULT_EXTRA_PROMPT  # 随每条回复注入 system_contexts 的附加要求
    split_reply: bool = True  # 回复按行拆成多条短消息发送（配合 extra_prompt 的按行风格）
    strip_rp_style: bool = True  # 发送前清洗 RP 风格标记（*动作*、「」引号），不依赖模型配合
    sender_label: bool = True  # 群聊消息注入【昵称】说话人标记（多对单会话的归属）
    member_memory: bool = True  # 群友印象：首轮交谈后生成第一印象，之后随消息注入
    quote_context: bool = True  # 引用回复时取原消息拼成（引用 昵称：…），让角色看到被引用的内容
    # 多人同时 @ 的合并窗口（秒）：首个发言到达后等待这么久，窗口内（及处理期间）
    # 攒下的消息合成一轮、一条回复同时回应所有人；0 = 不等待直接处理
    merge_window_seconds: float = 1.5
    # NapCat 掉线守护（bot_offline 事件/WS 断开 → 杀进程树 → 快速登录 → 确认回连）；
    # 节制：冷却期 + 每日上限，超限写哨兵文件告警停手（防无限重启激怒风控）
    watchdog_enabled: bool = True
    napcat_dir: Path = Path("ignore/NapCat.Shell")  # 相对 root_dir/cwd 或绝对路径
    watchdog_cooldown_seconds: float = 600.0  # 两次自动重启的最小间隔
    watchdog_max_restarts: int = 5  # 24h 内自动重启上限
    watchdog_recover_timeout: float = 900.0  # 重启后等待回连的超时（超时告警；冷启动实测可达 6+ 分钟）
    watchdog_disconnect_grace: float = 60.0  # WS 断开的回连宽限期（NapCat 自己会重连）
    # 掉线守护快速登录用的 bot QQ 号（硬编码 launcher 不带 main.bat 依赖）；
    # 未配置则由首次协议连接的 self_id 兜底
    bot_qq: int | None = None
    # 到点提醒：角色经 set_reminder 工具接活，到点用自己的口吻 @ 人说出；
    # nb2_data/reminders.json 持久化（重启不丢），30s tick 扫到点项
    reminders_enabled: bool = True
    reminder_max_per_tenant: int = 20  # 每个群/私聊的待办提醒上限（防滥用烧 token）
    # 注意力事务（AttentionThings）：候选预筛免费，命中才花一次性 LLM 判定，
    # 命中待办直接代办登记（不依赖主模型的工具纪律——群聊噪声下工具漏调的对症）
    attention_enabled: bool = True

    @classmethod
    def from_env(cls, get: Callable[[str], str | None] = os.environ.get) -> Nb2Config:
        """从环境变量读取配置；`get` 可注入 dict.get 便于测试。"""
        whitelist_raw = (get("GSK_NB2_GROUP_WHITELIST") or "").replace("，", ",")
        whitelist = frozenset(
            int(part)
            for part in (piece.strip() for piece in whitelist_raw.split(","))
            if part.isdigit()
        )
        root_raw = (get("GSK_NB2_ROOT_DIR") or "").strip()
        owner_raw = (get("GSK_NB2_OWNER_QQ") or "").replace("，", ",")
        owner_qq = frozenset(
            int(part)
            for part in (piece.strip() for piece in owner_raw.split(","))
            if part.isdigit()
        )
        return cls(
            character=(get("GSK_NB2_CHARACTER") or cls.character),
            data_dir=Path((get("GSK_NB2_DATA_DIR") or "").strip() or cls.data_dir),
            root_dir=Path(root_raw) if root_raw else None,
            group_whitelist=whitelist,
            owner_qq=owner_qq,
            initiative=_parse_bool(get("GSK_NB2_INITIATIVE"), cls.initiative),
            extra_prompt=((get("GSK_NB2_EXTRA_PROMPT") or "").strip() or DEFAULT_EXTRA_PROMPT),
            split_reply=_parse_bool(get("GSK_NB2_SPLIT_REPLY"), cls.split_reply),
            strip_rp_style=_parse_bool(get("GSK_NB2_STRIP_RP_STYLE"), cls.strip_rp_style),
            sender_label=_parse_bool(get("GSK_NB2_SENDER_LABEL"), cls.sender_label),
            member_memory=_parse_bool(get("GSK_NB2_MEMBER_MEMORY"), cls.member_memory),
            quote_context=_parse_bool(get("GSK_NB2_QUOTE_CONTEXT"), cls.quote_context),
            merge_window_seconds=_parse_float(
                get("GSK_NB2_MERGE_WINDOW_SECONDS"), cls.merge_window_seconds
            ),
            watchdog_enabled=_parse_bool(get("GSK_NB2_WATCHDOG"), cls.watchdog_enabled),
            napcat_dir=Path((get("GSK_NB2_NAPCAT_DIR") or "").strip() or cls.napcat_dir),
            watchdog_cooldown_seconds=_parse_float(
                get("GSK_NB2_WATCHDOG_COOLDOWN"), cls.watchdog_cooldown_seconds
            ),
            watchdog_max_restarts=_parse_int(
                get("GSK_NB2_WATCHDOG_MAX_RESTARTS"), cls.watchdog_max_restarts
            ),
            watchdog_recover_timeout=_parse_float(
                get("GSK_NB2_WATCHDOG_RECOVER_TIMEOUT"), cls.watchdog_recover_timeout
            ),
            watchdog_disconnect_grace=_parse_float(
                get("GSK_NB2_WATCHDOG_DISCONNECT_GRACE"), cls.watchdog_disconnect_grace
            ),
            reminders_enabled=_parse_bool(get("GSK_NB2_REMINDERS"), cls.reminders_enabled),
            reminder_max_per_tenant=_parse_int(
                get("GSK_NB2_REMINDER_MAX_PER_TENANT"), cls.reminder_max_per_tenant
            ),
            attention_enabled=_parse_bool(get("GSK_NB2_ATTENTION"), cls.attention_enabled),
            bot_qq=(
                int(bot_qq_raw.strip())
                if (bot_qq_raw := (get("GSK_NB2_BOT_QQ") or get("ACCOUNT") or "").strip())
                and bot_qq_raw.isdigit()
                else None
            ),
        )
