"""SSRF URL 安全校验测试。"""

from __future__ import annotations

import pytest

from GensokyoAI.utils.url_security import UnsafeUrlError, validate_external_url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
        "http://192.168.1.1/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://[::1]/",
        "ftp://public.example/file",
        "",
        "not-a-url",
    ],
)
def test_validate_external_url_rejects_unsafe(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_external_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://generativelanguage.googleapis.com/",
        "http://public.example.com:8080/path",
    ],
)
def test_validate_external_url_allows_public(url: str) -> None:
    validate_external_url(url)  # should not raise


def test_validate_external_url_allows_private_when_requested() -> None:
    validate_external_url("http://127.0.0.1:11434", allow_private=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254./latest/meta-data/",  # 尾点变体
        "http://2852039166/",  # 十进制整数 = 169.254.169.254
        "http://0xa9fea9fe/",  # 十六进制 = 169.254.169.254
        "http://[::ffff:169.254.169.254]/",  # v4-mapped v6
        "http://127.1/",  # 短写回环
        "http://2130706433/",  # 十进制整数 = 127.0.0.1
        "http://0x7f.0x0.0x0.0x1/",  # 分段十六进制回环
        "http://10.1/",  # 短写私有
    ],
)
def test_validate_external_url_rejects_nonstandard_ip_encodings(url: str) -> None:
    """非标准 IP 编码（尾点/短写/整数/hex/v4-mapped）必须按归一化后语义拒绝——
    回归：此前 ipaddress 解析失败即放行，SSRF 绕过直达元数据服务。"""
    with pytest.raises(UnsafeUrlError):
        validate_external_url(url)


def test_validate_external_url_rejects_metadata_even_when_private_allowed() -> None:
    with pytest.raises(UnsafeUrlError):
        validate_external_url("http://169.254.169.254/", allow_private=True)
