"""工作记忆"""

# GensokyoAI\memory\working.py

from typing import Any

from .types import WorkingMemory


class WorkingMemoryManager:
    """工作记忆管理器"""

    def __init__(self, max_turns: int = 20):
        self._memory = WorkingMemory(max_turns=max_turns)

    def add_message(
        self,
        role: str,
        content: str | list[dict[str, Any]],
        tool_calls=None,
        tool_call_id=None,
        reasoning_content: str | None = None,
        **extra,
    ):
        msg: dict = {"role": role, "content": content}

        if reasoning_content:
            msg["reasoning_content"] = reasoning_content

        if tool_calls:
            msg["tool_calls"] = [
                tc.to_dict() if hasattr(tc, "to_dict") else tc for tc in tool_calls
            ]

        if tool_call_id:
            msg["tool_call_id"] = tool_call_id

        for key, value in extra.items():
            if value is not None:
                msg[key] = value

        self._memory.messages.append(msg)
        self._trim()

    def _trim(self) -> None:
        """裁剪到最大轮数，并丢弃头部的孤儿 tool 结果消息。

        截断从头部进行，可能切断 tool_call 配对：assistant 的 tool_call 消息
        被裁掉后，其对应的 tool 结果消息会成为非法上下文（Provider 要求
        tool 消息必须紧跟带 tool_calls 的 assistant 消息），一并丢弃。
        """
        memory = self._memory
        limit = memory.max_turns * 2
        if len(memory.messages) > limit:
            del memory.messages[: len(memory.messages) - limit]
        while memory.messages and memory.messages[0].get("role") == "tool":
            del memory.messages[0]

    @staticmethod
    def _clean_reasoning(obj):
        """递归删除 reasoning_content，避免使用 copy.deepcopy。

        注意：该方法只能用于不支持 reasoning_content 的 Provider 出站清洗，
        不能用于 DeepSeek thinking mode 的工作记忆/上下文构建，否则会破坏
        DeepSeek 多轮对话必须回传 reasoning_content 的协议要求。
        """

        def _clean(item):
            if isinstance(item, dict):
                return {
                    key: _clean(value) for key, value in item.items() if key != "reasoning_content"
                }
            if isinstance(item, list):
                return [_clean(sub) for sub in item]
            return item

        return _clean(obj)

    def rollback_messages(self, count: int) -> int:
        """回滚最近 count 条消息，返回实际移除数量。"""
        if count <= 0:
            return 0

        removed = min(count, len(self._memory.messages))
        if removed:
            del self._memory.messages[-removed:]
        return removed

    def rollback_turns(self, count: int) -> int:
        """按对话轮回滚消息，每轮默认包含 user/assistant 两条消息。"""
        return self.rollback_messages(count * 2)

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """全量替换工作记忆消息，保留消息扩展字段。"""
        self._memory.messages = [dict(message) for message in messages]

    def get_context(self) -> list[dict[str, Any]]:
        """获取当前上下文"""
        return self._memory.get_context()

    def get_recent(self, n: int) -> list[dict[str, Any]]:
        """获取最近 n 条消息"""
        return self._memory.messages[-n:] if n > 0 else []

    def clear(self) -> None:
        """清空工作记忆"""
        self._memory.clear()

    def __len__(self) -> int:
        return len(self._memory.messages)
