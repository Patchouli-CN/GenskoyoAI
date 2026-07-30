"""通用辅助函数"""

# GensokyoAI\utils\helpers.py

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from .path_security import sanitize_path_id


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。项目统一使用时区感知时间。"""
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """如果 datetime 是 naive 的，则假设其为 UTC 并附加时区信息。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def async_to_sync(func: Callable[..., Awaitable[Any]]):
    """将异步函数转换为同步函数"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))  # type: ignore

    return wrapper


def sync_to_async(func: Callable):
    """将同步函数转换为异步函数"""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    return wrapper


def retry_async(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
):
    """异步重试装饰器"""

    def decorator(func: Callable[..., Awaitable[Any]]):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff

            raise last_exception  # type: ignore

        return wrapper

    return decorator


def deep_merge(base: dict, override: dict) -> dict:
    """深度合并字典"""
    result = base.copy()

    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value

    return result


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
        return [text.strip()]
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


def strip_rp_style(text: str) -> str:
    """去除角色扮演风格标记：星号动作描写（``*动作*``）与「」台词引号。

    QQ 群聊要求纯对话文本；角色卡与框架提示词都偏向 RP 风格，模型难免漏出，
    因此在发送前确定性地清洗，不依赖模型配合。清洗后留下的空行会被移除。
    """
    cleaned = _RP_ACTION_PATTERN.sub("", text)
    cleaned = cleaned.replace("「", "").replace("」", "")
    lines = [line.strip() for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


def build_world_memory_root(base_path, world_id: str, character_name: str):
    """构造 ``world/<world_id>/memory/<character_name>`` 长期记忆根。

    World 的一切存储统一收进 ``world/<world_id>/`` 命名空间（actor 私有会话、
    语义记忆、World 存档同树管理）；world id 与角色名分别净化，避免通过修改
    角色显示名伪造命名空间，也确保同一 World 的多个会话自然复用同一角色长期记忆。
    """
    safe_world_id = sanitize_path_id(world_id)
    safe_character_name = sanitize_path_id(character_name)
    return Path(base_path) / "world" / safe_world_id / "memory" / safe_character_name
