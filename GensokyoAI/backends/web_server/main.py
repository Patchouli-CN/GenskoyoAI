"""HTTP/WebSocket Runtime entry point for GensokyoAI."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path

from aiohttp import web

from .http_adapter import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GensokyoAI HTTP/WebSocket runtime")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="Root directory containing GensokyoAI, characters and config.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind.")
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help="Allowed browser Origin. Repeat for multiple origins.",
    )
    parser.add_argument(
        "--allow-all-origins",
        action="store_true",
        help="Allow every browser Origin. Use only behind a trusted gateway.",
    )
    parser.add_argument(
        "--allow-remote-admin",
        action="store_true",
        default=None,
        help="Enable privileged administration methods on HTTP and WebSocket transports.",
    )
    return parser.parse_args()


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def main() -> None:
    args = parse_args()
    web.run_app(
        create_app(
            root_dir=args.root.resolve(),
            allowed_origins=args.allowed_origin,
            allow_all_origins=args.allow_all_origins,
            allow_remote_admin=args.allow_remote_admin,
            require_auth=not _is_loopback_host(args.host),
        ),
        host=args.host,
        port=args.port,
    )
