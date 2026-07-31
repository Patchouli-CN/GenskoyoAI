# GensokyoAI/tools/tool_builtin/scene.py

"""场景工具 - 通过事件总线切换/查询当前场景（异步非阻塞）。

工具本身无状态：真实场景状态由 SceneManager 持有，工具通过 EventBus 的
request/response 与之通信，模式与 memory_tool 一致。
"""

from __future__ import annotations

from ...core.events import Event, SystemEvent
from ...utils.logger import logger
from ..base import tool
from ..tool_context import current_event_bus as get_event_bus


@tool(parallel_safe=False)  # 写状态：切换当前场景，同一 Actor 内串行
async def scene_switch(scene_id: str) -> str:
    """
    切换你当前所处的场景。当剧情发展让你移动到另一个地点时主动调用，
    例如从博丽神社走到魔法森林。切换后系统会记住你的新位置。

    只能使用上下文里【可前往的场景】清单中列出的场景 id（括号里的英文标识符），
    不要凭空编造 id。如果不确定有哪些场景或自己在哪，先调用 get_current_scene 查看。

    Args:
        scene_id: 目标场景的标识符，必须取自【可前往的场景】清单，如 hakurei_shrine
    """
    event_bus = get_event_bus()
    if event_bus is None:
        logger.warning("事件总线未初始化")
        return "「唔…好像没法移动到别的地方…」"

    if not scene_id or not scene_id.strip():
        return "「你想让我去哪里？」"

    request_event = Event(
        type=SystemEvent.SCENE_SWITCH_REQUESTED,
        source="tool.scene_switch",
        data={"scene_id": scene_id.strip()},
    )

    result = await event_bus.request(request_event, timeout=10.0)

    if result and isinstance(result, dict):
        if result.get("ok"):
            name = result.get("name", scene_id)
            return f"（已移动到「{name}」）"
        error = result.get("error", "无法前往该场景")
        return f"（{error}）"

    return "（场景切换失败了…）"


@tool()
async def get_current_scene() -> str:
    """
    查看你当前所处的场景。当你不确定自己现在身处何地、或想知道能前往哪些地点时调用，
    系统会告诉你当前场景的名称、环境描述，以及可前往的场景清单（含可用的 scene id）。
    """
    event_bus = get_event_bus()
    if event_bus is None:
        return "「唔…想不起来自己在哪了…」"

    request_event = Event(
        type=SystemEvent.SCENE_QUERY_CURRENT,
        source="tool.get_current_scene",
        data={},
    )

    result = await event_bus.request(request_event, timeout=10.0)

    if result and isinstance(result, dict) and result.get("description"):
        return str(result["description"])

    return "（你现在似乎不在任何特定的场景中。）"
