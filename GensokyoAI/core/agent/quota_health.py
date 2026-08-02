"""额度健康：按全局消耗速率动态计算的余额水位（框架统一算法）。

设计（用户 2026-08-02 定稿）：警告/临界阈值不按「次」而按「单位时间
总消耗」算——全租户成本样本（带时间戳）合并折算日耗（元/天），阈值 =
日耗 × 基准天数。烧得快阈值自动升高、烧得慢自动降低，全局统一、不按
租户分开。余额撑不过 1 小时即「耗尽」（紫色，四级水位新增）。

速率折算：近 24h 窗口内的样本求和 ÷ 实际时间跨度；跨度下限 1 小时
（启动初期摊薄，宁可低估也不爆表）。无消耗样本（单价未配置 / 尚无带
usage 的调用）时 compute 返回 None，由调用方回落静态阈值或直接隐藏
——绝不拿拍脑袋的数字冒充动态阈值。
"""

import time
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from msgspec import Struct

# 动态阈值基准（天）：余额覆盖不了 WARN_DAYS 天消耗 → 黄警告；
# 覆盖不了 CRIT_DAYS 天 → 红临界；撑不过 1 小时（日耗 / 24）→ 紫耗尽。
QUOTA_WARN_DAYS = 7.0
QUOTA_CRIT_DAYS = 1.0

# 速率折算窗口与跨度下限（秒）：只统计近 24h 的消耗；跨度不足 1 小时
# 按 1 小时摊（避免启动初期几分钟的突发被放大成天文数字的日耗）。
BURN_WINDOW_SECONDS = 86400.0
BURN_MIN_SPAN_SECONDS = 3600.0

# 速率回落半衰期（秒）：烧得慢下来时阈值不瞬间降低，按半衰期指数回落
# （警戒时间，用户 2026-08-02 定稿：避免状态突然健康/突然恶化）。
BURN_FALL_HALF_LIFE_SECONDS = 6 * 3600.0


class QuotaLevel(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    DEPLETED = "depleted"  # 紫色：按当前速度撑不过 1 小时


class BurnRateSmoother:
    """日耗速率的「快升慢降」平滑器（警戒时间）。

    上升立即生效——烧得快必须马上告警；下降按半衰期指数回落——突然慢
    下来（或全天静默样本滑出 24h 窗口）不瞬间拉低阈值，状态不会突然
    健康/突然恶化。无状态调用方直接用 compute_burn_rate 的原始值即可，
    需要跨次观测连续性的（如 /status）持有一个实例逐次 update。
    """

    def __init__(self, half_life_seconds: float = BURN_FALL_HALF_LIFE_SECONDS) -> None:
        self._half_life = half_life_seconds
        self._rate: float | None = None
        self._ts = 0.0

    def update(self, raw_rate: float, *, now: float | None = None) -> float:
        """喂入本次原始日耗，返回平滑后的日耗（快升慢降）。"""
        current = time.time() if now is None else now
        raw = max(0.0, raw_rate)
        if self._rate is None or raw >= self._rate:
            self._rate, self._ts = raw, current
            return raw
        decayed = self._rate * 0.5 ** ((current - self._ts) / self._half_life)
        self._rate = max(raw, decayed)
        self._ts = current
        return self._rate


class QuotaHealth(Struct):
    """额度健康报告。"""

    level: QuotaLevel
    index: int  # 0-100（余额 / 黄警告阈值，封顶 100）
    balance: float  # 当前余额（元）
    burn_per_day: float  # 全局日耗（元/天，近 24h 样本折算）
    warn_threshold: float  # 黄警告阈值（元）
    crit_threshold: float  # 红临界阈值（元）
    remaining_days: float  # 余额 / 日耗（按当前速度还能撑几天）


def compute_burn_rate(
    samples: Iterable[tuple[float, float]],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """把带时间戳的成本样本（ts, 元）折算为日耗速率（元/天）。

    只统计近 BURN_WINDOW_SECONDS 的样本；跨度 = now - 窗口内最早样本，
    以 BURN_MIN_SPAN_SECONDS 为下限摊薄。无有效样本返回 {"count": 0}
    （调用方按无样本回落，与旧版中位数路径同一约定）。
    """
    current = time.time() if now is None else now
    recent = [
        (ts, cost) for ts, cost in samples if 0.0 <= current - ts <= BURN_WINDOW_SECONDS
    ]
    if not recent:
        return {"count": 0}
    total = sum(cost for _, cost in recent)
    span = max(current - min(ts for ts, _ in recent), BURN_MIN_SPAN_SECONDS)
    return {
        "count": len(recent),
        "total_cost": total,
        "window_hours": span / 3600.0,
        "burn_per_day": total / span * 86400.0,
    }


def compute_quota_health(
    balance: float,
    burn_per_day: float,
    *,
    warn_days: float = QUOTA_WARN_DAYS,
    crit_days: float = QUOTA_CRIT_DAYS,
) -> QuotaHealth | None:
    """按全局日耗计算额度健康；无法计算（日耗 ≤ 0）返回 None。"""
    if burn_per_day <= 0:
        return None
    warn_threshold = burn_per_day * warn_days
    crit_threshold = burn_per_day * crit_days
    depleted_threshold = burn_per_day / 24.0  # 撑不过 1 小时
    remaining = balance / burn_per_day
    if balance < depleted_threshold:
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
        burn_per_day=burn_per_day,
        warn_threshold=warn_threshold,
        crit_threshold=crit_threshold,
        remaining_days=remaining,
    )
