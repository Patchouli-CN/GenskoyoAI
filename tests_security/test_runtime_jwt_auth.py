from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from GensokyoAI.runtime.auth import authorize_rpc, decode_hs256_jwt
from GensokyoAI.runtime.rpc import RpcError


def _jwt(secret: str, claims: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = encode({"alg": "HS256", "typ": "JWT"})
    payload = encode(claims)
    signature = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def test_gateway_jwt_derives_stable_principal_and_roles() -> None:
    secret = "s" * 32
    token = _jwt(
        secret,
        {
            "sub": "user-42",
            "roles": ["read", "chat", "unsupported"],
            "iss": "gateway",
            "aud": "gensokyo-runtime",
            "exp": 2000,
        },
    )

    principal = decode_hs256_jwt(
        token,
        secret,
        issuer="gateway",
        audience="gensokyo-runtime",
        now=1000,
    )

    assert principal.user_id == "user-42"
    assert principal.roles == frozenset({"read", "chat"})
    authorize_rpc("agent.send_message", principal)
    with pytest.raises(RpcError, match="admin"):
        authorize_rpc("runtime.shutdown", principal)


def test_gateway_jwt_rejects_expired_or_modified_token() -> None:
    secret = "s" * 32
    expired = _jwt(secret, {"sub": "user", "roles": ["read"], "exp": 10})
    with pytest.raises(RpcError, match="expired"):
        decode_hs256_jwt(expired, secret, now=11)

    modified = expired[:-1] + ("A" if expired[-1] != "A" else "B")
    with pytest.raises(RpcError, match="signature"):
        decode_hs256_jwt(modified, secret, now=1)
