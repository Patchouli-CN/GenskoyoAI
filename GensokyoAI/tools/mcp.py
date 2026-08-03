"""MCP 外部工具最小适配层。

该模块提供可单测的 MCP source 抽象，覆盖 initialize/list_tools/call_tool 的
最小协议转换。真实 stdio/http 传输只需实现 McpTransport 协议即可复用。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from .external_manager import (
    ExternalToolDefinition,
    make_external_tool_name,
    normalize_external_name,
    normalize_external_permissions,
)


class McpTransport(Protocol):
    """MCP 传输最小协议。"""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any: ...


class McpSource:
    """将 MCP server 暴露为 ExternalToolSource。

    start() 幂等且加锁：list_tools/call_tool 的惰性 start 与外部并发 start
    不会重复 initialize/start transport（旧 CODE_INF 06#4）。
    """

    def __init__(
        self,
        source_id: str,
        transport: McpTransport,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.source_id = normalize_external_name(source_id, kind="source_id")
        self.transport = transport
        self.metadata = metadata or {}
        self._initialized = False
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._initialized:
                return
            await self.transport.start()
            await self.transport.request(
                "initialize",
                {
                    "clientInfo": {"name": "GensokyoAI", "version": "p1-mcp-minimal"},
                    "protocolVersion": self.metadata.get("protocol_version", "2024-11-05"),
                    "capabilities": {},
                },
            )
            self._initialized = True

    async def stop(self) -> None:
        await self.transport.stop()
        self._initialized = False

    async def list_tools(self) -> list[ExternalToolDefinition]:
        if not self._initialized:
            await self.start()
        response = await self.transport.request("tools/list", {})
        raw_tools = _extract_mcp_tools(response)
        return [mcp_tool_to_external_definition(self.source_id, tool) for tool in raw_tools]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if not self._initialized:
            await self.start()
        return await self.transport.request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )


def _extract_mcp_tools(response: Any) -> list[dict[str, Any]]:
    tools = response.get("tools", []) if isinstance(response, dict) else response
    if not isinstance(tools, list):
        raise ValueError("MCP tools/list response must contain a tools list")
    return [tool for tool in tools if isinstance(tool, dict)]


def mcp_tool_to_external_definition(source_id: str, tool: dict[str, Any]) -> ExternalToolDefinition:
    """把 MCP tool 描述转换为外部工具定义。"""

    raw_name = tool.get("name")
    if not isinstance(raw_name, str):
        raise ValueError("MCP tool missing string name")
    tool_name = normalize_external_name(raw_name, kind="tool_name")
    description = str(tool.get("description") or "")
    input_schema = (
        tool.get("inputSchema") or tool.get("input_schema") or {"type": "object", "properties": {}}
    )
    metadata = dict(tool.get("metadata") or {})
    annotations = tool.get("annotations")
    if isinstance(annotations, dict):
        metadata.setdefault("annotations", annotations)
        if annotations.get("readOnlyHint") is True:
            metadata.setdefault("permissions", ["read-only"])
        if annotations.get("destructiveHint") is True:
            metadata.setdefault("permissions", ["destructive"])
    metadata["transport"] = metadata.get("transport", "mcp")
    metadata["permissions"] = sorted(normalize_external_permissions(metadata.get("permissions")))

    namespaced_name = make_external_tool_name(source_id, tool_name)
    return ExternalToolDefinition(
        source_id=source_id,
        tool_name=tool_name,
        namespaced_name=namespaced_name,
        description=description,
        schema={
            "type": "function",
            "function": {
                "name": namespaced_name,
                "description": description,
                "parameters": input_schema,
            },
        },
        metadata=metadata,
    )


__all__ = ["McpSource", "McpTransport", "mcp_tool_to_external_definition"]
