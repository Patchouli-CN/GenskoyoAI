"""行动规划器 - Agent 的大脑决策区域"""

# GensokyoAI/core/agent/action_planner.py
import json
import re
from collections import deque
from typing import TYPE_CHECKING, Any

from ...utils.logger import logger
from ..events import Event, EventBus, EventPriority, SystemEvent
from .actions import Action, ActionFactory, ActionType
from .conflict_detector import ConflictDetector
from .message_builder import MessageBuilder
from .motivation_evaluator import MotivationEvaluator

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
        debug_silent_output: bool = False,
    ):
        self.character_name = character_name
        self.model_client = model_client
        self.working_memory = working_memory
        self.semantic_memory = semantic_memory
        self.event_bus = event_bus
        self.debug_silent_output = debug_silent_output

        self.motivation_evaluator = MotivationEvaluator(self.character_name, self.model_client)
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
        """三阶段决策"""

        # 阶段1：动机评估
        emotional_valence = topics_detail[0].get("emotional_valence", 0) if topics_detail else 0
        motivation = await self.motivation_evaluator.evaluate(
            thought=thought,
            emotional_valence=emotional_valence,
            topics_detail=topics_detail,
        )

        # 阶段2：冲突检测
        conflict = self.conflict_detector.detect(
            motivation=motivation,
            emotional_valence=emotional_valence,
        )

        # 阶段3：策略选择
        if conflict.has_conflict and conflict.recommendation == "克制":
            # 明明想说但克制住了——记录这个"内心挣扎"
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

        # 正常流程：让 LLM 在动机数据基础上做最终判断
        return await self._llm_decide(motivation, thought, topics_detail, conflict)

    async def _llm_decide(self, motivation, thought, topics_detail, conflict) -> Action:
        """让 LLM 在提供动机和冲突数据的基础上做决策"""

        topics_desc = (
            "\n".join(f"- {t.get('name', '')}: {t.get('summary', '')}" for t in topics_detail)
            if topics_detail
            else "无"
        )

        conflict_note = ""
        if conflict.has_conflict:
            conflict_note = (
                f"\n⚠️ 检测到{conflict.conflict_type.name}冲突"
                f"(强度{conflict.intensity:.2f})，建议: {conflict.recommendation}"
            )

        prompt = f"""你是 {self.character_name}，正在决定是否主动说话。

    【当前动机画像】
    {motivation.to_prompt_context()}

    【思考内容】
    {thought}

    【相关话题】
    {topics_desc}
    {conflict_note}

    请综合判断，用 JSON 回答：
    {{
        "should_speak": true/false,
        "intensity": "high/medium/low",  // 如果说话，语气强度
        "reason": "决策理由，如果选择不说，请描述内心的挣扎",
        "message": "如果说话，完整的话；如果不说，留空"
    }}

    只输出 JSON。"""

        try:
            messages = [{"role": "system", "content": prompt}]
            raw_content = self.working_memory.get_recent(6)  # 这里固定3轮对话作为思考就好
            cleaned = (
                MessageBuilder.operate_on(raw_content)
                .exclude_role("tool")
                .exclude(role="assistant", has="tool_calls")
                .get()
            )

            messages.extend(cleaned)
            response = await self.model_client.chat(
                messages=messages,
                options={"temperature": 0.7, "num_predict": 300},
            )

            content = response.message.content
            text = content.strip() if isinstance(content, str) else ""
            match = re.search(r"\{[^{}]*\}", text)
            if match:
                data = json.loads(match.group())
                if data.get("should_speak", False):
                    message = data.get("message", "").strip()
                    if message:
                        return ActionFactory.initiative_speak(
                            content=message,
                            reason=f"{data.get('reason', '')} (驱动力:{motivation.total_drive:.2f})",
                        )
                else:
                    # 沉默也是一种行动——记录拒绝理由
                    if self.debug_silent_output:
                        logger.info(
                            f"🤫 [ActionPlanner] {self.character_name} 选择沉默: "
                            f"{data.get('reason', '')} (驱动力:{motivation.total_drive:.2f})"
                        )
                    else:
                        logger.debug(
                            f"🤫 [ActionPlanner] {self.character_name} 选择沉默（调试输出关闭，理由已隐藏）"
                        )
        except Exception as e:
            logger.error(f"LLM 决策失败: {e}")

        # 降级：高驱动力就说
        if motivation.total_drive > 0.7:
            return ActionFactory.initiative_speak(
                content=thought[:100] + "...", reason="驱动力太高，不忍了"
            )

        return ActionFactory.wait(reason="驱动力不足")

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
