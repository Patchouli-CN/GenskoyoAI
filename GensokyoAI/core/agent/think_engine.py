"""思考引擎 - 负责所有 AI 思考活动

职责分层：
- 长期思考：定时游走话题图谱，产生内心独白（原 ThinkEngine）
- 短期思考：每次 AI 回复后，决策是否设置主动定时器（原 InitiativeTimer._decide）
- 说话前思考：定时器到期前，生成主动消息前的内部思考（原 Agent._handle_initiative_timer_trigger thought）

❌ 不负责决策（交给 ActionPlanner）
❌ 不负责生成主动消息（交给 InitiativeTimer + Agent）
"""

import asyncio
import contextlib
import json
import random
import re
from datetime import datetime, timedelta
from typing import Any

from msgspec import Struct, field

from ...memory.semantic import SemanticMemoryManager
from ...memory.types import Topic
from ...utils.helpers import utc_now
from ...utils.logger import logger
from ..config import InitiativeTimerConfig, ThinkEngineConfig
from ..events import Event, EventBus, SystemEvent
from .model_client import ModelClient
from .motivation_evaluator import MotivationProfile
from .prompts import (
    build_long_term_think_prompt,
    build_pre_speak_thought_prompt,
    build_speaking_drive_prompts,
)
from .types import DECISION_MIN_MAX_TOKENS, ProviderCapability

# 决策 JSON 解析相关（从原 InitiativeTimer 迁移）
_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)

# 决策/思考类调用的 max_tokens 下限统一走 types.DECISION_MIN_MAX_TOKENS：
# thinking 模型的内部思考会消耗 max_tokens 预算（实测 kimi-k2.5 在 300 时
# 299 token 全被思考烧掉，正文挤成空串导致决策 JSON 永远解析失败）。
_DECISION_MIN_MAX_TOKENS = DECISION_MIN_MAX_TOKENS

# 对话欲评估（§7.3，2026-07-30 用户定稿）：ThinkEngine 用四维心情模型打分，
# total_drive 超 drive_threshold 即「想说」，否则沉默。无累积器、无犹豫链、
# 无强制 fallback；LLM 只负责打分与候选内容，二元判断由阈值独立完成。
_SPEAKING_DRIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "delay_seconds": {"type": "integer"},
        "reason": {"type": "string"},
        "enthusiasm": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "motivation": {
            "type": "object",
            "properties": {
                "expression_drive": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "emotional_charge": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "relational_need": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "situational_relevance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": [
                "expression_drive",
                "emotional_charge",
                "relational_need",
                "situational_relevance",
            ],
            "additionalProperties": False,
        },
    },
    "required": ["message", "delay_seconds", "reason", "enthusiasm", "motivation"],
    "additionalProperties": False,
}
_SPEAKING_DRIVE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "speaking_drive_evaluation",
        "strict": True,
        "schema": _SPEAKING_DRIVE_SCHEMA,
    },
}

# 对话欲阈值默认值（initiative_timer_config 缺失时的兜底）
_SPEAKING_DRIVE_DEFAULT_THRESHOLD = 0.6


class SpeakingDriveDecision(Struct):
    """四维心情打分后的主动发言决策（阈值二元判断，无累积器）。

    LLM 只产出四维动机与候选内容；`want_speak` 由代码按
    `total_drive >= drive_threshold` 独立判定，模型不参与「说不说」的决定。
    """

    want_speak: bool
    total_drive: float
    motivation: MotivationProfile = field(default_factory=MotivationProfile)
    message: str = ""  # 候选发言/意图摘要（超阈值时非空）
    delay_seconds: int = 120  # 建议延迟（主动定时器路径使用）
    enthusiasm: float = 0.5
    reason: str = ""


