"""月相工具"""

# GensokyoAI\tools\tool_builtin\moon.py

from datetime import UTC, datetime, timedelta

from ...utils.helpers import utc_now
from ..base import tool

# 朔望月平均长度（天）
_SYNODIC_MONTH_DAYS = 29.53058867
# 参考新月历元：2000-01-06 18:14 UTC（J2000 附近一次被广泛使用的精确新月）
_NEW_MOON_EPOCH = datetime(2000, 1, 6, 18, 14, tzinfo=UTC)
_PHASES = ["新月", "娥眉月", "上弦月", "盈凸月", "满月", "亏凸月", "下弦月", "残月"]


@tool(description="获取月相，可以指定偏移天数（正数为未来，负数为过去）")
def get_moon_phase(days_delta: int = 0) -> str:
    """
    获取指定日期的月相

    Args:
        days_delta: 相对于今天的偏移天数，0表示今天
    """
    target = utc_now() + timedelta(days=days_delta)
    # 以已知新月历元为基准，按朔望月周期近似月龄 → 映射到 8 相（06#5：
    # 旧实现 `day % 8` 与真实 ~29.5 天朔望周期无关，输出确定但完全错误）
    elapsed_days = (target - _NEW_MOON_EPOCH).total_seconds() / 86400.0
    moon_age = elapsed_days % _SYNODIC_MONTH_DAYS
    index = int(moon_age / _SYNODIC_MONTH_DAYS * len(_PHASES)) % len(_PHASES)
    return _PHASES[index]
