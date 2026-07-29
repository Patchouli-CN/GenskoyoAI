"""对话主循环协议：单角色 Agent 与多角色 World 共享的主动发言抽象。

主动定时器归属于「对话主循环」而非固定归属于 Agent：单角色模式由 Agent
充当主循环，多角色模式由 GensokyoWorld 充当主循环。两者通过本协议共享
计划/触发/取消的编排形状，避免复制粘贴出两份主动机制。
"""

from __future__ import annotations

from typing import Any, Protocol

from msgspec import Struct


class InitiativePlan(Struct):
    """一次主动发言计划：只存意图摘要，不存话术（说什么到点再生成）。

    - ``should_schedule``：是否安排；False 时其余字段无意义。
    - ``delay_seconds``：多少秒后触发。
    - ``summary``：意图摘要——到点要围绕什么思考和表达，不是可直接发送的台词。
    - ``reason``：安排理由（调试可见）。
    - ``enthusiasm``：0~1 的热情度，调度器可据此微调等待时长。
    """

    should_schedule: bool
    delay_seconds: int = 300
    summary: str = ""
    reason: str = ""
    enthusiasm: float = 0.5


class DialogueLoop(Protocol):
    """对话主循环的主动发言编排协议。"""

    async def plan_initiative_after_turn(self) -> InitiativePlan | None:
        """一轮对话结束后规划下一次主动发言；不安排返回 None。"""
        ...

    async def trigger_initiative(self, plan: InitiativePlan) -> Any:
        """定时器到点，执行主动发言。"""
        ...

    async def cancel_initiative(self, reason: str) -> bool:
        """取消/作废当前主动计划（用户输入、被新计划取代等）。"""
        ...
