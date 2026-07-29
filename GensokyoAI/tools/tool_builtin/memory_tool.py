# GensokyoAI/tools/tool_builtin/memory_tool.py

"""记忆工具 - 通过事件总线操作记忆（异步非阻塞）"""

from ...core.events import Event, SystemEvent
from ...utils.logger import logger
from ..base import tool
from ..tool_context import current_event_bus as get_event_bus
from ..tool_context import set_event_bus as set_event_bus  # 遗留兼容再导出


@tool(parallel_safe=False)  # 写状态：记忆写入，同一 Actor 内串行
async def remember(
    content: str,
    topic: str = "",
    importance: int = 5,
    emotional_valence: float = 0.0,  # 情感效价 -1.0到1.0
) -> str:
    """
    记住重要的信息。当你了解到新的事实、产生情感波动或发现重要事件时主动调用。

    Args:
        content: 要记住的内容
        topic: 话题名称，用于归类记忆。如果不填，系统会自动匹配
        importance: 重要性 1-10
        emotional_valence: 情感效价，-1.0(极度负面) 到 1.0(极度正面)，0为中性
    """
    event_bus = get_event_bus()
    if event_bus is None:
        logger.warning("事件总线未初始化")
        return "「唔…记忆功能好像还没准备好…」"

    if not content or len(content) <= 1:
        return "「这个…好像没什么值得记的…」"

    importance = max(1, min(10, importance))
    normalized_importance = importance / 10.0

    # 限制情感范围
    emotional_valence = max(-1.0, min(1.0, emotional_valence))

    request_event = Event(
        type=SystemEvent.MEMORY_SEMANTIC_ADDED,
        source="tool.remember",
        data={
            "content": content,
            "importance": normalized_importance,
            "emotional_valence": emotional_valence,
            "topic_name": topic if topic else None,
        },
    )

    result = await event_bus.request(request_event, timeout=10.0)

    if result and isinstance(result, dict):
        topic_name = result.get("topic_name", topic or "记忆")
        if importance >= 8:
            if abs(emotional_valence) > 0.7:
                return f"「这件事让我感触很深，我会永远记住的！({topic_name})」"
            return f"「这个很重要，我记住了！({topic_name})」"
        else:
            return "「嗯，记住了～」"

    # 事件总线未启动/请求超时/无订阅者时 result 为 None——诚实报失败，
    # 不能让模型以为已经记住（对比 update_memory 的失败路径）。
    return "「记忆写入失败了…（事件总线无响应，刚才的内容没有保存成功）」"


@tool()
async def recall(
    keyword: str,
    page: int = 1,
) -> str:
    """
    回忆之前记住的信息。当你需要引用已知事实、回忆过往情感或查找历史事件时调用。

    Args:
        keyword: 搜索关键词
        page: 页码，从1开始
    """
    event_bus = get_event_bus()
    if event_bus is None:
        return "「记忆功能还没准备好…」"

    if not keyword:
        return "「你想让我回忆什么？」"

    request_event = Event(
        type=SystemEvent.MEMORY_SEMANTIC_RECALLED,
        source="tool.recall",
        data={
            "keyword": keyword,
            "page": page,
        },
    )

    result = await event_bus.request(request_event, timeout=10.0)

    if result and isinstance(result, str):
        return result

    return f"「关于 '{keyword}' …我好像没什么印象…」"


@tool(parallel_safe=False)  # 写状态：更新记忆，同一 Actor 内串行
async def update_memory(
    topic: str,
    new_content: str,
    reason: str = "信息更新",
) -> str:
    """
    更新已有的记忆（当发现旧信息过时或不准确时调用）

    Args:
        topic: 要更新的话题名
        new_content: 新的记忆内容
        reason: 更新原因
    """
    event_bus = get_event_bus()
    if event_bus is None:
        return "「记忆功能还没准备好…」"

    request_event = Event(
        type=SystemEvent.MEMORY_SEMANTIC_UPDATED,
        source="tool.update_memory",
        data={
            "topic_name": topic,
            "new_content": new_content,
            "reason": reason,
        },
    )

    result = await event_bus.request(request_event, timeout=10.0)

    if result:
        return f"「嗯，我更新了关于'{topic}'的记忆。{reason}」"

    return f"「关于'{topic}'的记忆更新失败了…」"
