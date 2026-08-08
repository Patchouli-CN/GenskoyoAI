"""通用辅助函数"""

# GensokyoAI\utils\helpers.py

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from .path_security import sanitize_path_id


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。项目统一使用时区感知时间。"""
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """如果 datetime 是 naive 的，则假设其为 UTC 并附加时区信息。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def safe_get(obj: Any, path: str, default: Any = None) -> Any:
    """安全获取嵌套属性"""
    try:
        for key in path.split("."):
            obj = obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
            if obj is None:
                return default
        return obj
    except AttributeError, KeyError, TypeError:
        return default


def split_reply_segments(text: str, max_segments: int = 5) -> list[str]:
    """把回复按行拆成多条短消息（QQ 群聊风格：每句话一行，行边界即句子边界）。

    配合提示词使用：模型被要求每句话单独一行，因此这里不做句读分析，
    只按行拆分、丢弃空行；段数超过 ``max_segments`` 时超出部分合并进
    最后一段，不丢内容。
    """
    parts = [line.strip() for line in text.splitlines() if line.strip()]
    if not parts:
        return []  # 全空白/空输入：没有可发送的段（旧 CODE_INF 07#11）
    if len(parts) <= max_segments:
        return parts
    return parts[: max_segments - 1] + ["\n".join(parts[max_segments - 1 :])]


def sanitize_display_name(name: str, max_chars: int = 24) -> str:
    """净化 QQ 昵称/群名片用于提示词注入：去换行与各类括号、限长。

    群名片是用户可控文本，直接拼进提示词有注入风险（伪造指令/标记），
    这里只保留纯展示字符。
    """
    cleaned = re.sub(r"[\r\n【\[\]】<>「」]+", " ", name).strip()
    return cleaned[:max_chars]


_RP_ACTION_PATTERN = re.compile(r"\*[^*\n]*\*")

# 行首说话人标记（适配器注入的【昵称】前缀约定，见 nb2 plugin）
_SPEAKER_TAG_PATTERN = re.compile(r"^【([^【】\r\n]+)】", re.MULTILINE)

# 整行括号舞台指示（（环顾四周）/(waves fan)）：仅整行包裹且内容短才剥，
# 行内括注（如「说得对（笑）」）是 QQ 口语，不动
_STAGE_DIRECTION_PATTERN = re.compile(r"^\s*[（(][^（）()\n]{1,30}[）)]\s*$", re.MULTILINE)

# 行首伪说话人标签：【昵称】是适配器给入站消息的注入格式，模型在出站回复里
# 逐字带出（模仿上下文的格式泄漏）一律剥除——我们的出站格式从不以【开头
_FAKE_SPEAKER_PATTERN = re.compile(r"^【[^【】\r\n]{1,20}】", re.MULTILINE)

# 独立分隔线（--- / —— / === 等）：换名复读事故的「旧文+分隔线+新答」形态
_DIVIDER_PATTERN = re.compile(r"^\s*(?:-{3,}|—{2,}|={3,}|＝{2,}|_{3,})\s*$", re.MULTILINE)

# 模型意外输出的 XML 标签残留（<get_current_time>、</think> 等协议字节）
_XML_RESIDUE_PATTERN = re.compile(r"</?[a-z_]+[^>]*>")


class StripReport(NamedTuple):
    """清洗结果：text 为清洗后文本；stripped 是被剥除内容的类别标签（遥测回流）。"""

    text: str
    stripped: list[str]


def extract_speaker_tags(text: str) -> list[str]:
    """提取行首【昵称】说话人标记（去重保序）；无标记返回空列表。

    适配器把群聊消息拼成 `【昵称】内容`（合并批次每行一个），本函数只做
    机械提取——私聊/控制台等无标记场景自然返回空，调用方无需区分来源。
    """
    tags: list[str] = []
    for name in _SPEAKER_TAG_PATTERN.findall(text):
        if name not in tags:
            tags.append(name)
    return tags


def clean_display_text(text: str) -> StripReport:
    """投递档（全量清洗）：QQ 群聊要求纯对话文本，发送前确定性清洗。

    - 星号动作描写（``*动作*``）与「」台词引号；
    - 整行括号舞台指示（``（环顾四周）`` / ``(waves)``；行内括注不动）；
    - 行首伪说话人标签（``【名字】`` 是适配器给入站消息的注入格式，出站带出即泄漏）；
    - 独立分隔线（``---`` 等，换名复读事故的「旧文+分隔线+新答」形态）。
    清洗后留下的空行会被移除。``stripped`` 记录剥除类别供遥测回流。
    """
    stripped: list[str] = []
    cleaned = text
    if _RP_ACTION_PATTERN.search(cleaned):
        stripped.append("rp_action")
        cleaned = _RP_ACTION_PATTERN.sub("", cleaned)
    if _STAGE_DIRECTION_PATTERN.search(cleaned):
        stripped.append("stage_direction")
        cleaned = _STAGE_DIRECTION_PATTERN.sub("", cleaned)
    if _FAKE_SPEAKER_PATTERN.search(cleaned):
        stripped.append("fake_speaker_tag")
        cleaned = _FAKE_SPEAKER_PATTERN.sub("", cleaned)
    if _DIVIDER_PATTERN.search(cleaned):
        stripped.append("divider")
        cleaned = _DIVIDER_PATTERN.sub("", cleaned)
    if "「" in cleaned or "」" in cleaned:
        stripped.append("corner_quotes")
        cleaned = cleaned.replace("「", "").replace("」", "")
    lines = [line.strip() for line in cleaned.splitlines()]
    return StripReport("\n".join(line for line in lines if line), stripped)


def clean_memory_text(text: str) -> StripReport:
    """记忆档（保守清洗）：只剥格式泄漏——伪说话人标签、独立分隔线、XML 残留。

    RP 风格内容（*动作*、「」、整行括注）保留：World 舞台旁白是正当格式，
    QQ 侧的风格问题归 OOC 判定管语义——记忆层只负责不让协议字节/格式泄漏
    回流进历史（模型从自己历史里学到泄漏格式会回音式复读）。
    """
    stripped: list[str] = []
    cleaned = text
    if _FAKE_SPEAKER_PATTERN.search(cleaned):
        stripped.append("fake_speaker_tag")
        cleaned = _FAKE_SPEAKER_PATTERN.sub("", cleaned)
    if _DIVIDER_PATTERN.search(cleaned):
        stripped.append("divider")
        cleaned = _DIVIDER_PATTERN.sub("", cleaned)
    if _XML_RESIDUE_PATTERN.search(cleaned):
        stripped.append("xml_residue")
        cleaned = _XML_RESIDUE_PATTERN.sub("", cleaned)
    lines = [line.strip() for line in cleaned.splitlines()]
    return StripReport("\n".join(line for line in lines if line), stripped)


def strip_rp_style(text: str) -> str:
    """去除角色扮演风格标记（投递档纯文本版，兼容旧调用）。详见 clean_display_text。"""
    return clean_display_text(text).text


def build_world_memory_root(base_path, world_id: str, character_name: str):
    """构造 ``world/<world_id>/memory/<character_name>`` 长期记忆根。

    World 的一切存储统一收进 ``world/<world_id>/`` 命名空间（actor 私有会话、
    语义记忆、World 存档同树管理）；world id 与角色名分别净化，避免通过修改
    角色显示名伪造命名空间，也确保同一 World 的多个会话自然复用同一角色长期记忆。
    """
    safe_world_id = sanitize_path_id(world_id)
    safe_character_name = sanitize_path_id(character_name)
    return Path(base_path) / "world" / safe_world_id / "memory" / safe_character_name
