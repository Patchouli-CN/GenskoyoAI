"""env 覆盖的 SSRF 校验测试（修复 07#6：apply_env_overrides 绕过 URL 校验）。

env 覆盖（GENSOKYOAI_BASE_URL / GENSOKYOAI_EMBEDDING_BASE_URL / GENSOKYOAI_TOKEN_URL）
在 YAML 校验之后直接赋值，之前绕过 validate_external_url；现在就地补齐校验，
allow_private 对齐 YAML 层语义（本地 ollama 由 provider spec 放行、embedding 恒禁私网）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from GensokyoAI.core.config_loader import ConfigLoader
from GensokyoAI.utils.url_security import UnsafeUrlError

_TEMPLATE = Path("tmp/template-conf.yaml")


def _load_with_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """设置 env 后走 ConfigLoader.load（内部 apply_env_overrides 就地校验 URL）。"""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    ConfigLoader().load(_TEMPLATE)


def test_env_base_url_blocks_metadata_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """元数据服务地址恒拒绝（即使 provider spec 放行私网也禁 169.254.169.254）。"""
    with pytest.raises(UnsafeUrlError):
        _load_with_env(
            monkeypatch,
            {
                "GENSOKYOAI_PROVIDER": "ollama",
                "GENSOKYOAI_BASE_URL": "http://169.254.169.254/latest/meta-data/",
            },
        )


def test_env_base_url_blocks_private_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 provider（openai，allow_private_base_url=False）：内网 base_url 被拒。"""
    with pytest.raises(UnsafeUrlError):
        _load_with_env(monkeypatch, {"GENSOKYOAI_BASE_URL": "http://127.0.0.1:11434"})


def test_env_base_url_allows_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """公网 base_url 通过校验。"""
    _load_with_env(monkeypatch, {"GENSOKYOAI_BASE_URL": "https://api.example.com/v1"})


def test_env_base_url_allows_private_for_local_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地 provider（ollama spec 放行私网）：内网 base_url 通过。"""
    _load_with_env(
        monkeypatch,
        {"GENSOKYOAI_PROVIDER": "ollama", "GENSOKYOAI_BASE_URL": "http://127.0.0.1:11434"},
    )


def test_env_embedding_base_url_blocks_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """embedding base_url 恒禁私网（与 YAML 层一致，不随 provider 放行）。"""
    with pytest.raises(UnsafeUrlError):
        _load_with_env(monkeypatch, {"GENSOKYOAI_EMBEDDING_BASE_URL": "http://127.0.0.1:11434"})


def test_env_token_url_blocks_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth token 端点同样是 URL，过 SSRF 校验。"""
    with pytest.raises(UnsafeUrlError):
        _load_with_env(monkeypatch, {"GENSOKYOAI_TOKEN_URL": "http://169.254.169.254/"})


def test_apply_env_overrides_without_url_env_is_noop() -> None:
    """无 URL env 时不触发校验（回归：不破坏无 env 路径）。"""
    config = ConfigLoader().load(_TEMPLATE)
    assert config.model.base_url  # 模板默认值仍在
