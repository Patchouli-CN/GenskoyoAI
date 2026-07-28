#!/usr/bin/env python3
"""JSON Lines RPC entry point for generic GensokyoAI runtime clients.

Protocol:
- stdin: one JSON object per line, e.g. {"id":1,"method":"character.list","params":{}}
- stdout: one JSON object per line, e.g. {"id":1,"ok":true,"result":...}
- stderr: diagnostic logs only; never parsed by clients.

Legacy method names such as ``list_characters`` remain supported during the
frontend/backend decoupling migration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Protocol, cast

ROOT_FOR_IMPORT = Path(__file__).resolve().parent
if str(ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(ROOT_FOR_IMPORT))

from GensokyoAI.runtime.rpc import runtime_error_to_dict  # noqa: E402
from GensokyoAI.runtime.service import RuntimeService  # noqa: E402


def _json_default(value: Any) -> str:
    return str(value)


async def _write_response(response: dict[str, Any]) -> None:
    print(json.dumps(response, ensure_ascii=False, default=_json_default), flush=True)


async def run_bridge(root: Path) -> int:
    service = RuntimeService(root_dir=root)
    loop = asyncio.get_running_loop()

    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":
            await service.shutdown()
            return 0

        line = line.strip()
        if not line:
            continue

        request_id: Any = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            if not isinstance(method, str):
                raise ValueError("Request field 'method' must be a string")
            if not isinstance(params, dict):
                raise ValueError("Request field 'params' must be an object")

            result = await service.handle(method, params)
            await _write_response({"id": request_id, "ok": True, "result": result})
            if method in {"shutdown", "runtime.shutdown"}:
                return 0
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            await _write_response(
                {
                    "id": request_id,
                    "ok": False,
                    "error": _error_payload(exc),
                }
            )


def _error_payload(exc: Exception) -> dict[str, Any]:
    payload = runtime_error_to_dict(exc)
    payload.setdefault("type", type(exc).__name__)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GensokyoAI JSON Lines runtime")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Root directory containing GensokyoAI, characters and config.",
    )
    return parser.parse_args()


class _ReconfigurableTextIO(Protocol):
    def reconfigure(self, **kwargs: Any) -> None: ...


def _reconfigure_text_stream(stream: Any) -> None:
    if hasattr(stream, "reconfigure"):
        cast(_ReconfigurableTextIO, stream).reconfigure(encoding="utf-8")


def main() -> None:
    _reconfigure_text_stream(sys.stdin)
    _reconfigure_text_stream(sys.stdout)
    _reconfigure_text_stream(sys.stderr)

    args = parse_args()
    raise SystemExit(asyncio.run(run_bridge(args.root.resolve())))


if __name__ == "__main__":
    main()
