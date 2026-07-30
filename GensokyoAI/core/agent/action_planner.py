"""行动规划器 - Agent 的大脑决策区域"""

# GensokyoAI/core/agent/action_planner.py
from collections import deque
from typing import TYPE_CHECKING, Any

from ...utils.logger import logger
from ..events import Event, EventBus, EventPriority, SystemEvent
from .actions import Action, ActionFactory, ActionType
from .conflict_detector import ConflictDetector

if TYPE_CHECKING:
    from ...memory.semantic import SemanticMemoryManager
    from ...memory.working import WorkingMemoryManager
    from .model_client import ModelClient


def _extract_text_from_content(content: Any) -> str:
    """从字符串或多模态 content parts 中提取文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return " ".join(texts)
    return ""


class ActionPlanner:
    """
    行动规划器 - Agent 的大脑

    慧音：三思而后行！
    紫：边界要模糊，考虑多种可能！
    灵梦：简单点，能偷懒就偷懒~
    """

    def __init__(
        self,
        character_name: str,
        model_client: ModelClient,
        working_memory: WorkingMemoryManager,
        semantic_memory: SemanticMemoryManager,
        event_bus: EventBus,
        think_engine=None,
        debug_silent_output: bool = False,
    ):
        self.character_name = character_name
        self.model_client = model_client
        self.working_memory = working_memory
        self.semantic_memory = semantic_memory
        self.event_bus = event_bus
        # 主动说话的四维心情评估由 ThinkEngine 统一决策（模块化决策区）；
        # 为 None（思考引擎禁用）时主动说话直接沉默
        self._think_engine = think_engine
        self.debug_silent_output = debug_silent_output

        self.conflict_detector = ConflictDetector()

        self._last_action: Action | None = None
        self._action_history: deque[Action] = deque(maxlen=50)

        self._subscribe_events()
        logger.debug(f"🧠 [ActionPlanner] 初始化完成，角色: {character_name}")

    def _subscribe_events(self) -> None:
        """订阅需要决策的事件"""
        self.event_bus.subscribe(
            SystemEvent.MESSAGE_RECEIVED, self._on_message_received, priority=EventPriority.HIGHEST
        )
        self.event_bus.subscribe(
            SystemEvent.THINK_ENGINE_THOUGHT,
            self._on_thought_generated,
        )
        self.event_bus.subscribe(
            SystemEvent.TOOL_CALL_COMPLETED,
            self._on_tool_completed,
        )

    # ==================== 事件处理 ====================

    async def _on_message_received(self, event: Event) -> None:
        """收到用户消息 - 决定如何回应"""
        user_input = event.data.get("content", "")
        text_input = _extract_text_from_content(user_input)

        # 空消息不回应
        if not text_input or len(text_input.strip()) <= 1:
            action = ActionFactory.wait(reason="用户输入太短")
        else:
            action = ActionFactory.speak(reason=f"回应: {text_input[:30]}...")

        self._record_action(action)
        self._publish_action(action, trigger_event=event)

    async def _on_thought_generated(self, event: Event) -> None:
        """思考引擎产生想法 - 决定是否主动说话"""
        thought = event.data.get("thought", "")
        topics_detail = event.data.get("topics_detail", [])

        if not thought:
            return

        action = await self._decide_initiative_action(thought, topics_detail)

        if action.type != ActionType.WAIT:
            self._record_action(action)
            self._publish_action(action, trigger_event=event)
            logger.info(f"✨ [ActionPlanner] {self.character_name} 决定主动说话")
        else:
            logger.debug(f"🤫 [ActionPlanner] {self.character_name} 决定不主动说话")

    async def _on_tool_completed(self, event: Event) -> None:
        """工具执行完成 - 不需要再触发 SPEAK"""
        # FIX: response_handler.process_stream 已经在工具调用后
        # 自动进行了第二次流式调用并生成了最终回复，
        # 这里不需要再发布 SPEAK 行动，否则会导致重复调用和空消息
        pass  # 什么都不做

    # ==================== 决策核心 ====================

    async def _decide_initiative_action(self, thought: str, topics_detail: list) -> Action:
        """主动说话决策：ThinkEngine 四维心情打分，总分超阈值即说，否则沉默。

        决策区在 ThinkEngine（模块化）：LLM 只产出四维动机与候选发言，
        「说不说」由 `total_drive >= drive_threshold` 独立判定；
        无累积器、无 LLM 二次判断、无强制降级。
        """
        if self._think_engine is None:
            return ActionFactory.wait(reason="思考引擎不可用，主动说话保持沉默")

        decision = await self._think_engine.evaluate_speaking_drive(
            thought,
            self.working_memory.get_recent(6),
        )

        # 冲突检测（性格层）：总分想说但内心克制——记录这场「内心挣扎」
        emotional_valence = topics_detail[0].get("emotional_valence", 0) if topics_detail else 0
        motivation = decision.motivation if decision is not None else None
        if motivation is not None:
            conflict = self.conflict_detector.detect(
                motivation=motivation,
                emotional_valence=emotional_valence,
            )
            if conflict.has_conflict and conflict.recommendation == "克制":
                if self.debug_silent_output:
                    logger.info(
                        f"🌙 [ActionPlanner] {self.character_name} 想说但克制了 "
                        f"({conflict.conflict_type.name}, 驱动力: {motivation.total_drive:.2f})"
                    )
                else:
                    logger.debug(
                        f"🌙 [ActionPlanner] {self.character_name} 产生内心克制决策（调试输出关闭，内容已隐藏）"
                    )
                return ActionFactory.wait(
                    reason=f"内心有话说但{conflict.conflict_type.name}(强度{conflict.intensity:.2f})"
                )

        if decision is None:
            return ActionFactory.wait(reason="对话欲评估失败，保持沉默")

        if decision.want_speak:
            # content 传「待表达意图摘要」而非定稿话术：
            # 真正发给用户的消息由 executor 经说话前思考 + 即时生成产出
            return ActionFactory.initiative_speak(
                content=decision.message,
                reason=f"{decision.reason} (驱动力:{decision.total_drive:.2f})",
            )

        # 沉默也是一种行动——记录拒绝理由
        if self.debug_silent_output:
            logger.info(
                f"🤫 [ActionPlanner] {self.character_name} 选择沉默: "
                f"{decision.reason} (驱动力:{decision.total_drive:.2f})"
            )
        else:
            logger.debug(
                f"🤫 [ActionPlanner] {self.character_name} 选择沉默（调试输出关闭，理由已隐藏）"
            )
        return ActionFactory.wait(reason=f"对话欲不足（{decision.total_drive:.2f} 未达阈值）")

    def update_memory_context(
        self,
        working_memory: WorkingMemoryManager,
        semantic_memory: SemanticMemoryManager,
    ) -> None:
        """会话切换后就地更新记忆引用（保留事件订阅与行动历史）。

        本方法不重建实例：ActionPlanner 在构造时已订阅事件总线，换新实例
        而不退订旧实例会导致双重规划。
        """
        self.working_memory = working_memory
        self.semantic_memory = semantic_memory

    # ==================== 行动发布 ====================

    def _publish_action(self, action: Action, trigger_event: Event | None = None) -> None:
        """发布行动决策事件"""
        data: dict[str, Any] = {
            "action": action.to_dict(),
            "trigger_event_id": trigger_event.id if trigger_event else None,
            "user_input": trigger_event.data.get("content") if trigger_event else None,
        }
        if trigger_event is not None:
            # 透传本轮系统上下文与 world 标记，保证 GENERATE_RESPONSE 能拿到
            # World 注入的舞台/在场/共享剧本（此前在事件链中被丢弃）。
            if system_contexts := trigger_event.data.get("system_contexts"):
                data["system_contexts"] = system_contexts
            if trigger_event.data.get("world_turn"):
                data["world_turn"] = True
            # 透传发送方铸造的请求绑定 id，供 ActionExecutor 识别过期生成
            if request_id := trigger_event.data.get("request_id"):
                data["request_id"] = request_id
        self.event_bus.publish(
            Event(
                type=SystemEvent.ACTION_DECIDED,
                source="action_planner",
                data=data,
            )
        )
        logger.info(f"🧠 [ActionPlanner] 决策: {action.type.name} - {action.reason}")

    def _record_action(self, action: Action) -> None:
        self._last_action = action
        self._action_history.append(action)

    @property
    def last_action(self) -> Action | None:
        return self._last_action
