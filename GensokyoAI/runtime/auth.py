"""Runtime request identity and role-based authorization."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from GensokyoAI.runtime.rpc import RpcError

RUNTIME_ROLES = frozenset({"read", "chat", "admin"})


@dataclass(frozen=True, slots=True)
class RuntimePrincipal:
    """Authenticated Runtime caller."""

    user_id: str
    roles: frozenset[str]
    auth_type: str
    network: bool = True

    def has_role(self, role: str) -> bool:
        return "admin" in self.roles or role in self.roles


LOCAL_PRINCIPAL = RuntimePrincipal(
    user_id="local",
    roles=RUNTIME_ROLES,
    auth_type="local",
    network=False,
)
_principal_var: ContextVar[RuntimePrincipal] = ContextVar(
    "runtime_principal",
    default=LOCAL_PRINCIPAL,
)


def current_principal() -> RuntimePrincipal:
    return _principal_var.get()


def set_current_principal(principal: RuntimePrincipal) -> Token[RuntimePrincipal]:
    return _principal_var.set(principal)


def reset_current_principal(token: Token[RuntimePrincipal]) -> None:
    _principal_var.reset(token)


def decode_hs256_jwt(
    token: str,
    secret: str,
    *,
    issuer: str | None = None,
    audience: str | None = None,
    now: float | None = None,
) -> RuntimePrincipal:
    """Validate a gateway-issued HS256 JWT and derive a Runtime principal."""

    parts = token.split(".")
    if len(parts) != 3:
        raise _unauthorized("Runtime bearer token is not a JWT")
    header_segment, payload_segment, signature_segment = parts
    try:
        header = _decode_json_segment(header_segment)
        claims = _decode_json_segment(payload_segment)
        supplied_signature = _decode_segment(signature_segment)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _unauthorized("Runtime bearer token is malformed") from error

    if header.get("alg") != "HS256" or header.get("typ", "JWT") != "JWT":
        raise _unauthorized("Runtime JWT must use HS256")
    signed = f"{header_segment}.{payload_segment}".encode("ascii")
    expected_signature = hmac.new(secret.encode(), signed, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise _unauthorized("Runtime JWT signature is invalid")

    timestamp = time.time() if now is None else now
    _validate_time_claim(claims, "exp", timestamp, before=False, required=True)
    _validate_time_claim(claims, "nbf", timestamp, before=True, required=False)
    if issuer is not None and claims.get("iss") != issuer:
        raise _unauthorized("Runtime JWT issuer is invalid")
    if audience is not None and not _matches_audience(claims.get("aud"), audience):
        raise _unauthorized("Runtime JWT audience is invalid")

    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        raise _unauthorized("Runtime JWT subject is required")
    if len(user_id.strip()) > 256:
        raise _unauthorized("Runtime JWT subject is too long")
    roles = _normalize_roles(claims.get("roles", claims.get("role", ["read", "chat"])))
    if not roles:
        raise _unauthorized("Runtime JWT contains no supported roles")
    return RuntimePrincipal(
        user_id=user_id.strip(),
        roles=frozenset(roles),
        auth_type="jwt",
    )


def authorize_rpc(method: str, principal: RuntimePrincipal) -> None:
    required = required_role(method)
    if principal.has_role(required):
        return
    raise RpcError(
        f"Role '{required}' is required for Runtime method '{method}'",
        code="authorization.forbidden",
        user_message="当前身份没有执行此操作的权限。",
        recoverable=False,
        action_hint="请使用具有所需角色的身份重新认证。",
        details={"required_role": required, "roles": sorted(principal.roles)},
    )


def required_role(method: str) -> str:
    if method in {"runtime.shutdown", "shutdown"}:
        return "admin"
    if method in {"dependency.status", "external_tool.status"}:
        return "read"
    if method == "message.status":
        return "read"
    if method.startswith(("dependency.", "character_package.", "external_tool.")):
        return "admin"
    if method == "media.delete":
        return "chat"
    if method == "media.list":
        return "read"
    if method in {
        "agent.delete",
    }:
        return "admin"
    if method.startswith("world."):
        # world 命名空间必须显式分支：fallthrough 默认 admin 会把
        # 所有 world.* 变成仅管理员可用
        if method in {
            "world.state",
            "world.roster",
            "world.transcript",
            "world.session.list",
            "world.session.export",
        }:
            return "read"
        return "chat"
    if method.startswith(("agent.", "session.", "initiative_timer.")):
        read_suffixes = (".list", ".current", ".messages", ".export", ".status")
        if method == "initiative_timer.hesitation":
            return "read"
        return "read" if method.endswith(read_suffixes) else "chat"
    if method in {
        "send_message",
        "send_message_stream",
        "create_session",
        "resume_session",
        "delete_session",
        "rename_session",
        "rollback_session",
    }:
        return "chat"
    if method in {"list_sessions", "current_session", "export_session"}:
        return "read"
    if method in {"memory.update", "memory.delete", "scene.switch"}:
        return "chat"
    if method.startswith("runtime.") or method.startswith(
        ("character.", "model.", "config.", "migration.", "memory.", "scene.")
    ):
        return "read"
    return "admin"


def _decode_json_segment(value: str) -> dict[str, Any]:
    decoded = json.loads(_decode_segment(value))
    if not isinstance(decoded, dict):
        raise ValueError("JWT segment must contain an object")
    return decoded


def _decode_segment(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _validate_time_claim(
    claims: dict[str, Any],
    name: str,
    now: float,
    *,
    before: bool,
    required: bool,
) -> None:
    value = claims.get(name)
    if value is None and not required:
        return
    if not isinstance(value, int | float):
        raise _unauthorized(f"Runtime JWT claim '{name}' is invalid")
    invalid = now < value if before else now >= value
    if invalid:
        reason = "not active" if before else "expired"
        raise _unauthorized(f"Runtime JWT is {reason}")


def _matches_audience(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return hmac.compare_digest(value, expected)
    if isinstance(value, list):
        return any(isinstance(item, str) and hmac.compare_digest(item, expected) for item in value)
    return False


def _normalize_roles(value: Any) -> set[str]:
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, list):
        values = [item for item in value if isinstance(item, str)]
    else:
        values = []
    return {role for role in values if role in RUNTIME_ROLES}


def _unauthorized(message: str) -> RpcError:
    return RpcError(
        message,
        code="authentication.invalid_token",
        user_message="Runtime 身份凭据无效或已过期。",
        recoverable=True,
        action_hint="请重新登录后获取新的访问令牌。",
    )
