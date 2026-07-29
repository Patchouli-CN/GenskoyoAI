"""工具运行时上下文：按调用隔离的 Actor / 事件总线注入。

替代此前 memory_tool / scene 各自持有的模块级全局单例（`_event_bus`）。
模块级单例意味着整个进程只能有一个事件总线，多个 Agent 实例会互相覆盖——
最后初始化的 Agent 会“抢走”所有工具调用。这挡在“多角色同时对话”功能前面。

现在改为基于 :class:`contextvars.ContextVar` 承载 :class:`ToolRuntimeContext`：
``ToolExecutor`` 在每次调用工具前通过 :func:`bind_tool_context` 注入当前 Actor
的上下文（actor_id / world_id / event_bus），工具函数通过 :func:`current_event_bus`
（或 :func:`current_tool_context`）读取。``asyncio.gather`` 会为每个并发工具调用
复制上下文，因此并行工具执行天然按调用隔离，不同 Agent / Actor 也互不干扰。
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from msgspec import Struct

if TYPE_CHECKING:
    from ..core.events import EventBus

# 单角色（无 World）模式下的默认 actor 标识。
SINGLE_ACTOR_ID = "__single__"


class ToolRuntimeContext(Struct):
    """一次工具调用所归属的运行时上下文。

    - ``event_bus``：该 Actor 的事件总线，工具通过它与 memory/scene 服务通信。
    - ``actor_id``：稳定的 Actor 标识；单角色模式默认取角色名
      （composition 未注入 actor_id 时的兜底），多角色模式为 World
      roster 中的稳定 id。
    - ``world_id``：所属 World 的 id；单角色模式为 None。
    """

    event_bus: EventBus | None = None
    actor_id: str = SINGLE_ACTOR_ID
    world_id: str | None = None


# 当前工具调用上下文；默认 None（未在工具执行上下文中）。
_tool_context_var: contextvars.ContextVar[ToolRuntimeContext | None] = contextvars.ContextVar(
    "gensokyo_tool_runtime_context", default=None
)


@contextmanager
def bind_tool_context(context: ToolRuntimeContext | None) -> Iterator[None]:
    """在当前上下文内绑定工具运行时上下文，退出时恢复原值。

    由 ``ToolExecutor`` 在调用工具前后成对使用，保证上下文按调用生效、
    调用结束即还原，不会跨调用或跨 Actor 泄漏。
    """

    token = _tool_context_var.set(context)
    try:
        yield
    finally:
        _tool_context_var.reset(token)


def current_tool_context() -> ToolRuntimeContext | None:
    """返回当前工具调用的运行时上下文（未绑定时为 None）。"""

    return _tool_context_var.get()


def current_event_bus() -> EventBus | None:
    """返回当前工具调用上下文中的事件总线（未绑定时为 None）。"""

    context = _tool_context_var.get()
    return context.event_bus if context is not None else None


@contextmanager
def bind_event_bus(event_bus: EventBus | None) -> Iterator[None]:
    """遗留兼容：仅绑定事件总线的上下文管理器。

    等价于绑定一个只设置了 ``event_bus`` 的 :class:`ToolRuntimeContext`；
    保留是为了兼容早期只关心事件总线的调用点。新代码应使用
    :func:`bind_tool_context` 以携带 actor_id / world_id。
    """

    with bind_tool_context(ToolRuntimeContext(event_bus=event_bus)):
        yield


def set_event_bus(event_bus: EventBus | None) -> None:
    """遗留兼容入口：直接设置当前上下文的事件总线（不自动恢复）。

    仅为兼容早期直接调用 ``set_event_bus`` 的代码而保留；正常调用链应依赖
    ``ToolExecutor`` 的 :func:`bind_tool_context` 按调用注入。
    """

    _tool_context_var.set(ToolRuntimeContext(event_bus=event_bus))
