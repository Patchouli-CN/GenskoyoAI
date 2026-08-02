"""额度健康动态阈值（core/agent/quota_health + ModelClient 成本采样）定向测试"""

import time
import unittest
from collections import deque
from types import SimpleNamespace

from GensokyoAI.backends.nb2.commands import _format_quota_dynamic, _format_status
from GensokyoAI.core.agent.model_client import ModelClient
from GensokyoAI.core.agent.quota_health import (
    BurnRateSmoother,
    QuotaLevel,
    compute_burn_rate,
    compute_quota_health,
)
from GensokyoAI.core.config import ModelConfig
from GensokyoAI.runtime.host import RuntimeHost


class QuotaHealthComputeTests(unittest.TestCase):
    def test_four_levels_by_remaining_days(self):
        # 日耗 ¥1：warn=¥7（7 天），crit=¥1（1 天），紫=¥1/24（撑不过 1 小时）
        self.assertIs(compute_quota_health(10.0, 1.0).level, QuotaLevel.HEALTHY)
        self.assertIs(compute_quota_health(5.0, 1.0).level, QuotaLevel.WARNING)
        self.assertIs(compute_quota_health(0.5, 1.0).level, QuotaLevel.CRITICAL)
        self.assertIs(compute_quota_health(0.04, 1.0).level, QuotaLevel.DEPLETED)

    def test_depleted_boundary_is_one_hour_of_burn(self):
        one_hour = 1.0 / 24.0
        self.assertIs(compute_quota_health(one_hour, 1.0).level, QuotaLevel.CRITICAL)
        self.assertIs(compute_quota_health(one_hour - 0.001, 1.0).level, QuotaLevel.DEPLETED)
        self.assertIs(compute_quota_health(0.0, 1.0).level, QuotaLevel.DEPLETED)

    def test_fast_burn_raises_threshold_slow_burn_lowers(self):
        # 用户定稿性质：烧得快阈值升高、烧得慢阈值降低（全局速率型）
        # 日耗 ¥10：warn=¥70——同样余额 ¥50 从健康跌成警告
        self.assertIs(compute_quota_health(50.0, 10.0).level, QuotaLevel.WARNING)
        # 日耗 ¥0.1：warn=¥0.7——余额 ¥5 依然健康
        self.assertIs(compute_quota_health(5.0, 0.1).level, QuotaLevel.HEALTHY)

    def test_index_caps_at_100(self):
        health = compute_quota_health(70.0, 1.0)
        self.assertEqual(health.index, 100)
        self.assertEqual(compute_quota_health(3.5, 1.0).index, 50)

    def test_returns_none_without_burn_rate(self):
        self.assertIsNone(compute_quota_health(100.0, 0.0))
        self.assertIsNone(compute_quota_health(100.0, -1.0))

    def test_remaining_days(self):
        health = compute_quota_health(16.6, 2.0)
        self.assertAlmostEqual(health.remaining_days, 8.3, places=1)


class BurnRateTests(unittest.TestCase):
    def test_empty_samples(self):
        self.assertEqual(compute_burn_rate([]), {"count": 0})

    def test_rate_over_exact_span(self):
        now = time.time()
        # 1 小时内两笔共 ¥2 → 日耗 ¥48
        stats = compute_burn_rate([(now - 3600, 1.0), (now, 1.0)], now=now)
        self.assertEqual(stats["count"], 2)
        self.assertAlmostEqual(stats["total_cost"], 2.0)
        self.assertAlmostEqual(stats["burn_per_day"], 48.0)

    def test_span_floored_to_one_hour(self):
        now = time.time()
        # 10 分钟烧 ¥1 不外推为 ¥144/天，按 1 小时摊薄 → ¥24/天
        stats = compute_burn_rate([(now - 600, 0.5), (now, 0.5)], now=now)
        self.assertAlmostEqual(stats["burn_per_day"], 24.0)
        self.assertAlmostEqual(stats["window_hours"], 1.0)

    def test_samples_older_than_window_excluded(self):
        now = time.time()
        stats = compute_burn_rate(
            [(now - 90000, 100.0), (now - 3600, 1.0)], now=now
        )
        self.assertEqual(stats["count"], 1)
        self.assertAlmostEqual(stats["total_cost"], 1.0)

    def test_future_samples_excluded(self):
        now = time.time()
        stats = compute_burn_rate([(now + 60, 5.0)], now=now)
        self.assertEqual(stats["count"], 0)


