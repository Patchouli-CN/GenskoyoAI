# GensokyoAI/commands/context.py

from typing import TypeVar

from msgspec import Struct

from ..adapters import RuntimeAdapter
from ..core.agent import Agent
from .permission import PermissionLevel

# 泛型变量
T = TypeVar("T", bound="RuntimeAdapter")


class CommandContext[T: "RuntimeAdapter"](Struct, frozen=False):
    """
    命令执行上下文
    """

    agent: Agent | None = None
    backend: T | None = None
    source: str = "console"
    issuer: str = "Console"
    metadata: dict = {}
    # 调用方权限级（默认 OWNER：本地后端即主人，向后兼容；
    # 网络适配器应按平台身份显式给出，无法核实给 VISITOR）
    permission: PermissionLevel = PermissionLevel.OWNER

    @property
    def backend_inst(self) -> T:
        """后端实例"""
        if self.backend is None:
            raise ValueError("Backend is not set")
        return self.backend

    @property
    def agent_inst(self) -> Agent:
        if self.agent is None:
            raise ValueError("Agent is not set")
        return self.agent
