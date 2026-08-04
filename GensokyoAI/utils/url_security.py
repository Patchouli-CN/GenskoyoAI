"""URL 安全校验工具：防止 SSRF 等通过用户可控 URL 访问内网或元数据服务。

提供同步和异步两个版本：
- validate_external_url(): 同步版本，用于配置校验等同步上下文
- validate_external_url_async(): 异步版本，用于 runtime HTTP 等异步上下文，使用 aiohttp AsyncResolver
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """URL 未通过安全校验时抛出。"""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"不安全的 URL {url!r}: {reason}")
        self.url = url
        self.reason = reason


# 明确禁止的主机名（大小写不敏感）：元数据、通配符、空主机名永远不允许
_ALWAYS_FORBIDDEN_HOSTNAMES = frozenset(
    {
        "169.254.169.254",  # AWS / 云厂商 metadata
        "metadata.google.internal",
        "metadata",
        "*",
        "",
    }
)

# 回环主机名：默认禁止，但 allow_private=True 时可放行（本地 Ollama 场景）
_LOOPBACK_HOSTNAMES = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::",
        "::1",
    }
)

# 永远禁止解析到的网段：链路本地 / 元数据 / 0.0.0.0
_ALWAYS_FORBIDDEN_NETWORKS = frozenset(
    {
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    }
)

# 回环网段：默认禁止，allow_private=True 时可放行
_LOOPBACK_NETWORKS = frozenset(
    {
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    }
)

# 私有网段：默认禁止，allow_private=True 时可放行
_PRIVATE_NETWORKS = frozenset(
    {
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
        ipaddress.ip_network("fc00::/7"),  # IPv6 ULA
    }
)


_ALL_FORBIDDEN_NETWORKS = _ALWAYS_FORBIDDEN_NETWORKS | _LOOPBACK_NETWORKS | _PRIVATE_NETWORKS


def _coerce_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把主机名归一化为 IP 对象；非 IP 字面量返回 None。

    除标准写法外，按 inet_aton 语义归一化非标准变体——不处理这些会被
    SSRF 绕过（`ipaddress.ip_address` 解析失败即放行）：
    - 尾点：`169.254.169.254.`
    - 短写回环/短写：`127.1`、`10.1`
    - 单整数（十进制/十六进制/八进制）：`2852039166`、`0xa9fea9fe`、`0251`
    - v4-mapped v6：`::ffff:169.254.169.254`
    """
    candidate = host.strip().rstrip(".")
    # v6 / v4-mapped v6
    try:
        addr6 = ipaddress.IPv6Address(candidate)
        return addr6.ipv4_mapped or addr6
    except ValueError:
        pass
    # v4 标准形
    try:
        return ipaddress.IPv4Address(candidate)
    except ValueError:
        pass
    # inet_aton：1~4 段，每段十进制/0x 十六进制/前导 0 八进制
    parts = candidate.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            value = int(part, 0)
        except ValueError:
            try:
                value = int(part, 8) if part.startswith("0") else -1
            except ValueError:
                return None
        if value < 0:
            return None
        numbers.append(value)
    # 除最后一段外每段占 1 字节；最后一段占剩余字节
    if any(value > 0xFF for value in numbers[:-1]):
        return None
    remaining_bytes = 4 - (len(numbers) - 1)
    if numbers[-1] > (1 << (8 * remaining_bytes)) - 1:
        return None
    packed = 0
    for value in numbers[:-1]:
        packed = (packed << 8) | value
    packed = (packed << (8 * remaining_bytes)) | numbers[-1]
    return ipaddress.IPv4Address(packed)


def _is_private_ip(host: str) -> bool:
    """判断主机名是否为私有网段 IP。"""

    addr = _coerce_ip(host)
    if addr is None:
        return False

    return any(addr in network for network in _PRIVATE_NETWORKS)


def _is_loopback_ip(host: str) -> bool:
    """判断主机名是否为回环 IP。"""

    addr = _coerce_ip(host)
    if addr is None:
        return False

    return any(addr in network for network in _LOOPBACK_NETWORKS)


def _is_always_forbidden_ip(host: str) -> bool:
    """判断主机名是否为永远禁止的 IP（链路本地、元数据、0.0.0.0）。"""

    addr = _coerce_ip(host)
    if addr is None:
        return False

    return any(addr in network for network in _ALWAYS_FORBIDDEN_NETWORKS)


