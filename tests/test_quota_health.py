"""额度健康动态阈值（core/agent/quota_health + ModelClient 成本采样）定向测试"""

import unittest
from collections import deque

from GensokyoAI.backends.nb2.commands import _format_quota_dynamic, _format_status
from GensokyoAI.core.agent.model_client import ModelClient
from GensokyoAI.core.agent.quota_health import QuotaLevel, compute_quota_health
from GensokyoAI.core.config import ModelConfig


class QuotaHealthComputeTests(unittest.TestCase):
    def test_four_levels_by_remaining_calls(self):
        # 中位单次 ¥0.1：warn=¥10（100 次），crit=¥2（20 次）
        self.assertIs(compute_quota_health(15.0, 0.1).level, QuotaLevel.HEALTHY)
        self.assertIs(compute_quota_health(5.0, 0.1).level, QuotaLevel.WARNING)
        self.assertIs(compute_quota_health(1.5, 0.1).level, QuotaLevel.CRITICAL)
        self.assertIs(compute_quota_health(0.05, 0.1).level, QuotaLevel.DEPLETED)

    def test_depleted_boundary_is_one_typical_call(self):
        self.assertIs(compute_quota_health(0.1, 0.1).level, QuotaLevel.CRITICAL)
        self.assertIs(compute_quota_health(0.099, 0.1).level, QuotaLevel.DEPLETED)
        self.assertIs(compute_quota_health(0.0, 0.1).level, QuotaLevel.DEPLETED)

    def test_index_caps_at_100(self):
        health = compute_quota_health(50.0, 0.1)
        self.assertEqual(health.index, 100)
        self.assertEqual(compute_quota_health(5.0, 0.1).index, 50)

    def test_returns_none_without_cost_samples(self):
        self.assertIsNone(compute_quota_health(100.0, 0.0))
        self.assertIsNone(compute_quota_health(100.0, -1.0))

    def test_remaining_calls(self):
        health = compute_quota_health(16.6, 0.027)
        self.assertAlmostEqual(health.remaining_calls, 614.8, places=0)


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

    def test_no_pricing_or_no_usage_gives_none(self):
        client = _FakeModelClient(ModelConfig())
        self.assertIsNone(client._estimate_call_cost({"prompt_tokens": 1000}))
        priced = _FakeModelClient(ModelConfig(price_input_per_million=10.0))
        self.assertIsNone(priced._estimate_call_cost(None))
        self.assertIsNone(priced._estimate_call_cost({}))

    def test_cost_stats_median(self):
        client = _FakeModelClient(ModelConfig(price_input_per_million=10.0))
        self.assertEqual(client.cost_stats(), {"count": 0})
        for cost in (0.01, 0.02, 0.03):
            client._cost_samples.append(cost)
        stats = client.cost_stats()
        self.assertEqual(stats["count"], 3)
        self.assertAlmostEqual(stats["median_cost"], 0.02)
        self.assertAlmostEqual(stats["total_cost"], 0.06)


class QuotaDisplayTests(unittest.TestCase):
    def test_dynamic_format_includes_remaining_calls(self):
        health = compute_quota_health(16.6, 0.027)
        text = _format_quota_dynamic(health)
        self.assertIn("中位单次 ¥0.0270", text)
        self.assertIn("约可再聊", text)
        # 余额可覆盖 600+ 次典型调用（远超 100 次基准）→ 健康
        self.assertIn("🟢", text)

    def test_dynamic_depleted_is_purple(self):
        health = compute_quota_health(0.01, 0.027)
        self.assertIn("🟣", _format_quota_dynamic(health))

    def test_status_prefers_dynamic_when_cost_available(self):
        text = _format_status(
            {
                "tenants": {"groups": 1, "users": 0, "meta": 0, "other": 0},
                "active_operations": 0,
                "latency": {"count": 0},
                "gates": [],
                "load_level": {"level": "healthy", "reason": "ok"},
                "cost": {"count": 20, "median_cost": 0.1, "total_cost": 2.0},
            },
            quota={"available_balance": 5.0},
            quota_fetched=True,
        )
        self.assertIn("约可再聊 50 次", text)  # 动态路径：5.0 / 0.1
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
