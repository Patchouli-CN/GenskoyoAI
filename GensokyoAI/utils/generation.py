"""代际令牌（借鉴 creature-chat 的 ContextToken）：翻代后旧代际的异步结果禁止写回。

适用面：会话切换 / 角色热切换等「作用域翻代」场景。LLM 调用在途期间作用域
被替换（如 ThinkEngine 的记忆实例被换），迟到结果若写进新作用域即污染——
旧会话的蒸馏内容会落进新会话的记忆。用法：

    token = guard.capture()          # 发起异步前捕获
    ...await 慢调用...
    if not guard.if_current(token, "蒸馏记忆写回"):  # 写回前终验
        return
    ...写入...
"""

# GensokyoAI/utils/generation.py

from .logger import logger


class GenerationGuard:
    """单调代际计数器；令牌只在捕获它的作用域代内有效。"""

    __slots__ = ("_generation",)

    def __init__(self) -> None:
        self._generation = 0

    def capture(self) -> int:
        """捕获当前代际令牌（发起异步操作前调用）。"""
        return self._generation

    def bump(self) -> None:
        """翻代：此后所有旧令牌一律判过期（作用域替换时调用）。"""
        self._generation += 1

    def is_current(self, token: int) -> bool:
        return token == self._generation

    def if_current(self, token: int, what: str = "异步结果") -> bool:
        """写回前终验；过期时打一条可归因日志并返回 False。"""
        if self.is_current(token):
            return True
        logger.debug(f"代际已翻，丢弃过期{what}（捕获于 gen={token}，当前 gen={self._generation}）")
        return False