class ThinkEngine:
    """思考引擎 - 负责所有 AI 思考活动"""

    def __init__(
        self,
        semantic_memory: SemanticMemoryManager,
        model_client: ModelClient,
        event_bus: EventBus,
        character_name: str,
        config: ThinkEngineConfig,
        initiative_timer_config: InitiativeTimerConfig | None = None,
        debug_silent_output: bool = False,
    ) -> None:
        self.semantic_memory = semantic_memory
        self.model_client = model_client
        self.event_bus = event_bus
        self.character_name = character_name
        self.config = config
        self.initiative_timer_config = initiative_timer_config
        self.debug_silent_output = debug_silent_output

        # 长期思考状态
        self._running = False
        self._long_term_task: asyncio.Task | None = None
        self._last_long_term_time: datetime | None = None
        self._long_term_interval = timedelta(minutes=config.think_interval_minutes)

    def update_semantic_memory(self, semantic_memory: SemanticMemoryManager) -> None:
        """会话切换后就地更新语义记忆引用（不中断长期思考循环）。"""
        self.semantic_memory = semantic_memory

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        """启动思考引擎（仅启动长期思考循环）"""
        if self._running or not self.config.enabled:
            return

        self._running = True
        self._long_term_task = asyncio.create_task(self._long_term_loop())
        logger.info(
            f"🧠 [ThinkEngine] 思考引擎已启动 (角色: {self.character_name}, "
            f"长期思考间隔: {self.config.think_interval_minutes}分钟)"
        )

    async def stop(self) -> None:
        """停止思考引擎"""
        self._running = False
        if self._long_term_task:
            self._long_term_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._long_term_task
        logger.info(f"🧠 [ThinkEngine] 思考引擎已停止 (角色: {self.character_name})")

    # ==================== 长期思考（定时话题游走）====================

    async def _long_term_loop(self) -> None:
        """长期思考主循环"""
        while self._running:
            try:
                await asyncio.sleep(self._long_term_interval.total_seconds())

                if not self._running:
                    break

                await self._long_term_think()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"长期思考循环异常: {e}")

    async def _long_term_think(self) -> None:
        """随机游走话题图谱，产生联想（长期思考）"""
        store = self.semantic_memory.store
        topics = store.get_all_topics()

        if not topics:
            logger.debug(f"🧠 [ThinkEngine] {self.character_name} 没有话题可思考")
            return

        # 优先选择高情感值的话题，但刚刚思考过的话题会进入冷却
        threshold = self.config.emotional_trigger_threshold
        emotional_topics = [t for t in topics if abs(t.emotional_valence) > threshold]

        now = utc_now()

        def _topic_weight(topic: Topic) -> float:
            base = 1.0
            emotional = 1.0 + abs(topic.emotional_valence) * 2.0
            freshness = 1.0
            if topic.last_thought_at is not None:
                minutes_since = (now - topic.last_thought_at).total_seconds() / 60.0
                cooldown = max(1.0, float(self.config.think_cooldown_minutes))
                freshness = min(1.0, max(0.05, minutes_since / cooldown))
            return base * emotional * freshness

        if emotional_topics and random.random() < self.config.emotional_priority_probability:
            weights = [_topic_weight(t) for t in emotional_topics]
            start_topic = random.choices(emotional_topics, weights=weights, k=1)[0]
            logger.debug(
                f"🧠 [ThinkEngine] {self.character_name} 优先选择高情感话题: {start_topic.name} "
                f"(权重: {_topic_weight(start_topic):.2f})"
            )
        else:
            weights = [_topic_weight(t) for t in topics]
            start_topic = random.choices(topics, weights=weights, k=1)[0]
            logger.debug(
                f"🧠 [ThinkEngine] {self.character_name} 选择话题: {start_topic.name} "
                f"(权重: {_topic_weight(start_topic):.2f})"
            )

        # 随机游走
        walk = [start_topic]
        current = start_topic
        steps = random.randint(self.config.random_walk_steps_min, self.config.random_walk_steps_max)
        visited = {start_topic.id}

        for _ in range(steps):
            neighbors = list(current.related_topics.keys())
            if self.config.walk_visit_dedup:
                neighbors = [n for n in neighbors if n not in visited and n in store._topics]
            if neighbors:
                weights = [current.related_topics[n] for n in neighbors]
                next_id = random.choices(neighbors, weights=weights)[0]
                current = store.get_topic_by_id(next_id)
                if current:
                    walk.append(current)
                    visited.add(current.id)
                else:
                    break
            else:
                break

        # 构建思考提示
        walk_desc = "\n".join(
            f"- {t.name}: {t.summary} (情感: {t.emotional_valence:.2f})" for t in walk
        )

        prompt = build_long_term_think_prompt(self.character_name, walk_desc)

        logger.debug(
            f"🧠 [ThinkEngine] {self.character_name} 正在长期思考，游走话题: {[t.name for t in walk]}"
        )

        try:
            response = await self.model_client.chat(
                messages=[{"role": "system", "content": prompt}],
                options={
                    "temperature": self.config.think_temperature,
                    # thinking 模型的思考消耗预算，抬下限避免正文被挤空
                    "num_predict": max(self.config.think_max_tokens, _DECISION_MIN_MAX_TOKENS),
                },
            )

            thought = response.message.content
            if thought:
                if self.debug_silent_output:
                    logger.info(
                        f"💭 [ThinkEngine] {self.character_name} 内心独白: {thought[:100]}..."
                    )
                else:
                    logger.debug(
                        f"💭 [ThinkEngine] {self.character_name} 产生长期思考（调试输出关闭，内容已隐藏）"
                    )

                self.event_bus.publish(
                    Event(
                        type=SystemEvent.THINK_ENGINE_THOUGHT,
                        source="think_engine",
                        data={
                            "character": self.character_name,
                            "thought": thought,
                            "topics": [t.name for t in walk],
                            "topics_detail": [
                                {
                                    "name": t.name,
                                    "summary": t.summary,
                                    "emotional_valence": t.emotional_valence,
                                }
                                for t in walk[:3]
                            ],
                        },
                    )
                )

                for topic in walk:
                    store.mark_topic_thought(topic.id)
            else:
                logger.debug(f"🤫 [ThinkEngine] {self.character_name} 长期思考了但内容为空")

        except Exception as e:
            logger.error(f"长期思考失败: {e}")

    def trigger_think_now(self) -> None:
        """立即触发一次长期思考"""
        if self._running:
            asyncio.create_task(self._long_term_think())
            logger.debug(f"🧠 [ThinkEngine] {self.character_name} 手动触发长期思考")

    # ==================== 短期思考（回复后主动决策）====================

    # ==================== 说话前思考（定时器到期前）====================

    async def pre_speak_thought(
        self,
        pending_summary: str,
        recent_context: str,
        *,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> str:
        """说话前思考：定时器到期后，生成主动消息前的内部思考。

        返回思考文本（可能为空字符串）。
        """
        thought_prompt = build_pre_speak_thought_prompt(
            self.character_name, pending_summary, recent_context
        )

        logger.trace(f"[ThinkEngine] 说话前思考 prompt:\n{thought_prompt}")

        try:
            thought_max_tokens = max(max_tokens, _DECISION_MIN_MAX_TOKENS)
            response = await self.model_client.chat(
                messages=[{"role": "system", "content": thought_prompt}],
                options={
                    "temperature": temperature,
                    "num_predict": thought_max_tokens,
                    "max_tokens": thought_max_tokens,
                },
            )
            content = response.message.content
            thought = content.strip() if isinstance(content, str) else ""
            logger.debug(f"[ThinkEngine] 说话前思考结果: {thought[:100]}...")
            return thought
        except Exception as error:
            logger.error(f"说话前思考失败: {error}")
            return ""

    # ==================== 对话欲短期思考（§7.3：四维动机 + 智能调度）====================

    async def evaluate_speaking_drive(
        self,
        trigger_text: str,
        recent_messages: list[dict[str, Any]],
        *,
        min_delay_seconds: int = 30,
        max_delay_seconds: int = 1800,
        decision_max_tokens: int = 300,
        decision_temperature: float = 0.4,
    ) -> SpeakingDriveDecision | None:
        """对话欲统一评估：四维心情模型打分 + 阈值二元判断（ThinkEngine 决策区）。

        LLM 一次短 JSON 调用：四维动机 + 候选发言 + 建议延迟 + 热情度。
        「说不说」不由模型回答——代码按 `total_drive >= drive_threshold` 独立判定，
        无累积器、无犹豫链、无强制 fallback。两个调用方：
        ActionPlanner 主动说话（用 message 作发言内容）与主动定时器调度
        （用 message 作意图摘要、delay_seconds 排定时器）。
        """
        context_text = self._format_context_for_decision(recent_messages, trigger_text)
        threshold = (
            self.initiative_timer_config.drive_threshold
            if self.initiative_timer_config is not None
            else _SPEAKING_DRIVE_DEFAULT_THRESHOLD
        )

        system_prompt, user_prompt = build_speaking_drive_prompts(
            self.character_name,
            trigger_text,
            context_text,
            min_delay_seconds,
            max_delay_seconds,
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        logger.trace(
            f"[ThinkEngine] 对话欲评估请求 messages:\n"
            f"{json.dumps(messages, ensure_ascii=False, indent=2, default=str)}"
        )

        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                max_tok = max(decision_max_tokens, _DECISION_MIN_MAX_TOKENS)
                options: dict[str, Any] = {
                    "temperature": decision_temperature,
                    "num_predict": max_tok,
                    "max_tokens": max_tok,
                }
                if self._supports_structured_output():
                    options["response_format"] = _SPEAKING_DRIVE_RESPONSE_FORMAT

                response = await self.model_client.chat(
                    messages=messages,
                    options=options,
                )
                content = response.message.content
                text = content.strip() if isinstance(content, str) else ""
                logger.trace(f"[ThinkEngine] 对话欲评估原始响应: {text!r}")
                decision = self._parse_speaking_drive(text, threshold=threshold)
                if decision is not None:
                    logger.debug(
                        f"[ThinkEngine] 对话欲评估: total={decision.total_drive:.2f} "
                        f"(阈值 {threshold:.2f}) want_speak={decision.want_speak}, "
                        f"motivation={decision.motivation.to_prompt_context()}"
                    )
                    return decision

                if attempt < max_retries:
                    logger.warning("对话欲评估未返回合法 JSON，准备重试一次")
                    messages.append({"role": "assistant", "content": text[:1000]})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "你上一条回复不是合法的 JSON。请严格按照要求只输出 JSON 对象，"
                                "不要写成角色台词、对白或解释。请重试。"
                            ),
                        }
                    )
                    continue

                return None
            except Exception as error:
                logger.error(f"对话欲评估失败: {error}")
                return None

        return None

    @staticmethod
    def _parse_speaking_drive(text: str, *, threshold: float) -> SpeakingDriveDecision | None:
        """解析对话欲评估 JSON 并按阈值给出二元判断；motivation 缺失/畸形按零动机。"""
        match = _JSON_OBJECT_PATTERN.search(text)
        raw = match.group(0) if match else text
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            preview = raw.replace("\r", "\\r").replace("\n", "\\n")[:300]
            logger.error(f"对话欲评估 JSON 解析失败: {error}; raw={preview!r}")
            return None
        if not isinstance(data, dict):
            return None

        def _clamp01(value: Any) -> float:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return 0.0
            return max(0.0, min(1.0, float(value)))

        raw_motivation = data.get("motivation")
        motivation = MotivationProfile()
        if isinstance(raw_motivation, dict):
            motivation = MotivationProfile(
                expression_drive=_clamp01(raw_motivation.get("expression_drive")),
                emotional_charge=_clamp01(raw_motivation.get("emotional_charge")),
                relational_need=_clamp01(raw_motivation.get("relational_need")),
                situational_relevance=_clamp01(raw_motivation.get("situational_relevance")),
            )

        total_drive = motivation.total_drive
        message = str(data.get("message") or "").strip()
        delay = data.get("delay_seconds")
        enthusiasm = data.get("enthusiasm")
        return SpeakingDriveDecision(
            want_speak=total_drive >= threshold and bool(message),
            total_drive=total_drive,
            motivation=motivation,
            message=message,
            delay_seconds=delay if isinstance(delay, int) and delay > 0 else 300,
            enthusiasm=_clamp01(enthusiasm) if isinstance(enthusiasm, int | float) else 0.5,
            reason=str(data.get("reason") or "").strip(),
        )

    # ==================== 辅助方法 ====================

    @staticmethod
    def _format_context_for_decision(
        recent_messages: list[dict[str, Any]], current_response: str
    ) -> str:
        """把近期对话格式化为决策上下文，避免重复放入当前刚生成的回复。"""
        lines = []
        for item in recent_messages:
            role = item.get("role")
            content = item.get("content")
            if not isinstance(role, str) or not isinstance(content, str):
                continue
            if role not in {"user", "assistant"}:
                continue
            # 避免把刚生成的 assistant 回复再当成上下文末尾
            if role == "assistant" and content.strip() == current_response.strip():
                continue
            label = "User" if role == "user" else "(角色)"
            lines.append(f"{label}: {content.strip()}")
        if not lines:
            return "（无更早上下文）"
        return "\n".join(lines)

    def _supports_structured_output(self) -> bool:
        supports = getattr(self.model_client, "supports", None)
        if callable(supports):
            try:
                return bool(supports(ProviderCapability.STRUCTURED_OUTPUT))
            except Exception as error:
                logger.warning(f"结构化输出能力判断失败: {error}")
        return False
