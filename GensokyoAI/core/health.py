"""框架健康中心（HealthCenter）：健康判定统一收口，关键阈值走 yaml `health:` 节。

设计（2026-08-02 用户定稿）：
- **判定归这里**：额度水位等健康判定不再散落适配器/运行时各处，一律经
  HealthCenter 按 `HealthConfig` 的**静态阈值**评估——重启不漂移。
  （此前按运行时消耗速率估算的动态阈值，重启样本清零就「满血复活」，
  判定失真，已砍；计费/消耗计量保留，纯观测不参与判定，见下。）
- **计量保留**：`compute_burn_rate` 把 ModelClient 成本采样（配置单价 ×
  usage）折算为全局日耗，供 /status 等观测展示，不影响任何健康等级。
"""

import time
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from msgspec import Struct

from .config_schema import HealthConfig

# ==================== 消耗计量（纯观测，不参与判定） ====================

# 速率折算窗口与跨度下限（秒）：只统计近 24h 的消耗；跨度不足 1 小时
# 按 1 小时摊（避免启动初期几分钟的突发被放大成天文数字的日耗）。
BURN_WINDOW_SECONDS = 86400.0
BURN_MIN_SPAN_SECONDS = 3600.0


def compute_burn_rate(
    samples: Iterable[tuple[float, float]],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """把带时间戳的成本样本（ts, 元）折算为日耗速率（元/天）。

    只统计近 BURN_WINDOW_SECONDS 的样本；跨度 = now - 窗口内最早样本，
    以 BURN_MIN_SPAN_SECONDS 为下限摊薄。无有效样本返回 {"count": 0}。
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


# ==================== 健康判定（静态阈值，HealthCenter 收口） ====================


class QuotaLevel(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    DEPLETED = "depleted"  # 🟣：余额 ≤ 0，耗尽
    UNKNOWN = "unknown"  # Provider 不支持/查询失败


QUOTA_LEVEL_EMOJI = {
    QuotaLevel.HEALTHY: "🟢",
    QuotaLevel.WARNING: "🟡",
    QuotaLevel.CRITICAL: "🔴",
    QuotaLevel.DEPLETED: "🟣",
    QuotaLevel.UNKNOWN: "⚫",
}


class QuotaVerdict(Struct):
    """额度水位判定结果（展示归调用方）。"""

    level: QuotaLevel
    index: int  # 0-100（余额 / 警告阈值，封顶 100；UNKNOWN 时为 0）
    balance: float | None


class HealthCenter:
    """框架健康总监控：所有健康判定收口此处（yaml `health:` 节配置阈值）。"""

    def __init__(self, config: HealthConfig) -> None:
        self._config = config

    @classmethod
    def from_app_config(cls, app_config: Any) -> HealthCenter:
        return cls(app_config.health)

    def evaluate_quota(self, balance: float | None) -> QuotaVerdict:
        """按静态阈值判定额度水位；balance=None（查询失败/不支持）→ UNKNOWN。"""
        if balance is None:
            return QuotaVerdict(level=QuotaLevel.UNKNOWN, index=0, balance=None)
        warn = self._config.quota_warn_yuan
        crit = self._config.quota_crit_yuan
        if balance <= 0:
            level = QuotaLevel.DEPLETED
        elif balance < crit:
            level = QuotaLevel.CRITICAL
        elif balance < warn:
            level = QuotaLevel.WARNING
        else:
            level = QuotaLevel.HEALTHY
        index = min(100, max(0, round(balance / warn * 100))) if warn > 0 else 100
        return QuotaVerdict(level=level, index=index, balance=balance)
