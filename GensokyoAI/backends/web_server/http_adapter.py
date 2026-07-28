"""HTTP and WebSocket Runtime adapter built on aiohttp.

This module exposes the frontend-agnostic RuntimeService through network
transports without coupling clients to Agent internals.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import math
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from uuid import uuid4

from aiohttp import WSMsgType, web
from aiohttp.multipart import BodyPartReader
from msgspec import Struct

from GensokyoAI.runtime.auth import (
    RUNTIME_ROLES,
    RuntimePrincipal,
    authorize_rpc,
    decode_hs256_jwt,
    set_current_principal,
)
from GensokyoAI.runtime.rpc import (
    RpcError,
    remote_admin_rpc_methods,
    runtime_error_to_dict,
)
from GensokyoAI.runtime.service import RuntimeService
from GensokyoAI.utils.helpers import utc_now

RUNTIME_SERVICE_APP_KEY: web.AppKey[RuntimeService] = web.AppKey(
    "runtime_service",
    RuntimeService,
)

DEFAULT_WS_HEARTBEAT_INTERVAL = 30.0
MIN_WS_HEARTBEAT_INTERVAL = 5.0
MAX_WS_HEARTBEAT_INTERVAL = 120.0
DEFAULT_MAX_REQUEST_BODY_SIZE = 1024 * 1024
DEFAULT_MAX_MEDIA_SIZE = 10 * 1024 * 1024
DEFAULT_WS_MAX_MSG_SIZE = 1024 * 1024
MIN_AUTH_TOKEN_LENGTH = 16
AUTH_RATE_LIMIT_MAX_FAILURES = 10
AUTH_RATE_LIMIT_WINDOW_SECONDS = 60.0
AUTH_RATE_LIMIT_MAX_PEERS = 10_000
DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0


class RuntimeHttpSecurityConfig(Struct, frozen=True):
    token: str | None = None
    jwt_secret: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    allowed_origins: tuple[str, ...] = ()
    allow_all_origins: bool = False
    max_request_body_size: int = DEFAULT_MAX_REQUEST_BODY_SIZE
    max_media_size: int = DEFAULT_MAX_MEDIA_SIZE
    allow_remote_admin: bool = False
    drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS

    @property
    def auth_enabled(self) -> bool:
        # 空字符串视为未启用认证，避免 compare_digest("", "") == True 的绕过
        return bool(self.token or self.jwt_secret)


RUNTIME_SECURITY_APP_KEY: web.AppKey[RuntimeHttpSecurityConfig] = web.AppKey(
    "runtime_http_security",
    RuntimeHttpSecurityConfig,
)

RUNTIME_AUTH_RATE_LIMIT_APP_KEY: web.AppKey[dict[str, tuple[int, float]]] = web.AppKey(
    "runtime_auth_rate_limit",
    dict,
)


def json_default(value: Any) -> str:
    return str(value)


def json_response(payload: dict[str, Any], *, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=_json_dumps)


def rpc_success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result}


def rpc_error(request_id: Any, error: Exception) -> dict[str, Any]:
    error_object = runtime_error_to_dict(error)
    if isinstance(error, web.HTTPException):
        error_object.update(
            {
                "code": f"http.{error.status}",
                "error_code": f"http.{error.status}",
                "message": error.reason,
                "error": error.reason,
                "technical_message": error.reason,
                "user_message": error.reason,
                "recoverable": error.status in {401, 403, 408, 409, 429},
                "details": {"http_status": error.status},
            }
        )
    return {
        "id": request_id,
        "ok": False,
        "error": error_object,
    }


def parse_rpc_payload(payload: Any) -> tuple[Any, str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("RPC request payload must be an object")
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params", {})
    if not isinstance(method, str):
        raise ValueError("RPC request field 'method' must be a string")
    if not isinstance(params, dict):
        raise ValueError("RPC request field 'params' must be an object")
    return request_id, method, params


def _require_remote_admin_enabled(
    method: str,
    security: RuntimeHttpSecurityConfig,
) -> None:
    if security.allow_remote_admin or method not in remote_admin_rpc_methods():
        return
    raise RpcError(
        f"Remote Runtime administration is disabled for method '{method}'",
        code="authorization.remote_admin_disabled",
        user_message="当前网络入口未启用远程管理操作。",
        recoverable=False,
        action_hint="请在受信任部署中显式启用远程管理，或改用本机管理入口。",
        details={"method": method},
    )


def _normalize_token(value: str | None) -> str | None:
    """把空字符串规范化为 None，避免空 token 被误认为启用认证。"""

    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else None
    if scheme not in {"http", "https", "ws", "wss"} or host is None:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if parsed.path not in {"", "/"}:
        return None
    if port is None:
        port = 443 if scheme in {"https", "wss"} else 80
    return scheme, host, port


@web.middleware
async def _cors_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    origin = request.headers.get("Origin")
    if origin:
        _validate_origin(request, request.app[RUNTIME_SECURITY_APP_KEY])
    try:
        response = await handler(request)
    except web.HTTPException as error:
        if origin:
            _apply_cors_headers(error, origin, request.app[RUNTIME_SECURITY_APP_KEY])
        raise
    if origin:
        _apply_cors_headers(response, origin, request.app[RUNTIME_SECURITY_APP_KEY])
    return response


def _apply_cors_headers(
    response: web.StreamResponse,
    origin: str,
    security: RuntimeHttpSecurityConfig,
) -> None:
    response.headers["Access-Control-Allow-Origin"] = "*" if security.allow_all_origins else origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, Last-Event-ID, X-Runtime-Token"
    )
    response.headers["Vary"] = "Origin"


def create_app(
    root_dir: Path | None = None,
    *,
    service: RuntimeService | None = None,
    auth_token: str | None = None,
    jwt_secret: str | None = None,
    jwt_issuer: str | None = None,
    jwt_audience: str | None = None,
    allowed_origins: list[str] | tuple[str, ...] | None = None,
    allow_all_origins: bool = False,
    require_auth: bool = False,
    max_request_body_size: int = DEFAULT_MAX_REQUEST_BODY_SIZE,
    max_media_size: int = DEFAULT_MAX_MEDIA_SIZE,
    allow_remote_admin: bool | None = None,
    drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
) -> web.Application:
    token = _normalize_token(auth_token or os.environ.get("GENSOKYOAI_RUNTIME_TOKEN"))
    jwt_secret = _normalize_token(jwt_secret or os.environ.get("GENSOKYOAI_RUNTIME_JWT_SECRET"))
    jwt_issuer = _normalize_token(jwt_issuer or os.environ.get("GENSOKYOAI_RUNTIME_JWT_ISSUER"))
    jwt_audience = _normalize_token(
        jwt_audience or os.environ.get("GENSOKYOAI_RUNTIME_JWT_AUDIENCE")
    )
    if allow_remote_admin is None:
        allow_remote_admin = _env_flag("GENSOKYOAI_RUNTIME_ALLOW_REMOTE_ADMIN")
    if require_auth and token is None and jwt_secret is None:
        raise RuntimeError(
            "Runtime authentication is required for non-loopback binding; "
            "set GENSOKYOAI_RUNTIME_JWT_SECRET or GENSOKYOAI_RUNTIME_TOKEN"
        )
    if token is not None and len(token) < MIN_AUTH_TOKEN_LENGTH:
        raise RuntimeError(
            f"Runtime auth token must be at least {MIN_AUTH_TOKEN_LENGTH} characters"
        )
    if jwt_secret is not None and len(jwt_secret) < 32:
        raise RuntimeError("Runtime JWT secret must be at least 32 characters")

    origins = tuple(allowed_origins or ())
    if origins and "*" in origins:
        allow_all_origins = True
        origins = ()

    app = web.Application(
        client_max_size=max(max_request_body_size, max_media_size),
        middlewares=[_cors_middleware],
    )
    app[RUNTIME_SERVICE_APP_KEY] = service or RuntimeService(root_dir=root_dir)
    app[RUNTIME_SECURITY_APP_KEY] = RuntimeHttpSecurityConfig(
        token=token,
        jwt_secret=jwt_secret,
        jwt_issuer=jwt_issuer,
        jwt_audience=jwt_audience,
        allowed_origins=origins,
        allow_all_origins=allow_all_origins,
        max_request_body_size=max_request_body_size,
        max_media_size=max_media_size,
        allow_remote_admin=allow_remote_admin,
        drain_timeout_seconds=max(0.0, float(drain_timeout_seconds)),
    )
    app[RUNTIME_AUTH_RATE_LIMIT_APP_KEY] = {}
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ready", handle_ready)
    app.router.add_get("/info", handle_info)
    app.router.add_post("/rpc", handle_rpc)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/events", handle_events)
    app.router.add_post("/media", handle_media_upload)
    app.router.add_get("/media/{agent_id}/{media_id}", handle_media_download)
    app.router.add_post("/character-packages", handle_character_package_upload)
    app.router.add_options("/{path:.*}", handle_options)
    app.on_shutdown.append(drain_runtime_service)
    app.on_cleanup.append(cleanup_runtime_service)
    return app


async def cleanup_runtime_service(app: web.Application) -> None:
    await app[RUNTIME_SERVICE_APP_KEY].shutdown()


async def drain_runtime_service(app: web.Application) -> None:
    security = app[RUNTIME_SECURITY_APP_KEY]
    wait_for_drain = getattr(app[RUNTIME_SERVICE_APP_KEY], "wait_for_drain", None)
    if callable(wait_for_drain):
        await cast(Callable[[float], Awaitable[bool]], wait_for_drain)(
            security.drain_timeout_seconds
        )


async def handle_health(request: web.Request) -> web.Response:
    principal = _validate_runtime_request(request)
    authorize_rpc("runtime.health", principal)
    result = await request.app[RUNTIME_SERVICE_APP_KEY].health()
    return json_response(result)


async def handle_ready(request: web.Request) -> web.Response:
    principal = _validate_runtime_request(request)
    authorize_rpc("runtime.ready", principal)
    result = await request.app[RUNTIME_SERVICE_APP_KEY].readiness()
    return json_response(result, status=200 if result.get("ready") else 503)


def _attach_active_transport(
    result: dict[str, Any],
    security: RuntimeHttpSecurityConfig,
) -> dict[str, Any]:
    return {
        **result,
        "active_transport": {
            "name": "http-websocket",
            "authentication": (
                "jwt-hs256"
                if security.jwt_secret
                else "shared-bearer"
                if security.token
                else "none"
            ),
            "cors": "allow-all" if security.allow_all_origins else "allowlist",
            "max_request_body_size": security.max_request_body_size,
            "max_media_size": security.max_media_size,
            "remote_admin_enabled": security.allow_remote_admin,
            "disabled_methods": (
                [] if security.allow_remote_admin else sorted(remote_admin_rpc_methods())
            ),
            "max_websocket_message_size": DEFAULT_WS_MAX_MSG_SIZE,
            "websocket_heartbeat_seconds": {
                "default": DEFAULT_WS_HEARTBEAT_INTERVAL,
                "minimum": MIN_WS_HEARTBEAT_INTERVAL,
                "maximum": MAX_WS_HEARTBEAT_INTERVAL,
            },
        },
    }


async def handle_info(request: web.Request) -> web.Response:
    principal = _validate_runtime_request(request)
    authorize_rpc("runtime.info", principal)
    result = await request.app[RUNTIME_SERVICE_APP_KEY].info()
    security = request.app[RUNTIME_SECURITY_APP_KEY]
    result = _attach_active_transport(result, security)
    return json_response(result)


async def handle_rpc(request: web.Request) -> web.Response:
    request_id: Any = None
    try:
        _validate_runtime_request(request)
        security = request.app[RUNTIME_SECURITY_APP_KEY]
        if request.content_length and request.content_length > security.max_request_body_size:
            raise web.HTTPRequestEntityTooLarge(
                max_size=security.max_request_body_size,
                actual_size=request.content_length,
            )
        payload = await request.json()
        request_id, method, params = parse_rpc_payload(payload)
        authorize_rpc(method, _validate_runtime_request(request))
        _require_remote_admin_enabled(method, security)
        result = await request.app[RUNTIME_SERVICE_APP_KEY].handle(method, params)
        if method == "runtime.info" and isinstance(result, dict):
            result = _attach_active_transport(result, security)
        return json_response(rpc_success(request_id, result))
    except web.HTTPException as error:
        return json_response(rpc_error(request_id, error), status=error.status)
    except Exception as error:
        status = 400 if request_id is None else 200
        return json_response(rpc_error(request_id, error), status=status)


async def handle_options(request: web.Request) -> web.Response:
    _validate_origin(request, request.app[RUNTIME_SECURITY_APP_KEY])
    return web.Response(status=204)


async def handle_events(request: web.Request) -> web.StreamResponse:
    principal = _validate_runtime_request(request)
    authorize_rpc("runtime.subscribe", principal)
    service = request.app[RUNTIME_SERVICE_APP_KEY]
    subscription = await service.create_event_subscription(
        **_event_subscription_params_from_request(request)
    )
    subscription_id = subscription["subscription_id"]
    queue = subscription["queue"]
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    try:
        while True:
            event = await queue.get()
            try:
                await response.write(_sse_frame("runtime.event", event))
                # drain() is deprecated in aiohttp 3.8+, write() already handles buffering
            finally:
                queue.task_done()
    except asyncio.CancelledError, ConnectionResetError, RuntimeError:
        pass
    finally:
        with contextlib.suppress(Exception):
            await service.close_event_subscription(subscription_id)

    return response


async def handle_media_upload(request: web.Request) -> web.Response:
    principal = _validate_runtime_request(request)
    authorize_rpc("agent.send_message", principal)
    agent_id = request.query.get("agent_id")
    if not agent_id:
        raise web.HTTPBadRequest(reason="Runtime media upload requires agent_id")
    reader = await request.multipart()
    part = await reader.next()
    if not isinstance(part, BodyPartReader) or part.name != "file" or not part.filename:
        raise web.HTTPBadRequest(reason="Runtime media upload requires multipart field 'file'")
    max_size = request.app[RUNTIME_SECURITY_APP_KEY].max_media_size
    data = bytearray()
    while chunk := await part.read_chunk():
        data.extend(chunk)
        if len(data) > max_size:
            raise web.HTTPRequestEntityTooLarge(max_size=max_size, actual_size=len(data))
    try:
        result = await request.app[RUNTIME_SERVICE_APP_KEY].upload_media(
            agent_id,
            bytes(data),
            filename=part.filename,
            content_type=part.headers.get("Content-Type", "application/octet-stream"),
        )
    except (RpcError, ValueError) as error:
        return json_response(rpc_error(None, error), status=400)
    result["download_path"] = f"/media/{agent_id}/{result['media_id']}"
    return json_response(result, status=201)


async def handle_media_download(request: web.Request) -> web.Response:
    principal = _validate_runtime_request(request)
    authorize_rpc("media.list", principal)
    try:
        metadata, data = await request.app[RUNTIME_SERVICE_APP_KEY].get_media(
            request.match_info["agent_id"],
            request.match_info["media_id"],
        )
    except (RpcError, ValueError) as error:
        return json_response(rpc_error(None, error), status=404)
    return web.Response(
        body=data,
        content_type=metadata["content_type"],
        headers={"Content-Disposition": f'inline; filename="{metadata["filename"]}"'},
    )


async def handle_character_package_upload(request: web.Request) -> web.Response:
    principal = _validate_runtime_request(request)
    authorize_rpc("character_package.import", principal)
    if not request.app[RUNTIME_SECURITY_APP_KEY].allow_remote_admin:
        raise web.HTTPForbidden(reason="Runtime remote administration is disabled")
    reader = await request.multipart()
    part = await reader.next()
    if not isinstance(part, BodyPartReader) or part.name != "file" or not part.filename:
        raise web.HTTPBadRequest(reason="Character package upload requires multipart field 'file'")
    max_size = request.app[RUNTIME_SECURITY_APP_KEY].max_media_size
    data = bytearray()
    while chunk := await part.read_chunk():
        data.extend(chunk)
        if len(data) > max_size:
            raise web.HTTPRequestEntityTooLarge(max_size=max_size, actual_size=len(data))
    try:
        result = await request.app[RUNTIME_SERVICE_APP_KEY].import_uploaded_character_package(
            bytes(data),
            filename=part.filename,
            locale=request.query.get("locale"),
            overwrite=request.query.get("overwrite", "false").lower() == "true",
            allow_untrusted=request.query.get("allow_untrusted", "false").lower() == "true",
        )
    except (RpcError, ValueError) as error:
        return json_response(rpc_error(None, error), status=422)
    return json_response(result, status=201)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    _validate_runtime_request(request)
    ws = web.WebSocketResponse(max_msg_size=DEFAULT_WS_MAX_MSG_SIZE)
    await ws.prepare(request)
    service = request.app[RUNTIME_SERVICE_APP_KEY]
    send_lock = asyncio.Lock()
    subscription_tasks: dict[str, asyncio.Task[None]] = {}
    stream_tasks: dict[str, asyncio.Task[None]] = {}
    request_tasks: set[asyncio.Task[None]] = set()
    heartbeat_interval = _heartbeat_interval_from_request(request)
    heartbeat_task = asyncio.create_task(_pump_ws_heartbeat(ws, send_lock, heartbeat_interval))

    try:
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                task = asyncio.create_task(
                    _handle_ws_text(
                        ws,
                        service,
                        message.data,
                        send_lock,
                        subscription_tasks,
                        stream_tasks,
                        request.app[RUNTIME_SECURITY_APP_KEY],
                    )
                )
                request_tasks.add(task)
                task.add_done_callback(request_tasks.discard)
            elif message.type == WSMsgType.ERROR:
                break
    finally:
        heartbeat_task.cancel()
        await _await_cancelled_task(heartbeat_task)
        await _cleanup_tasks(request_tasks)
        await _cleanup_ws_streams(stream_tasks)
        await _cleanup_ws_subscriptions(service, subscription_tasks)

    return ws


async def _handle_ws_text(
    ws: web.WebSocketResponse,
    service: RuntimeService,
    data: str,
    send_lock: asyncio.Lock,
    subscription_tasks: dict[str, asyncio.Task[None]] | None = None,
    stream_tasks: dict[str, asyncio.Task[None]] | None = None,
    security: RuntimeHttpSecurityConfig | None = None,
) -> None:
    request_id: Any = None
    try:
        payload = json.loads(data)
        request_id, method, params = parse_rpc_payload(payload)
        authorize_rpc(method, _current_request_principal())
        if security is not None:
            _require_remote_admin_enabled(method, security)
        if method == "agent.send_message_stream":
            await _start_streaming_rpc_task(
                ws, service, request_id, params, send_lock, stream_tasks
            )
            return
        if method == "runtime.cancel_stream":
            result = await _cancel_streaming_rpc_task(params, stream_tasks)
            await _send_ws_json(ws, send_lock, rpc_success(request_id, result))
            return
        if method == "runtime.subscribe":
            await _start_event_subscription(
                ws,
                service,
                request_id,
                params,
                send_lock,
                subscription_tasks,
            )
            return
        if method == "runtime.unsubscribe":
            result = await _stop_event_subscription(service, params, subscription_tasks)
            await _send_ws_json(ws, send_lock, rpc_success(request_id, result))
            return

        result = await service.handle(method, params)
        if method == "runtime.info" and isinstance(result, dict) and security is not None:
            result = _attach_active_transport(result, security)
        await _send_ws_json(ws, send_lock, rpc_success(request_id, result))
    except Exception as error:
        await _send_ws_json(ws, send_lock, rpc_error(request_id, error))


async def _start_streaming_rpc_task(
    ws: web.WebSocketResponse,
    service: RuntimeService,
    request_id: Any,
    params: dict[str, Any],
    send_lock: asyncio.Lock,
    stream_tasks: dict[str, asyncio.Task[None]] | None,
) -> str:
    stream_id = str(params.pop("stream_id", None) or uuid4())
    generation_id = str(uuid4())
    if stream_tasks is not None and stream_id in stream_tasks:
        raise ValueError(f"Runtime stream already exists: {stream_id}")
    await _send_ws_json(
        ws,
        send_lock,
        rpc_success(
            request_id,
            {"stream_id": stream_id, "generation_id": generation_id},
        ),
    )
    task = asyncio.create_task(
        _send_streaming_rpc_frames(
            ws,
            service,
            request_id,
            stream_id,
            generation_id,
            params,
            send_lock,
        )
    )
    if stream_tasks is not None:
        stream_tasks[stream_id] = task
        task.add_done_callback(lambda _task: stream_tasks.pop(stream_id, None))
    return stream_id


async def _cancel_streaming_rpc_task(
    params: dict[str, Any],
    stream_tasks: dict[str, asyncio.Task[None]] | None,
) -> dict[str, Any]:
    stream_id = params.get("stream_id")
    if not isinstance(stream_id, str) or not stream_id:
        raise ValueError("Runtime stream_id is required")
    task = stream_tasks.get(stream_id) if stream_tasks is not None else None
    if task is None:
        raise ValueError(f"Runtime stream does not exist: {stream_id}")
    task.cancel()
    await _await_cancelled_task(task)
    return {"stream_id": stream_id, "cancel_requested": True, "cancelled": task.cancelled()}


async def _send_streaming_rpc_frames(
    ws: web.WebSocketResponse,
    service: RuntimeService,
    request_id: Any,
    stream_id: str,
    generation_id: str,
    params: dict[str, Any],
    send_lock: asyncio.Lock,
) -> None:
    events: list[dict[str, Any]] = []
    final_content = ""
    final_reasoning = ""
    session_payload: dict[str, Any] | None = None

    try:
        async for event in service.iter_message_stream(
            **params,
            generation_id=generation_id,
        ):
            event.setdefault("generation_id", generation_id)
            events.append(event)
            if event.get("type") == "content":
                final_content += event.get("content", "")
            if reasoning := event.get("reasoning_content"):
                final_reasoning += reasoning
            if event.get("type") == "finish":
                final_content = event.get("content", final_content)
                final_reasoning = event.get("reasoning_content", final_reasoning) or ""
                session_payload = event.get("session")
            await _send_ws_json(
                ws,
                send_lock,
                {
                    "id": request_id,
                    "ok": True,
                    "stream_id": stream_id,
                    "generation_id": generation_id,
                    "event": event,
                },
            )
    except asyncio.CancelledError:
        if not events or events[-1].get("type") != "cancelled":
            cancelled_event = {
                "type": "cancelled",
                "index": len(events),
                "content": final_content,
                "reasoning_content": final_reasoning or None,
                "generation_id": generation_id,
            }
            events.append(cancelled_event)
            await _send_ws_json(
                ws,
                send_lock,
                {
                    "id": request_id,
                    "ok": True,
                    "stream_id": stream_id,
                    "generation_id": generation_id,
                    "event": cancelled_event,
                },
            )
        return
    except Exception as error:
        if not events or events[-1].get("type") != "error":
            error_event = {
                "type": "error",
                "index": len(events),
                "content": final_content,
                "reasoning_content": final_reasoning or None,
                "generation_id": generation_id,
                "error": runtime_error_to_dict(error),
            }
            events.append(error_event)
            await _send_ws_json(
                ws,
                send_lock,
                {
                    "id": request_id,
                    "ok": True,
                    "stream_id": stream_id,
                    "generation_id": generation_id,
                    "event": error_event,
                },
            )
        await _send_ws_json(ws, send_lock, rpc_error(request_id, error))
        return

    await _send_ws_json(
        ws,
        send_lock,
        {
            "id": request_id,
            "ok": True,
            "stream_id": stream_id,
            "generation_id": generation_id,
            "done": True,
            "result": {
                "role": "assistant",
                "content": final_content,
                "reasoning_content": final_reasoning or None,
                "generation_id": generation_id,
                "events": events,
                "session": session_payload,
            },
        },
    )


async def _start_event_subscription(
    ws: web.WebSocketResponse,
    service: RuntimeService,
    request_id: Any,
    params: dict[str, Any],
    send_lock: asyncio.Lock,
    subscription_tasks: dict[str, asyncio.Task[None]] | None,
) -> None:
    subscription = await service.create_event_subscription(**params)
    subscription_id = subscription["subscription_id"]
    queue = subscription["queue"]
    if subscription_tasks is not None:
        subscription_tasks[subscription_id] = asyncio.create_task(
            _pump_event_subscription(ws, request_id, subscription_id, queue, send_lock)
        )
    result = {
        "subscription_id": subscription_id,
        "event_types": subscription["event_types"],
        "replayed_count": subscription.get("replayed_count", 0),
        "earliest_sequence": subscription.get("earliest_sequence"),
        "latest_sequence": subscription.get("latest_sequence"),
    }
    await _send_ws_json(ws, send_lock, rpc_success(request_id, result))


async def _stop_event_subscription(
    service: RuntimeService,
    params: dict[str, Any],
    subscription_tasks: dict[str, asyncio.Task[None]] | None,
) -> dict[str, Any]:
    subscription_id = params.get("subscription_id")
    if not isinstance(subscription_id, str) or not subscription_id:
        raise ValueError("Runtime event subscription_id is required")
    task = subscription_tasks.pop(subscription_id, None) if subscription_tasks is not None else None
    if task is not None:
        task.cancel()
        await _await_cancelled_task(task)
    return await service.close_event_subscription(subscription_id)


async def _pump_event_subscription(
    ws: web.WebSocketResponse,
    request_id: Any,
    subscription_id: str,
    queue: asyncio.Queue[dict[str, Any]],
    send_lock: asyncio.Lock,
) -> None:
    while True:
        event = await queue.get()
        try:
            await _send_ws_json(
                ws,
                send_lock,
                {
                    "id": request_id,
                    "ok": True,
                    "subscription_id": subscription_id,
                    "event": event,
                },
            )
        finally:
            queue.task_done()


async def _pump_ws_heartbeat(
    ws: web.WebSocketResponse,
    send_lock: asyncio.Lock,
    interval: float,
) -> None:
    while True:
        await asyncio.sleep(interval)
        await _send_ws_json(
            ws,
            send_lock,
            {
                "ok": True,
                "type": "heartbeat",
                "ts": utc_now().isoformat(),
            },
        )


async def _cleanup_ws_streams(stream_tasks: dict[str, asyncio.Task[None]]) -> None:
    for task in list(stream_tasks.values()):
        task.cancel()
    for task in list(stream_tasks.values()):
        await _await_cancelled_task(task)
    stream_tasks.clear()


async def _cleanup_tasks(tasks: set[asyncio.Task[None]]) -> None:
    for task in list(tasks):
        task.cancel()
    for task in list(tasks):
        await _await_cancelled_task(task)
    tasks.clear()


async def _cleanup_ws_subscriptions(
    service: RuntimeService,
    subscription_tasks: dict[str, asyncio.Task[None]],
) -> None:
    for subscription_id, task in list(subscription_tasks.items()):
        task.cancel()
        await _await_cancelled_task(task)
        with contextlib.suppress(Exception):
            await service.close_event_subscription(subscription_id)
    subscription_tasks.clear()


async def _await_cancelled_task(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _send_ws_json(
    ws: web.WebSocketResponse,
    send_lock: asyncio.Lock,
    payload: dict[str, Any],
) -> None:
    async with send_lock:
        await ws.send_str(_json_dumps(payload))


def _validate_origin(request: web.Request, security: RuntimeHttpSecurityConfig) -> None:
    origin = request.headers.get("Origin")
    if not origin:
        return

    if security.allow_all_origins:
        return

    normalized_origin = _normalize_origin(origin)
    if normalized_origin is None:
        raise web.HTTPForbidden(reason="Runtime request origin is not allowed")

    if not security.allowed_origins:
        # 默认未配置 allowed_origins 时，拒绝所有跨域 Origin 请求
        raise web.HTTPForbidden(reason="Runtime request origin is not allowed")

    for allowed in security.allowed_origins:
        if normalized_origin == _normalize_origin(allowed):
            return

    raise web.HTTPForbidden(reason="Runtime request origin is not allowed")


def _validate_auth_token(
    request: web.Request,
    security: RuntimeHttpSecurityConfig,
) -> RuntimePrincipal:
    if not security.auth_enabled:
        return RuntimePrincipal(
            user_id="local",
            roles=RUNTIME_ROLES,
            auth_type="loopback",
        )
    expected = security.token
    candidates = []
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        candidates.append(auth_header[len("Bearer ") :].strip())
    header_token = request.headers.get("X-Runtime-Token")
    if header_token:
        candidates.append(header_token.strip())
    for candidate in candidates:
        if expected is not None and hmac.compare_digest(candidate, expected):
            _clear_auth_failures(request)
            return RuntimePrincipal(
                user_id="runtime-admin",
                roles=RUNTIME_ROLES,
                auth_type="shared-token",
            )
        if security.jwt_secret is not None:
            try:
                principal = decode_hs256_jwt(
                    candidate,
                    security.jwt_secret,
                    issuer=security.jwt_issuer,
                    audience=security.jwt_audience,
                )
            except Exception:
                continue
            _clear_auth_failures(request)
            return principal
    _record_auth_failure(request)
    raise web.HTTPUnauthorized(reason="Runtime authentication token is required or invalid")


def _record_auth_failure(request: web.Request) -> None:
    peer = _request_peer(request)
    bucket = request.app[RUNTIME_AUTH_RATE_LIMIT_APP_KEY]
    now = time.monotonic()
    expired = [
        stored_peer
        for stored_peer, (_, started_at) in bucket.items()
        if now - started_at > AUTH_RATE_LIMIT_WINDOW_SECONDS
    ]
    for stored_peer in expired:
        bucket.pop(stored_peer, None)
    if peer not in bucket and len(bucket) >= AUTH_RATE_LIMIT_MAX_PEERS:
        oldest_peer = min(bucket, key=lambda item: bucket[item][1])
        bucket.pop(oldest_peer, None)
    count, window_start = bucket.get(peer, (0, now))
    if now - window_start > AUTH_RATE_LIMIT_WINDOW_SECONDS:
        count = 0
        window_start = now
    count += 1
    bucket[peer] = (count, window_start)
    if count > AUTH_RATE_LIMIT_MAX_FAILURES:
        raise web.HTTPTooManyRequests(reason="Too many failed authentication attempts")


def _clear_auth_failures(request: web.Request) -> None:
    request.app[RUNTIME_AUTH_RATE_LIMIT_APP_KEY].pop(_request_peer(request), None)


def _request_peer(request: web.Request) -> str:
    return request.remote or request.headers.get("X-Forwarded-For", "unknown")


def _validate_runtime_request(request: web.Request) -> RuntimePrincipal:
    security = request.app[RUNTIME_SECURITY_APP_KEY]
    # 先校验 Origin，再校验 token；避免 token 被同源策略无关地泄露
    _validate_origin(request, security)
    principal = _validate_auth_token(request, security)
    set_current_principal(principal)
    return principal


def _current_request_principal() -> RuntimePrincipal:
    from GensokyoAI.runtime.auth import current_principal

    return current_principal()


def _event_subscription_params_from_request(request: web.Request) -> dict[str, Any]:
    params: dict[str, Any] = {}
    agent_id = request.query.get("agent_id")
    after_sequence = request.query.get("after_sequence") or request.headers.get("Last-Event-ID")
    replay_limit = request.query.get("replay_limit")
    event_types = _split_query_values(request.query.getall("event_types", []))
    categories = _split_query_values(request.query.getall("categories", []))
    queue_size = request.query.get("queue_size")
    if event_types:
        params["event_types"] = event_types
    if categories:
        params["categories"] = categories
    if agent_id:
        params["agent_id"] = agent_id
    if after_sequence:
        try:
            params["after_sequence"] = int(after_sequence)
        except ValueError as error:
            raise ValueError("SSE after_sequence/Last-Event-ID must be an integer") from error
    if replay_limit:
        try:
            params["replay_limit"] = int(replay_limit)
        except ValueError as error:
            raise ValueError("SSE replay_limit must be an integer") from error
    if queue_size:
        try:
            params["queue_size"] = int(queue_size)
        except ValueError as error:
            raise ValueError("SSE query parameter 'queue_size' must be an integer") from error
    return params


def _split_query_values(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        result.extend(item.strip() for item in value.split(",") if item.strip())
    return result


def _sse_frame(event_name: str, payload: dict[str, Any]) -> bytes:
    data = _json_dumps(payload)
    event_id = payload.get("sequence")
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event_name}\ndata: {data}\n\n".encode()


def _heartbeat_interval_from_request(request: web.Request) -> float:
    raw_value = request.query.get("heartbeat_interval")
    if raw_value is None:
        return DEFAULT_WS_HEARTBEAT_INTERVAL
    try:
        interval = float(raw_value)
    except ValueError:
        return DEFAULT_WS_HEARTBEAT_INTERVAL
    if not math.isfinite(interval):
        return DEFAULT_WS_HEARTBEAT_INTERVAL
    return min(max(interval, MIN_WS_HEARTBEAT_INTERVAL), MAX_WS_HEARTBEAT_INTERVAL)


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=json_default)
