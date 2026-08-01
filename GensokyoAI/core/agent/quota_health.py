"""额度健康：按消耗中位数动态计算的余额水位（框架统一算法）。

设计（用户 2026-08-01 定稿）：警告/临界阈值不静态写死，而是按
「余额还能撑多少次典型调用」评估——单次调用成本由 ModelClient 按
配置单价 × usage token 采样（滚动窗口取中位数），阈值 = 中位成本 ×
基准调用次数。余额不足一次典型调用即「耗尽」（紫色，四级水位新增）。

无消耗样本（单价未配置 / 尚无带 usage 的调用）时 compute 返回 None，
由调用方回落静态阈值或直接隐藏——绝不拿拍脑袋的数字冒充动态阈值。
"""

from enum import StrEnum

from msgspec import Struct

# 动态阈值基准（次）：余额覆盖不了 WARN_CALLS 次典型调用 → 黄警告；
# 覆盖不了 CRIT_CALLS 次 → 红临界；不足 1 次 → 紫耗尽。
QUOTA_WARN_CALLS = 100
QUOTA_CRIT_CALLS = 20


class QuotaLevel(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    DEPLETED = "depleted"  # 紫色：余额连一次典型调用都撑不起


class QuotaHealth(Struct):
    """额度健康报告。"""

    level: QuotaLevel
    index: int  # 0-100（余额 / 黄警告阈值，封顶 100）
    balance: float  # 当前余额（元）
    median_cost: float  # 中位单次调用成本（元）
    warn_threshold: float  # 黄警告阈值（元）
    crit_threshold: float  # 红临界阈值（元）
    remaining_calls: float  # 余额 / 中位成本（约可再聊次数）


def compute_quota_health(
    balance: float,
    median_cost: float,
    *,
    warn_calls: int = QUOTA_WARN_CALLS,
    crit_calls: int = QUOTA_CRIT_CALLS,
) -> QuotaHealth | None:
    """按消耗中位数计算额度健康；无法计算（中位成本 ≤ 0）返回 None。"""
    if median_cost <= 0:
        return None
    warn_threshold = median_cost * warn_calls
    crit_threshold = median_cost * crit_calls
    remaining = balance / median_cost
    if balance < median_cost:
        level = QuotaLevel.DEPLETED
    elif balance < crit_threshold:
        level = QuotaLevel.CRITICAL
    elif balance < warn_threshold:
        level = QuotaLevel.WARNING
    else:
        level = QuotaLevel.HEALTHY
    index = min(100, max(0, round(balance / warn_threshold * 100)))
    return QuotaHealth(
        level=level,
        index=index,
        balance=balance,
        median_cost=median_cost,
        warn_threshold=warn_threshold,
        crit_threshold=crit_threshold,
        remaining_calls=remaining,
    )