def validate_external_url(
    url: str,
    *,
    allow_private: bool = False,
    resolve_dns: bool = False,
) -> None:
    """校验外部 URL 是否安全。

    默认禁止：
    - 非 http/https 协议
    - localhost / 127.0.0.1 / 0.0.0.0 / ::1 等回环地址
    - 私有网段（10/8、172.16/12、192.168/16 等）
    - 链路本地 / 元数据服务（169.254.169.254）
    - 空主机名或通配符

    Args:
        url: 待校验 URL。
        allow_private: 若为 True，则允许私有 IP（但仍禁止元数据和回环）。
            用于本地 Ollama 等场景。
        resolve_dns: 是否解析域名并检查解析结果。默认 False，避免在配置校验
            等路径引入网络调用；开启后可防御"域名指向内网"场景。

    Raises:
        UnsafeUrlError: 校验失败。
    """

    if not isinstance(url, str) or not url:
        raise UnsafeUrlError(str(url), "URL 不能为空")

    # 去除首尾空白，防御简单绕过
    url = url.strip()
    parsed = urlparse(url)

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError(url, f"不支持的协议: {scheme!r}")

    hostname = parsed.hostname
    if hostname is None:
        raise UnsafeUrlError(url, "无法解析主机名")

    hostname_lower = hostname.lower()
    if hostname_lower in _ALWAYS_FORBIDDEN_HOSTNAMES:
        raise UnsafeUrlError(url, f"禁止的主机名: {hostname!r}")

    if not allow_private and (hostname_lower in _LOOPBACK_HOSTNAMES or _is_loopback_ip(hostname)):
        raise UnsafeUrlError(url, f"禁止的回环主机名: {hostname!r}")

    if _is_always_forbidden_ip(hostname):
        raise UnsafeUrlError(url, f"禁止的 IP 地址: {hostname!r}")

    if not allow_private and _is_private_ip(hostname):
        raise UnsafeUrlError(url, f"禁止的私有 IP 地址: {hostname!r}")

    if resolve_dns and not _is_always_forbidden_ip(hostname):
        try:
            resolved = socket.getaddrinfo(hostname, None)
            addresses = {str(item[4][0]) for item in resolved}
            for addr in addresses:
                if _is_always_forbidden_ip(addr):
                    raise UnsafeUrlError(url, f"域名解析到禁止地址: {addr!r}")
                if not allow_private and (_is_loopback_ip(addr) or _is_private_ip(addr)):
                    raise UnsafeUrlError(url, f"域名解析到非安全地址: {addr!r}")
        except UnsafeUrlError:
            raise
        except Exception:
            # DNS 解析失败时保守拒绝
            raise UnsafeUrlError(url, "无法解析域名或解析到非安全地址") from None


def is_safe_public_url(url: str) -> bool:
    """安全返回 bool 的便捷封装（同步版本）。"""

    try:
        validate_external_url(url)
        return True
    except UnsafeUrlError:
        return False


async def is_safe_public_url_async(
    url: str, *, allow_private: bool = False, resolve_dns: bool = False
) -> bool:
    """安全返回 bool 的便捷封装（异步版本）。"""

    try:
        await validate_external_url_async(url, allow_private=allow_private, resolve_dns=resolve_dns)
        return True
    except UnsafeUrlError:
        return False


# 延迟导入，避免循环依赖
_resolver: Any = None


async def _get_resolver() -> Any:
    """获取或创建异步 DNS 解析器（延迟初始化）。"""
    global _resolver
    if _resolver is None:
        from aiohttp.resolver import AsyncResolver

        _resolver = AsyncResolver()
    return _resolver


async def _close_resolver() -> None:
    """关闭异步 DNS 解析器（用于清理）。"""
    global _resolver
    if _resolver is not None:
        await _resolver.close()
        _resolver = None


async def validate_external_url_async(
    url: str,
    *,
    allow_private: bool = False,
    resolve_dns: bool = False,
) -> None:
    """异步版本：校验外部 URL 是否安全。

    与同步版本行为一致，但使用 aiohttp AsyncResolver 进行异步 DNS 解析，
    避免阻塞事件循环。适用于 runtime HTTP 等高并发场景。

    Args:
        url: 待校验 URL。
        allow_private: 若为 True，则允许私有 IP（但仍禁止元数据和回环）。
        resolve_dns: 是否解析域名并检查解析结果。默认 False；开启后可防御
            "DNS Rebinding" 攻击（域名指向内网）。

    Raises:
        UnsafeUrlError: 校验失败。
    """
    if not isinstance(url, str) or not url:
        raise UnsafeUrlError(str(url), "URL 不能为空")

    # 去除首尾空白，防御简单绕过
    url = url.strip()
    parsed = urlparse(url)

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError(url, f"不支持的协议: {scheme!r}")

    hostname = parsed.hostname
    if hostname is None:
        raise UnsafeUrlError(url, "无法解析主机名")

    hostname_lower = hostname.lower()
    if hostname_lower in _ALWAYS_FORBIDDEN_HOSTNAMES:
        raise UnsafeUrlError(url, f"禁止的主机名: {hostname!r}")

    if not allow_private and (hostname_lower in _LOOPBACK_HOSTNAMES or _is_loopback_ip(hostname)):
        raise UnsafeUrlError(url, f"禁止的回环主机名: {hostname!r}")

    if _is_always_forbidden_ip(hostname):
        raise UnsafeUrlError(url, f"禁止的 IP 地址: {hostname!r}")

    if not allow_private and _is_private_ip(hostname):
        raise UnsafeUrlError(url, f"禁止的私有 IP 地址: {hostname!r}")

    # 异步 DNS 解析（使用 aiohttp AsyncResolver）
    if resolve_dns and not _is_always_forbidden_ip(hostname):
        resolver = await _get_resolver()
        try:
            # AsyncResolver.resolve 返回的是 [{"host": ip, "port": port, "family": family, ...}]
            hosts = await resolver.resolve(hostname, 80)
            addresses = {str(h["host"]) for h in hosts}
            for addr in addresses:
                if _is_always_forbidden_ip(addr):
                    raise UnsafeUrlError(url, f"域名解析到禁止地址: {addr!r}")
                if not allow_private and (_is_loopback_ip(addr) or _is_private_ip(addr)):
                    raise UnsafeUrlError(url, f"域名解析到非安全地址: {addr!r}")
        except UnsafeUrlError:
            raise
        except Exception as exc:
            # DNS 解析失败时保守拒绝
            raise UnsafeUrlError(url, f"无法解析域名或解析到非安全地址: {exc}") from None


__all__ = [
    "UnsafeUrlError",
    "is_safe_public_url",
    "is_safe_public_url_async",
    "validate_external_url",
    "validate_external_url_async",
    "_close_resolver",  # 用于清理
]
