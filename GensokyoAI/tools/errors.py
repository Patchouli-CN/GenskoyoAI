"""工具结构化错误契约。"""

from __future__ import annotations

from typing import Any

from msgspec import Struct, field


class ToolError(Struct):
    """工具错误的三层结构。

    technical_message 给日志/事件诊断；user_message 给调用方/UI 展示；
    model_message 给模型上下文（干净、角色能自然回应的文本，不泄漏原始诊断串，
    如 ddg 超时的 startpage URL）。model_message 为 None 时回退 user/technical。
    """

    error_code: str
    technical_message: str
    user_message: str
    model_message: str | None = None
    recoverable: bool = True
    action_hint: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "error_code": self.error_code,
            "technical_message": self.technical_message,
            "user_message": self.user_message,
            "recoverable": self.recoverable,
            "action_hint": self.action_hint,
            "details": dict(self.details),
        }
        if self.model_message is not None:
            payload["model_message"] = self.model_message
        return payload


class ToolExecutionError(Exception):
    """携带结构化 ToolError 的工具执行异常。"""

    def __init__(self, error: ToolError):
        super().__init__(error.technical_message)
        self.error = error


__all__ = ["ToolError", "ToolExecutionError"]
