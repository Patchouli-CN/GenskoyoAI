"""多人同时发言的待发合并（群聊/私聊共用，无 nonebot 依赖可单测）。

问题：两个人同时 @bot 时，旧实现按会话锁串行逐条处理，bot 交替回两条，
群里看起来像在和两个人分别对话。现在改为按会话 key 的待发队列：
处理期间（含初始合并窗口）到达的消息攒成一批，合成一轮输入、一条回复
同时回应所有人——文本各自带【昵称】标记，模型天然分得清谁说了什么。

并发说明：全部状态只在事件循环内访问（入队/取批/收尾之间无 await），
单线程语义天然无竞态——处理循环取空批次到 finish 之间不发生切换，
间隙到达的消息必然落在下一次 take_batch 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PendingChat:
    """一条等待处理的发言（文本已带说话人标记，上下文为当轮注入）。"""

    text: str
    contexts: list[str] = field(default_factory=list)
    member_name: str | None = None
    member_qq: int | None = None
    self_id: int = 0
    message_id: int = 0


class PendingChatQueue:
    """按会话 key 的待发队列：有活跃处理时新消息并入下批。"""

    def __init__(self) -> None:
        self._queues: dict[str, list[PendingChat]] = {}
        self._active: set[str] = set()

    def add(self, key: str, item: PendingChat) -> bool:
        """入队；返回 True 表示调用方应成为处理者（此前该 key 无活跃处理）。"""
        self._queues.setdefault(key, []).append(item)
        if key in self._active:
            return False
        self._active.add(key)
        return True

    def take_batch(self, key: str) -> list[PendingChat]:
        """取走当前全部待发（合并为一批）；队列为空返回空列表。"""
        queue = self._queues.get(key)
        if not queue:
            return []
        batch = queue[:]
        queue.clear()
        return batch

    def finish(self, key: str) -> None:
        """处理循环结束：无残留待发才清理（收尾间隙新到消息会留下队列）。"""
        self._active.discard(key)
        if not self._queues.get(key):
            self._queues.pop(key, None)

    def pending_count(self, key: str) -> int:
        return len(self._queues.get(key, []))


def batch_at_targets(batch: list[PendingChat]) -> list[int]:
    """一批待发里要 @ 的发言人 QQ（去重保序，无 QQ 的跳过）。"""
    targets: list[int] = []
    for item in batch:
        if item.member_qq is not None and item.member_qq not in targets:
            targets.append(item.member_qq)
    return targets


def merge_batch(batch: list[PendingChat]) -> tuple[str, list[str], str]:
    """把一批待发合并为一轮输入。

    返回 ``(合并文本, 合并上下文, 幂等键)``：文本按到达顺序逐行拼接
    （各自带【昵称】标记）；上下文去重保序（全局附加要求只留一份，
    不同成员的印象/情绪注入各自保留）；幂等键取批次首条消息。
    """
    text = "\n".join(item.text for item in batch)
    contexts: list[str] = []
    for item in batch:
        for context in item.contexts:
            if context not in contexts:
                contexts.append(context)
    first = batch[0]
    idempotency_key = f"nb2:{first.self_id}:{first.message_id}"
    return text, contexts, idempotency_key