class BurnRateSmootherTests(unittest.TestCase):
    """快升慢降（警戒时间）：上升立即、下降按半衰期指数回落。"""

    def test_rise_is_instant(self):
        smoother = BurnRateSmoother()
        self.assertEqual(smoother.update(10.0, now=1000.0), 10.0)
        self.assertEqual(smoother.update(20.0, now=1001.0), 20.0)

    def test_fall_decays_by_half_life(self):
        smoother = BurnRateSmoother(half_life_seconds=3600.0)
        smoother.update(10.0, now=1000.0)
        # 突然静默：1 个半衰期后只降到一半，不瞬间归零
        self.assertAlmostEqual(smoother.update(0.0, now=4600.0), 5.0)
        self.assertAlmostEqual(smoother.update(0.0, now=8200.0), 2.5)

    def test_raw_between_decayed_and_peak_wins(self):
        smoother = BurnRateSmoother(half_life_seconds=3600.0)
        smoother.update(10.0, now=1000.0)
        # 半衰期到，衰减值 5；真实速率 7 更高 → 采用真实值
        self.assertAlmostEqual(smoother.update(7.0, now=4600.0), 7.0)

    def test_negative_raw_clamped_to_zero(self):
        smoother = BurnRateSmoother(half_life_seconds=3600.0)
        smoother.update(10.0, now=1000.0)
        self.assertAlmostEqual(smoother.update(-3.0, now=4600.0), 5.0)


def _fake_tenant_service(cost_samples: deque) -> SimpleNamespace:
    """模拟一个租户服务：agent.runtime_context.model_client._cost_samples。"""
    return SimpleNamespace(
        state=SimpleNamespace(
            agent=SimpleNamespace(
                runtime_context=SimpleNamespace(
                    model_client=SimpleNamespace(_cost_samples=cost_samples)
                )
            )
        )
    )


def _bare_host(tenant_services: dict) -> RuntimeHost:
    """绕过 __init__ 构造只够跑 _collect_cost_stats 的 RuntimeHost。"""
    host = RuntimeHost.__new__(RuntimeHost)
    host._service = SimpleNamespace(_tenant_services=tenant_services)
    host._cost_smoother = BurnRateSmoother()
    host._cost_has_samples = False
    return host


class HostCostStatsTests(unittest.TestCase):
    def test_never_sampled_returns_count_zero(self):
        host = _bare_host({})
        self.assertEqual(host._collect_cost_stats(), {"count": 0})
        self.assertEqual(host._collect_cost_stats(), {"count": 0})  # 不置 has_samples

    def test_global_merge_across_tenants(self):
        now = time.time()
        host = _bare_host(
            {
                "a": _fake_tenant_service(deque([(now - 3600, 1.0)])),
                "b": _fake_tenant_service(deque([(now, 1.0)])),
            }
        )
        stats = host._collect_cost_stats()
        self.assertEqual(stats["count"], 2)
        self.assertAlmostEqual(stats["total_cost"], 2.0)
        self.assertAlmostEqual(stats["burn_per_day"], 48.0, places=1)

    def test_silence_after_samples_decays_instead_of_cliff(self):
        now = time.time()
        samples = deque([(now - 3600, 1.0), (now, 1.0)])
        host = _bare_host({"a": _fake_tenant_service(samples)})
        first = host._collect_cost_stats()
        self.assertGreater(first["burn_per_day"], 0)
        # 全天静默（样本滑出 24h 窗口）：不回落 {"count": 0} 静态路径，
        # 而是沿平滑器指数衰减（raw=0 仍报平滑值）
        samples.clear()
        later = host._collect_cost_stats()
        self.assertEqual(later["count"], 0)
        self.assertEqual(later["raw_burn_per_day"], 0.0)
        self.assertGreater(later["burn_per_day"], 0.0)
        self.assertLessEqual(later["burn_per_day"], first["burn_per_day"])


class _FakeModelClient(ModelClient):
    """绕过 __init__ 的 Provider 创建，只测成本估算与采样统计。"""

    def __init__(self, config: ModelConfig):
        self.config = config
        self._cost_samples = deque(maxlen=100)


