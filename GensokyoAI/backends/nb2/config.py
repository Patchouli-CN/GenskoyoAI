"""NoneBot2 适配器配置（环境变量驱动）。

GensokyoAI 的 config/local.yaml 不接受未知顶层键，因此适配器配置全部走
环境变量；启动入口（__main__）会先用 python-dotenv 加载 .env，
所以这些键与 NoneBot 自身配置（HOST/PORT/DRIVER 等）写在同一个 .env 即可。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "是"}


# QQ 聊天场景的默认风格要求（群友反馈：话少一点、按行分段、不要动作描写）；
# 可用 GSK_NB2_EXTRA_PROMPT 覆盖
DEFAULT_EXTRA_PROMPT = (
    "你在 QQ 群聊/私聊里聊天，不是在写角色扮演小说：回复要简短、口语化、像真人发消息；"
    "每句话单独占一行（系统会按行拆成多条消息依次发送）；"
    "不要用 *星号* 或括号描写动作、表情、心理活动；不要用「」等台词引号，直接写说的话；"
    "一次回复最多三句话。"
    "群聊消息开头的【昵称】是说话人标记，让你分清谁在说话（私聊没有这个标记）；"
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
        )
