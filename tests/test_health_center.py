"""框架健康中心（core.health.HealthCenter）+ 消耗计量（compute_burn_rate / ModelClient 成本采样）定向测试

判定（静态阈值，重启不漂移）与计量（纯观测，不参与判定）分离——
2026-08-02 用户定稿：砍动态阈值判定，保留计费。
"""

import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import yaml

from GensokyoAI.core.agent.model_client import ModelClient
from GensokyoAI.core.config import ModelConfig
from GensokyoAI.core.config_loader import ConfigLoader
from GensokyoAI.core.config_schema import HealthConfig
from GensokyoAI.core.health import (
    HealthCenter,
    QuotaLevel,
    compute_burn_rate,
)
from GensokyoAI.runtime.host import RuntimeHost


class HealthCenterQuotaTests(unittest.TestCase):
    def setUp(self):
        self.center = HealthCenter(HealthConfig())  # 默认 20 / 5

    def test_four_levels_by_static_threshold(self):
        self.assertIs(self.center.evaluate_quota(25.0).level, QuotaLevel.HEALTHY)
        self.assertIs(self.center.evaluate_quota(10.0).level, QuotaLevel.WARNING)
        self.assertIs(self.center.evaluate_quota(4.0).level, QuotaLevel.CRITICAL)
        self.assertIs(self.center.evaluate_quota(0.0).level, QuotaLevel.DEPLETED)
        self.assertIs(self.center.evaluate_quota(-1.0).level, QuotaLevel.DEPLETED)

    def test_boundaries(self):
        self.assertIs(self.center.evaluate_quota(20.0).level, QuotaLevel.HEALTHY)
        self.assertIs(self.center.evaluate_quota(19.99).level, QuotaLevel.WARNING)
        self.assertIs(self.center.evaluate_quota(5.0).level, QuotaLevel.WARNING)
        self.assertIs(self.center.evaluate_quota(4.99).level, QuotaLevel.CRITICAL)

    def test_none_balance_is_unknown(self):
        verdict = self.center.evaluate_quota(None)
        self.assertIs(verdict.level, QuotaLevel.UNKNOWN)
        self.assertEqual(verdict.index, 0)
        self.assertIsNone(verdict.balance)

    def test_index_caps_at_100(self):
        self.assertEqual(self.center.evaluate_quota(100.0).index, 100)
        self.assertEqual(self.center.evaluate_quota(10.0).index, 50)

    def test_custom_thresholds_from_config(self):
        center = HealthCenter(HealthConfig(quota_warn_yuan=50.0, quota_crit_yuan=10.0))
        self.assertIs(center.evaluate_quota(30.0).level, QuotaLevel.WARNING)
        self.assertIs(center.evaluate_quota(60.0).level, QuotaLevel.HEALTHY)

    def test_thresholds_come_from_yaml_health_section(self):
        # 关键条件走 yaml `health:` 节（loader → AppConfig.health → HealthCenter）
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "local.yaml"
            config_path.write_text(
                yaml.safe_dump({"health": {"quota_warn_yuan": 50.0, "quota_crit_yuan": 10.0}}),
                encoding="utf-8",
            )
            app_config = ConfigLoader().load(config_path)
        center = HealthCenter.from_app_config(app_config)
        self.assertIs(center.evaluate_quota(30.0).level, QuotaLevel.WARNING)
        self.assertIs(center.evaluate_quota(60.0).level, QuotaLevel.HEALTHY)


class RemovedEpisodicKeysTests(unittest.TestCase):
    """memory.episodic_* 已删键：旧配置应被静默丢弃（校验层给迁移警告），
    而不是在 MemoryConfig(**data) 构造时裸 TypeError（与 initiative_timer 同一招）。"""

    def test_old_episodic_keys_load_without_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "local.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "memory": {
                            "episodic_threshold": 5,
                            "episodic_summary_model": "gpt-4",
                            "episodic_keep_recent": 3,
                            "semantic_top_k": 3,
                        }
                    }
                ),
                encoding="utf-8",
            )
            app_config = ConfigLoader().load(config_path)
        self.assertEqual(app_config.memory.semantic_top_k, 3)


class BurnRateTests(unittest.TestCase):
    """消耗计量（纯观测）：日耗折算，与判定无关。"""

    def test_empty_samples(self):
        self.assertEqual(compute_burn_rate([]), {"count": 0})

    def test_rate_over_exact_span(self):
        now = time.time()
        stats = compute_burn_rate([(now - 3600, 1.0), (now, 1.0)], now=now)
        self.assertEqual(stats["count"], 2)
        self.assertAlmostEqual(stats["total_cost"], 2.0)
        self.assertAlmostEqual(stats["burn_per_day"], 48.0)

    def test_span_floored_to_one_hour(self):
        now = time.time()
        stats = compute_burn_rate([(now - 600, 0.5), (now, 0.5)], now=now)
        self.assertAlmostEqual(stats["burn_per_day"], 24.0)
        self.assertAlmostEqual(stats["window_hours"], 1.0)

    def test_samples_older_than_window_excluded(self):
        now = time.time()
        stats = compute_burn_rate([(now - 90000, 100.0), (now - 3600, 1.0)], now=now)
        self.assertEqual(stats["count"], 1)
        self.assertAlmostEqual(stats["total_cost"], 1.0)

    def test_future_samples_excluded(self):
        now = time.time()
        stats = compute_burn_rate([(now + 60, 5.0)], now=now)
        self.assertEqual(stats["count"], 0)


class _FakeModelClient(ModelClient):
    """绕过 __init__ 的 Provider 创建，只测成本估算与采样统计。"""

    def __init__(self, config: ModelConfig):
        self.config = config
        self._cost_samples = deque(maxlen=100)


class ModelClientCostTests(unittest.TestCase):
    """计费保留：单价 × usage 的成本估算与采样统计（不参与健康判定）。"""

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
        expected = (1000 * 4.0 + 5000 * 0.7 + 100 * 21.0) / 1_000_000
        self.assertAlmostEqual(cost, expected)

    def test_cache_write_billed_at_write_price(self):
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
        self.assertAlmostEqual(stats["burn_per_day"], 24.0, places=1)


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


class HostCostStatsTests(unittest.TestCase):
    def _bare_host(self, tenant_services: dict) -> RuntimeHost:
        host = RuntimeHost.__new__(RuntimeHost)
        host._service = SimpleNamespace(_tenant_services=tenant_services)
        return host

    def test_no_tenants_returns_count_zero(self):
        self.assertEqual(self._bare_host({})._collect_cost_stats(), {"count": 0})

    def test_global_merge_across_tenants(self):
        now = time.time()
        host = self._bare_host(
            {
                "a": _fake_tenant_service(deque([(now - 3600, 1.0)])),
                "b": _fake_tenant_service(deque([(now, 1.0)])),
            }
        )
        stats = host._collect_cost_stats()
        self.assertEqual(stats["count"], 2)
        self.assertAlmostEqual(stats["total_cost"], 2.0)
        self.assertAlmostEqual(stats["burn_per_day"], 48.0, places=1)

    def test_uninitialized_tenant_skipped(self):
        now = time.time()
        host = self._bare_host(
            {
                "a": SimpleNamespace(state=SimpleNamespace(agent=None)),
                "b": _fake_tenant_service(deque([(now, 1.0)])),
            }
        )
        self.assertEqual(host._collect_cost_stats()["count"], 1)


if __name__ == "__main__":
    unittest.main()