class ModelClientCostTests(unittest.TestCase):
    def test_estimate_openai_style_usage(self):
        client = _FakeModelClient(
            ModelConfig(price_input_per_million=10.0, price_output_per_million=30.0)
        )
        cost = client._estimate_call_cost({"prompt_tokens": 1000, "completion_tokens": 500})
        self.assertAlmostEqual(cost, (1000 * 10.0 + 500 * 30.0) / 1_000_000)

    def test_estimate_claude_style_usage(self):
        client = _FakeModelClient(ModelConfig(price_input_per_million=10.0))
        cost = client._estimate_call_cost({"input_tokens": 2000, "output_tokens": 100})
        self.assertAlmostEqual(cost, 2000 * 10.0 / 1_000_000)

    def test_anthropic_cache_fields_billed_at_cached_price(self):
        client = _FakeModelClient(
            ModelConfig(
                price_input_per_million=4.0,
                price_output_per_million=21.0,
                price_input_cached_per_million=0.7,
            )
        )
        cost = client._estimate_call_cost(
            {
                "input_tokens": 1000,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 2000,
                "output_tokens": 100,
            }
        )
        # 缓存读取按 0.7、缓存创建按全价 4.0（独立于 input_tokens 相加）
        expected = (1000 * 4.0 + 5000 * 0.7 + 2000 * 4.0 + 100 * 21.0) / 1_000_000
        self.assertAlmostEqual(cost, expected)

    def test_openai_cached_subset_split_from_prompt(self):
        client = _FakeModelClient(
            ModelConfig(
                price_input_per_million=4.0,
                price_output_per_million=21.0,
                price_input_cached_per_million=0.7,
            )
        )
        cost = client._estimate_call_cost(
            {
                "prompt_tokens": 6000,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 5000},
            }
        )
        # cached_tokens 是 prompt_tokens 的子集：拆开分别计价
        expected = (1000 * 4.0 + 5000 * 0.7 + 100 * 21.0) / 1_000_000
        self.assertAlmostEqual(cost, expected)

    def test_cache_billed_full_price_without_cached_price(self):
        client = _FakeModelClient(
            ModelConfig(price_input_per_million=4.0, price_output_per_million=21.0)
        )
        cost = client._estimate_call_cost(
            {"input_tokens": 1000, "cache_read_input_tokens": 5000, "output_tokens": 100}
        )
        expected = (1000 * 4.0 + 5000 * 4.0 + 100 * 21.0) / 1_000_000
        self.assertAlmostEqual(cost, expected)

    def test_cache_write_billed_at_write_price(self):
        # Anthropic 口径：缓存写入 1.25× 输入价、读取 0.1×
        client = _FakeModelClient(
            ModelConfig(
                price_input_per_million=24.0,
                price_output_per_million=120.0,
                price_input_cached_per_million=2.4,
                price_input_cache_write_per_million=30.0,
            )
        )
        cost = client._estimate_call_cost(
            {
                "input_tokens": 1000,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 2000,
                "output_tokens": 100,
            }
        )
        expected = (1000 * 24.0 + 5000 * 2.4 + 2000 * 30.0 + 100 * 120.0) / 1_000_000
        self.assertAlmostEqual(cost, expected)

    def test_no_pricing_or_no_usage_gives_none(self):
        client = _FakeModelClient(ModelConfig())
        self.assertIsNone(client._estimate_call_cost({"prompt_tokens": 1000}))
        priced = _FakeModelClient(ModelConfig(price_input_per_million=10.0))
        self.assertIsNone(priced._estimate_call_cost(None))
        self.assertIsNone(priced._estimate_call_cost({}))

    def test_cost_stats_burn_rate(self):
        client = _FakeModelClient(ModelConfig(price_input_per_million=10.0))
        self.assertEqual(client.cost_stats(), {"count": 0})
        now = time.time()
        for ts, cost in ((now - 3600, 0.5), (now, 0.5)):
            client._cost_samples.append((ts, cost))
        stats = client.cost_stats()
        self.assertEqual(stats["count"], 2)
        self.assertAlmostEqual(stats["total_cost"], 1.0)
        # 1 小时 ¥1 → 日耗 ¥24（与全局同一算法）
        self.assertAlmostEqual(stats["burn_per_day"], 24.0, places=1)


class QuotaDisplayTests(unittest.TestCase):
    def test_dynamic_format_includes_burn_and_days(self):
        health = compute_quota_health(16.6, 2.0)
        text = _format_quota_dynamic(health)
        self.assertIn("日耗 ¥2.00", text)
        self.assertIn("约可再撑 8.3 天", text)
        # 余额可覆盖 8.3 天消耗（超过 7 天基准）→ 健康
        self.assertIn("🟢", text)

    def test_dynamic_format_shows_hours_under_one_day(self):
        health = compute_quota_health(1.0, 2.0)
        text = _format_quota_dynamic(health)
        self.assertIn("约可再撑 12 小时", text)
        self.assertIn("🔴", text)

    def test_dynamic_depleted_is_purple(self):
        health = compute_quota_health(0.01, 2.0)
        self.assertIn("🟣", _format_quota_dynamic(health))

    def test_status_prefers_dynamic_when_cost_available(self):
        text = _format_status(
            {
                "tenants": {"groups": 1, "users": 0, "meta": 0, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
                "gates": [],
                "load_level": {"level": "healthy", "reason": "ok"},
                "cost": {"count": 20, "burn_per_day": 1.0, "total_cost": 2.0},
            },
            quota={"available_balance": 5.0},
            quota_fetched=True,
        )
        self.assertIn("约可再撑 5.0 天", text)  # 动态路径：5.0 / 1.0
        self.assertIn("🟡", text)  # 撑不过 7 天 → 黄警告
        self.assertNotIn("暂不可用", text)

    def test_status_falls_back_to_static_without_cost(self):
        text = _format_status(
            {
                "tenants": {"groups": 1, "users": 0, "meta": 0, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
                "gates": [],
                "load_level": {"level": "healthy", "reason": "ok"},
                "cost": {"count": 0},
            },
            quota={"available_balance": 10.0},
            quota_fetched=True,
            quota_warn=20.0,
            quota_crit=5.0,
        )
        self.assertIn("🟡 健康指数 50", text)  # 静态回落：10/20

    def test_static_zero_balance_is_purple(self):
        text = _format_status(
            {
                "tenants": {"groups": 1, "users": 0, "meta": 0, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
                "gates": [],
                "load_level": {"level": "healthy", "reason": "ok"},
            },
            quota={"available_balance": 0.0},
            quota_fetched=True,
        )
        self.assertIn("🟣 耗尽", text)


if __name__ == "__main__":
    unittest.main()
