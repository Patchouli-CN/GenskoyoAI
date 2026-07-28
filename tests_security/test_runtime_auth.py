"""Web Runtime 认证与限流相关安全测试。"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from GensokyoAI.backends.web_server.http_adapter import (
    RUNTIME_AUTH_RATE_LIMIT_APP_KEY,
    create_app,
)
from GensokyoAI.backends.web_server.main import _is_loopback_host


class _FakeRuntimeService:
    def __init__(self) -> None:
        self.shutdown_called = False

    async def health(self) -> dict[str, Any]:
        return {"ok": True}

    async def info(self) -> dict[str, Any]:
        return {"name": "fake"}

    async def handle(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return {"method": method, "params": params or {}}

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture
def fake_service() -> _FakeRuntimeService:
    return _FakeRuntimeService()


@pytest.mark.asyncio
async def test_short_auth_token_rejected(fake_service: _FakeRuntimeService) -> None:
    with pytest.raises(RuntimeError, match="at least"):
        create_app(service=fake_service, auth_token="short")


@pytest.mark.asyncio
async def test_empty_auth_token_means_disabled(fake_service: _FakeRuntimeService) -> None:
    server = TestServer(create_app(service=fake_service, auth_token=""))
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get("/health")
        assert response.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_query_string_token_is_ignored(fake_service: _FakeRuntimeService) -> None:
    server = TestServer(create_app(service=fake_service, auth_token="supersecrettoken123"))
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get("/health?token=supersecrettoken123")
        assert response.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_header_token_is_accepted(fake_service: _FakeRuntimeService) -> None:
    server = TestServer(create_app(service=fake_service, auth_token="supersecrettoken123"))
    client = TestClient(server)
    await client.start_server()
    try:
        bearer = await client.get(
            "/health", headers={"Authorization": "Bearer supersecrettoken123"}
        )
        custom = await client.get("/health", headers={"X-Runtime-Token": "supersecrettoken123"})
        assert bearer.status == 200
        assert custom.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_auth_rate_limit(fake_service: _FakeRuntimeService) -> None:
    server = TestServer(create_app(service=fake_service, auth_token="supersecrettoken123"))
    client = TestClient(server)
    await client.start_server()
    try:
        for _ in range(10):
            response = await client.get("/health")
            assert response.status == 401

        # 第 11 次失败应当触发限流
        limited = await client.get("/health")
        assert limited.status == 429
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_not_required_when_no_token(fake_service: _FakeRuntimeService) -> None:
    server = TestServer(create_app(service=fake_service))
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get("/health")
        assert response.status == 200
    finally:
        await client.close()


def test_remote_binding_requires_auth_token(fake_service: _FakeRuntimeService) -> None:
    with pytest.raises(RuntimeError, match="required for non-loopback"):
        create_app(service=fake_service, require_auth=True)


def test_builtin_server_requires_auth_for_non_loopback_hosts() -> None:
    assert _is_loopback_host("127.0.0.1")
    assert _is_loopback_host("::1")
    assert _is_loopback_host("localhost")
    assert not _is_loopback_host("0.0.0.0")
    assert not _is_loopback_host("::")


@pytest.mark.asyncio
async def test_successful_auth_clears_peer_failures(fake_service: _FakeRuntimeService) -> None:
    app = create_app(service=fake_service, auth_token="supersecrettoken123")
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        failed = await client.get("/health")
        assert failed.status == 401
        assert app[RUNTIME_AUTH_RATE_LIMIT_APP_KEY]

        allowed = await client.get(
            "/health", headers={"Authorization": "Bearer supersecrettoken123"}
        )
        assert allowed.status == 200
        assert app[RUNTIME_AUTH_RATE_LIMIT_APP_KEY] == {}
    finally:
        await client.close()
