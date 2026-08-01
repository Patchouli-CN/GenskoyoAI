"""工具执行器"""

# GensokyoAI\tools\executor.py

import asyncio
import json
from typing import TYPE_CHECKING, Any

from ..core.agent.types import UnifiedMessage
from ..runtime.event_contract import sanitize_event_payload
from ..runtime.resource_control import (
    ResourceGate,
    ResourceLimitError,
    resource_limit_payload,
    resource_scope,
)
from ..utils.logger import logger
from .errors import ToolError, ToolExecutionError
from .external_manager import ExternalToolManager, is_external_tool_name
from .registry import ToolRegistry
from .tool_context import SINGLE_ACTOR_ID, ToolRuntimeContext, bind_tool_context

if TYPE_CHECKING:
    from ..core.events import EventBus
    from .web_search.service import WebSearchService


class ToolExecutor:
    """工具执行器"""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        event_bus: EventBus | None = None,
        external_tool_manager: ExternalToolManager | None = None,
        resource_gates: dict[str, ResourceGate] | None = None,
        actor_id: str = SINGLE_ACTOR_ID,
        world_id: str | None = None,
        web_search_service: WebSearchService | None = None,
    ):
        self._registry = registry or ToolRegistry()
        self._event_bus = event_bus
        self._external_tool_manager = external_tool_manager
        self._resource_gates = resource_gates or {}
        # Actor 身份：单角色模式默认取角色名 / world_id=None，
        # 多角色模式由 World 装配时按 roster 注入稳定 id。
        self._actor_id = actor_id
        self._world_id = world_id
        # 该 Actor 的联网搜索服务，按调用注入工具上下文；
        # 为 None 时工具回落模块级兜底（兼容测试与裸调用）。
        self._web_search_service = web_search_service

    def set_event_bus(self, event_bus: EventBus) -> None:
        """注入事件总线"""
        self._event_bus = event_bus

    def set_external_tool_manager(self, manager: ExternalToolManager | None) -> None:
        """注入外部工具管理器。"""
        self._external_tool_manager = manager

    def update_resource_gates(self, resource_gates: dict[str, ResourceGate] | None) -> None:
        """更新 Runtime 深层资源闸门引用。"""

        self._resource_gates = resource_gates or {}

    def parse_tool_calls(self, message: UnifiedMessage) -> list[dict[str, Any]]:
        """从 UnifiedMessage 对象解析工具调用"""
        if not message.tool_calls:
            return []

        parsed = []
        for tc in message.tool_calls:
            parsed.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
            )
        return parsed

    async def execute(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """执行单个工具调用"""
        name = tool_call.get("name")
        arguments = tool_call.get("arguments", {})

        if not name or not isinstance(name, str):
            return self._fail(
                tool_call,
                str(name) if name else "unknown",
                self._invalid_name_error(name),
                arguments,
                log_level="error",
            )

        self._publish_tool_event("started", name, arguments)

        if is_external_tool_name(name):
            return await self._execute_external(tool_call, name, arguments)

        tool_def = self._registry.get(name)
        if not tool_def:
            return self._fail(
                tool_call,
                name,
                self._tool_not_found_error(name),
                arguments,
                legacy_prefix="调用出错啦",
            )

        try:
            logger.trace(f"执行工具: {name}({arguments})")

            async with (
                resource_scope(self._resource_gates.get("tool"), f"tool:{name}"),
                resource_scope(
                    self._resource_gates.get("web_search") if name == "web_search" else None,
                    f"tool:{name}",
                ),
            ):
                # 按调用注入运行时上下文：内置工具（memory/scene）通过 tool_context
                # 读取当前 Actor 的事件总线与身份，替代模块级全局单例，
                # 使多个 Agent / Actor 互不覆盖。
                with bind_tool_context(
                    ToolRuntimeContext(
                        event_bus=self._event_bus,
                        actor_id=self._actor_id,
                        world_id=self._world_id,
                        web_search_service=self._web_search_service,
                    )
                ):
                    if tool_def.is_async:
                        result = await tool_def.func(**arguments)
                    else:
                        result = await asyncio.to_thread(tool_def.func, **arguments)

            result = self._serialize_tool_result(result)

            logger.trace(f"工具 {name} 执行成功: {result[:100]}...")
            self._publish_tool_event("completed", name, arguments, result=result)

            return {
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "name": name,
                "content": result,
            }
        except ResourceLimitError as e:
            return self._fail(tool_call, name, self._resource_limit_tool_error(e), arguments)
        except ToolExecutionError as e:
            return self._fail(tool_call, name, e.error, arguments, log_level="error")
        except Exception as e:
            return self._fail(
                tool_call,
                name,
                self._execution_failed_error(name, e, scope="tool"),
                arguments,
                log_level="error",
            )

    async def _execute_external(
        self,
        tool_call: dict[str, Any],
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if self._external_tool_manager is None:
            return self._fail(
                tool_call,
                name,
                ToolError(
                    error_code="external_tool.manager_unavailable",
                    technical_message="External tool manager is not configured for ToolExecutor.",
                    user_message="外部工具管理器不可用。",
                    recoverable=True,
                    action_hint="请确认 Runtime 或 Agent 已注入 ExternalToolManager。",
                    details={"name": name},
                ),
                arguments,
                legacy_prefix="调用出错啦",
            )

        try:
            async with resource_scope(self._resource_gates.get("tool"), f"external_tool:{name}"):
                result = await self._external_tool_manager.call_tool(name, arguments)
            result_content = self._serialize_tool_result(result)
            logger.info(f"外部工具 {name} 执行成功: {result_content[:100]}...")
            self._publish_tool_event("completed", name, arguments, result=result_content)
            return {
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "name": name,
                "content": result_content,
            }
        except ResourceLimitError as e:
            return self._fail(tool_call, name, self._resource_limit_tool_error(e), arguments)
        except ToolExecutionError as e:
            return self._fail(tool_call, name, e.error, arguments, log_level="error")
        except Exception as e:
            return self._fail(
                tool_call,
                name,
                self._execution_failed_error(name, e, scope="external_tool"),
                arguments,
                log_level="error",
            )

    @staticmethod
    def _resource_limit_tool_error(error: ResourceLimitError) -> ToolError:
        payload = resource_limit_payload(error)
        return ToolError(
            error_code=payload["code"],
            technical_message=payload["technical_message"],
            user_message=payload["user_message"],
            recoverable=payload["recoverable"],
            action_hint=payload["action_hint"],
            details=payload["details"],
        )

    @staticmethod
    def _invalid_name_error(name: Any) -> ToolError:
        return ToolError(
            error_code="tool.invalid_name",
            technical_message=f"无效的工具名称: {name}",
            user_message="工具调用名称无效。",
            recoverable=True,
            action_hint="请检查模型输出的 tool call name 字段。",
            details={"name": name},
        )

    @staticmethod
    def _tool_not_found_error(name: str) -> ToolError:
        return ToolError(
            error_code="tool.not_found",
            technical_message=f"工具 '{name}' 未找到",
            user_message=f"工具“{name}”不可用。",
            recoverable=True,
            action_hint="请确认工具已注册，或从模型可用工具 schema 中移除该工具。",
            details={"name": name},
        )

    @staticmethod
    def _execution_failed_error(name: str, exc: Exception, *, scope: str = "tool") -> ToolError:
        is_external = scope == "external_tool"
        prefix = "外部工具" if is_external else "工具"
        return ToolError(
            error_code=f"{scope}.execution_failed",
            technical_message=f"{prefix}执行失败: {exc}",
            user_message=f"{prefix}执行失败。",
            recoverable=True,
            action_hint="请检查外部工具源状态后重试。"
            if is_external
            else "请稍后重试；若持续失败，请检查工具配置和运行环境。",
            details={"name": name, "exception_type": type(exc).__name__},
        )

    def _fail(
        self,
        tool_call: dict[str, Any],
        name: str,
        error: ToolError,
        arguments: dict[str, Any],
        *,
        legacy_prefix: str = "错误",
        log_level: str = "warning",
    ) -> dict[str, Any]:
        """统一发布工具失败事件并返回结构化错误结果。"""
        log_func = getattr(logger, log_level, logger.warning)
        log_func(error.technical_message)
        self._publish_tool_event(
            "failed", name, arguments, error.technical_message, tool_error=error
        )
        return self._error_result(tool_call, name, error, legacy_prefix=legacy_prefix)

    @staticmethod
    def _serialize_tool_result(result: Any) -> str:
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    @staticmethod
    def _error_result(
        tool_call: dict[str, Any],
        name: str,
        error: ToolError,
        *,
        legacy_prefix: str,
    ) -> dict[str, Any]:
        """构造兼容旧字段的结构化错误结果。"""
        return {
            "role": "tool",
            "tool_call_id": tool_call.get("id", ""),
            "name": name,
            "content": f"{legacy_prefix}: {error.technical_message}",
            "is_error": True,
            "error": error.to_dict(),
        }

    def _publish_tool_event(
        self,
        status: str,
        name: str,
        arguments: dict[str, Any],
        error: str | None = None,
        result: str | None = None,
        tool_error: ToolError | None = None,
    ) -> None:
        """发布工具事件"""
        if self._event_bus is None:
            return

        from ..core.events import Event, SystemEvent

        if status == "started":
            event_type = SystemEvent.TOOL_CALL_STARTED
        elif status == "progress":
            event_type = SystemEvent.TOOL_CALL_PROGRESS
        elif status == "completed":
            event_type = SystemEvent.TOOL_CALL_COMPLETED
        else:
            event_type = SystemEvent.TOOL_CALL_FAILED

        data: dict[str, Any] = {
            "name": name,
            "arguments": sanitize_event_payload(arguments),
            "external": is_external_tool_name(name),
        }
        if error:
            data["error"] = error
        if tool_error:
            data.update(
                {
                    "error_code": tool_error.error_code,
                    "user_message": tool_error.user_message,
                    "technical_message": tool_error.technical_message,
                    "recoverable": tool_error.recoverable,
                    "action_hint": tool_error.action_hint,
                    "details": dict(tool_error.details),
                }
            )
        if result:
            data["result"] = result[:200] if len(result) > 200 else result

        self._event_bus.publish(
            Event(
                type=event_type,
                source="tool_executor",
                data=sanitize_event_payload(data),
            )
        )

    def publish_progress(
        self,
        name: str,
        status: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """发布工具调用进度事件，供长耗时工具或外部工具适配器复用。"""
        payload: dict[str, Any] = {"status": status}
        if message:
            payload["message"] = message
        if details:
            payload["details"] = details
        self._publish_tool_event("progress", name, payload)

    def _is_parallel_safe(self, name: str | None) -> bool:
        """判断某工具是否可并发执行。

        写状态工具（记忆写入、scene_switch 等）声明 ``parallel_safe=False``，需串行；
        本地注册表查不到的工具（含外部工具）默认视为并行安全——这是乐观选择
        （外部工具也可能是写状态），为保持既有行为而保留。
        """
        if not name or not isinstance(name, str):
            return True
        tool_def = self._registry.get(name)
        return tool_def.parallel_safe if tool_def is not None else True

    async def execute_batch(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """批量执行工具调用。

        按调用顺序扫描：连续的纯查询工具（``parallel_safe=True``）组成并发段
        （gather）；写状态工具（``parallel_safe=False``）在其原位串行执行。
        这样同批「先写后读」的依赖语义（如 remember 后 recall）保持正确，
        写状态工具彼此也不会并发。返回结果顺序与入参一致（按 tool_call_id 对齐）。
        """
        results: list[dict[str, Any] | None] = [None] * len(tool_calls)
        index = 0
        while index < len(tool_calls):
            if self._is_parallel_safe(tool_calls[index].get("name")):
                # 收集连续并发段，段内 gather 并发
                segment_start = index
                while index < len(tool_calls) and self._is_parallel_safe(
                    tool_calls[index].get("name")
                ):
                    index += 1
                segment = list(range(segment_start, index))
                segment_results = await asyncio.gather(
                    *(self.execute(tool_calls[i]) for i in segment)
                )
                for i, res in zip(segment, segment_results, strict=True):
                    results[i] = res
            else:
                # 写状态工具原位串行，等待前面的并发段完成后再执行
                results[index] = await self.execute(tool_calls[index])
                index += 1

        # execute() 所有路径必返回 dict；空槽位说明契约被破坏，显式失败而非静默错位
        final: list[dict[str, Any]] = []
        for result in results:
            if result is None:
                raise RuntimeError("工具批量执行结果出现空槽位，execute() 契约被破坏")
            final.append(result)
        return final

    def execute_sync(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """同步执行（兼容非异步环境）"""
        name = tool_call.get("name")
        arguments = tool_call.get("arguments", {})

        if not name or not isinstance(name, str):
            error = self._invalid_name_error(name)
            logger.error(error.technical_message)
            return self._error_result(
                tool_call, str(name) if name else "unknown", error, legacy_prefix="错误"
            )

        if is_external_tool_name(name):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self.execute(tool_call))
            error = ToolError(
                error_code="external_tool.sync_not_supported",
                technical_message="External tools require async execution when an event loop is already running.",
                user_message="外部工具不支持当前同步调用方式。",
                recoverable=True,
                action_hint="请改用异步 execute() 调用外部工具。",
                details={"name": name},
            )
            return self._error_result(tool_call, name, error, legacy_prefix="错误")

        tool_def = self._registry.get(name)
        if not tool_def:
            return self._error_result(
                tool_call, name, self._tool_not_found_error(name), legacy_prefix="错误"
            )

        # async 工具不能裸调用：直接 func(**args) 只会得到未 await 的协程，
        # 副作用没发生却被序列化成 "<coroutine object ...>" 当成功返回。
        if tool_def.is_async:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self.execute(tool_call))
            error = ToolError(
                error_code="tool.sync_not_supported",
                technical_message=(
                    f"Async tool '{name}' cannot run via execute_sync "
                    "while an event loop is already running."
                ),
                user_message="该工具不支持当前同步调用方式。",
                recoverable=True,
                action_hint="请改用异步 execute() 调用该工具。",
                details={"name": name},
            )
            return self._error_result(tool_call, name, error, legacy_prefix="错误")

        try:
            result = tool_def.func(**arguments)
            result_content = self._serialize_tool_result(result)
            return {
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "name": name,
                "content": result_content,
            }
        except ToolExecutionError as e:
            return self._error_result(tool_call, name, e.error, legacy_prefix="错误")
        except Exception as e:
            return self._error_result(
                tool_call, name, self._execution_failed_error(name, e), legacy_prefix="错误"
            )
