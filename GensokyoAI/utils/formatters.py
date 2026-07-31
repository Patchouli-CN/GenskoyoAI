"""格式化工具"""

# GensokyoAI\utils\formatters.py

from datetime import datetime


def format_session_id(session_id: str, length: int = 8) -> str:
    """格式化会话ID显示"""
    return f"{session_id[:length]}..."


def format_datetime(dt: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """格式化日期时间"""
    return dt.strftime(fmt)
