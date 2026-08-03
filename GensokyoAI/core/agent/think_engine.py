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
from datetime import timedelta
from typing import Any

from msgspec import Struct, field

from ...memory.decay import filter_active_topics
from ...memory.semantic import SemanticMemoryManager
from ...memory.types import Topic
from ...utils.helpers import utc_now
from ...utils.logger import logger
from ..config import InitiativeTimerConfig, ThinkEngineConfig
from ..config_schema import MotivationWeightsConfig
from ..events import Event, EventBus, SystemEvent
from .emotion import Emotion, EmotionState
from .model_client import ModelClient
from .motivation_evaluator import MotivationProfile
from .prompts import (
    build_emotion_tone_context,
    build_long_term_think_prompt,
    build_memory_distill_prompt,
    build_pre_speak_thought_prompt,
    build_speaking_drive_prompts,
)
from .types import DECISION_MIN_MAX_TOKENS, ProviderCapability

# 决策 JSON 解析相关（从原 InitiativeTimer 迁移）
_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_PATTERN = re.compile(r"\[.*\]", re.DOTALL)

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
        "emotion": {
            "type": "object",
            "properties": {
                "anger": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "sorrow": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "fear": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "happy": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "love": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "surprised": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "disgust": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "shame": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": [
                "anger",
                "sorrow",
                "fear",
                "happy",
                "love",
                "surprised",
                "disgust",
                "shame",
            ],
            "additionalProperties": False,
        },
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
    "required": ["message", "delay_seconds", "reason", "enthusiasm", "emotion", "motivation"],
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
    emotion: Emotion = field(default_factory=Emotion)  # 本次自评后的八维情绪状态
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
        motivation_weights: MotivationWeightsConfig | None = None,
        emotion_baseline: Emotion | None = None,
        log_label: str | None = None,
    ) -> None:
        self.semantic_memory = semantic_memory
        self.model_client = model_client
        self.event_bus = event_bus
        self.character_name = character_name
        self.config = config
        self.initiative_timer_config = initiative_timer_config
        self.debug_silent_output = debug_silent_output
        # 日志租户后缀（Runtime 多租户下区分各租户的同款日志）
        self._log_suffix = f", 租户: {log_label}" if log_label else ""
        # 四维心情权重（角色卡 motivation_weights）；None = 通用人格基线
        self._motivation_weights = motivation_weights or MotivationWeightsConfig()
        # 八维情绪状态机（角色卡 emotion_baseline 为初始/衰减基线）：
        # 由对话欲评估中 LLM 顺带自评驱动（零新增调用），喂给回复语气与评估输入
        self.emotion_state = EmotionState(emotion_baseline)
        # 定期记忆蒸馏的轮次计数（§8.29）
        self._distill_pending_turns = 0

        # 长期思考状态
        self._running = False
        self._long_term_task: asyncio.Task | None = None
        self._long_term_interval = timedelta(minutes=config.think_interval_minutes)

    def update_semantic_memory(self, semantic_memory: SemanticMemoryManager) -> None:
        """会话切换后就地更新语义记忆引用（不中断长期思考循环）。"""
        self.semantic_memory = semantic_memory
        self._distill_pending_turns = 0  # 换会话重新计蒸馏轮次

    def emotion_context_line(self) -> str:
        """当前情绪状态的一行描述（全平稳时为空串，不注入）。"""
        return self.emotion_state.context_line()

    def emotion_tone_context(self) -> str:
        """完整的情绪语气注入上下文（含行为倾向）；全平稳时为空串。"""
        line = self.emotion_state.context_line()
        if not line:
            return ""
        return build_emotion_tone_context(line, self.emotion_state.current.behavior_tendency())

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        """启动思考引擎（仅启动长期思考循环）"""
        if self._running or not self.config.enabled:
            return

        self._running = True
        self._long_term_task = asyncio.create_task(self._long_term_loop())
        logger.info(
            f"🧠 [ThinkEngine] 思考引擎已启动 (角色: {self.character_name}{self._log_suffix}, "
            f"长期思考间隔: {self.config.think_interval_minutes}分钟)"
        )

    async def stop(self) -> None:
        """停止思考引擎"""
        self._running = False
        if self._long_term_task:
            self._long_term_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._long_term_task
        logger.info(f"🧠 [ThinkEngine] 思考引擎已停止 (角色: {self.character_name}{self._log_suffix})")

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

        # 话题热度淘汰（memory.topic_decay_*）：冷话题对主动思考隐藏，
        # 不被想起也不被删除，后续对话刷新时间戳后自然复活
        memory_config = getattr(self.semantic_memory, "config", None)
        if getattr(memory_config, "topic_decay_enabled", False):
            active_topics = filter_active_topics(
                topics,
                half_life_hours=getattr(memory_config, "topic_half_life_hours", 72.0),
                prune_threshold=getattr(memory_config, "topic_decay_threshold", 0.1),
                pin_importance=getattr(memory_config, "topic_pin_importance", 8.0),
            )
            hidden_count = len(topics) - len(active_topics)
            if hidden_count:
                logger.debug(
                    f"🧠 [ThinkEngine] {self.character_name} 有 {hidden_count} 个冷话题已被遗忘（未删除）"
                )
            topics = active_topics
            if not topics:
                logger.debug(f"🧠 [ThinkEngine] {self.character_name} 话题已全部冷却，没有可思考的")
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
                call_context="think_engine",
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

    # ==================== 定期记忆蒸馏（§8.29） ====================

    def note_turn_for_distillation(self, recent_messages: list[dict[str, Any]]) -> None:
        """每完成一轮回复计一次；达到 memory.distill_turns 时后台蒸馏一次。

        确定性周期触发（替代已删除的 AI 主动记忆工具）；
        调用方以主动机制总闸隔离元租户与 World Actor。
        """
        memory_config = getattr(self.semantic_memory, "config", None)
        if not getattr(memory_config, "distill_enabled", False):
            return
        self._distill_pending_turns += 1
        if self._distill_pending_turns < getattr(memory_config, "distill_turns", 10):
            return
        self._distill_pending_turns = 0
        asyncio.create_task(self.distill_memories(recent_messages))

    async def distill_memories(self, recent_messages: list[dict[str, Any]]) -> int:
        """从近期工作记忆提炼「珍贵记忆」写入语义记忆；返回写入条数。"""
        lines = []
        for item in recent_messages:
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            label = "User" if role == "user" else f"({self.character_name})"
            lines.append(f"{label}: {content.strip()}")
        if not lines:
            return 0
        conversation = "\n".join(lines)[-4000:]
        prompt = build_memory_distill_prompt(self.character_name, conversation)
        try:
            max_tok = max(300, _DECISION_MIN_MAX_TOKENS)
            response = await self.model_client.chat(
                messages=[{"role": "system", "content": prompt}],
                options={
                    "temperature": 0.4,
                    "num_predict": max_tok,
                    "max_tokens": max_tok,
                },
                call_context="think_engine",
            )
            content = response.message.content
            text = content.strip() if isinstance(content, str) else ""
        except Exception as error:
            logger.error(f"[ThinkEngine] 记忆蒸馏调用失败: {error}")
            return 0

        items = self._parse_distill_items(text)
        written = 0
        for item in items:
            try:
                await self.semantic_memory.add_async(
                    content=item["content"],
                    importance=item["importance"],
                    emotional_valence=item["emotional_valence"],
                    topic_name=item.get("topic"),
                )
                written += 1
            except Exception as error:
                logger.warning(f"[ThinkEngine] 蒸馏记忆写入失败: {error}")
        if written:
            logger.info(f"[ThinkEngine] 定期蒸馏完成：写入 {written} 条珍贵记忆")
        else:
            logger.debug("[ThinkEngine] 定期蒸馏：本轮没有值得记住的内容")
        return written

    @staticmethod
    def _parse_distill_items(text: str) -> list[dict[str, Any]]:
        """解析蒸馏 JSON 数组（最多 3 条；畸形一律为空）。"""
        match = _JSON_ARRAY_PATTERN.search(text)
        raw = match.group(0) if match else text
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"[ThinkEngine] 蒸馏结果 JSON 解析失败: {raw[:120]!r}")
            return []
        if not isinstance(data, list):
            return []

        items = []
        for entry in data[:3]:
            if not isinstance(entry, dict):
                continue
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            raw_importance = entry.get("importance")
            importance = (
                max(0.0, min(1.0, float(raw_importance) / 10.0))
                if isinstance(raw_importance, int | float) and not isinstance(raw_importance, bool)
                else 0.5
            )
            valence = entry.get("emotional_valence")
            items.append(
                {
                    "content": content,
                    "importance": importance,
                    "emotional_valence": (
                        max(-1.0, min(1.0, float(valence)))
                        if isinstance(valence, int | float) and not isinstance(valence, bool)
                        else 0.0
                    ),
                    "topic": str(entry.get("topic") or "").strip() or None,
                }
            )
        return items

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
                call_context="think_engine",
            )
            content = response.message.content
            thought = content.strip() if isinstance(content, str) else ""
            logger.trace(f"[ThinkEngine] 说话前思考结果: {thought[:100]}...")
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
        base_threshold = (
            self.initiative_timer_config.drive_threshold
            if self.initiative_timer_config is not None
            else _SPEAKING_DRIVE_DEFAULT_THRESHOLD
        )
        # 情绪调制阈值（§8.25）：消沉少言、兴致话多；二元判断结构不变，
        # 最终阈值钳制在 [0.3, 0.9]
        adjust = self.emotion_state.current.threshold_adjustment()
        threshold = max(0.3, min(0.9, base_threshold + adjust))

        system_prompt, user_prompt = build_speaking_drive_prompts(
            self.character_name,
            trigger_text,
            context_text,
            min_delay_seconds,
            max_delay_seconds,
            emotion_line=self.emotion_state.context_line(),
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        logger.opt(lazy=True).trace(
            "[ThinkEngine] 对话欲评估请求 messages:\n{dump}",
            dump=lambda: json.dumps(messages, ensure_ascii=False, indent=2, default=str),
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
                    call_context="think_engine",
                )
                content = response.message.content
                text = content.strip() if isinstance(content, str) else ""
                logger.trace(f"[ThinkEngine] 对话欲评估原始响应: {text!r}")
                decision = self._parse_speaking_drive(text, threshold=threshold)
                if decision is not None:
                    emotion_line = self.emotion_state.context_line()
                    logger.trace(
                        f"[ThinkEngine] 对话欲评估: total={decision.total_drive:.2f} "
                        f"(阈值 {threshold:.2f}"
                        + (f"={base_threshold:.2f}{adjust:+.2f}" if adjust else "")
                        + f") want_speak={decision.want_speak}, "
                        f"motivation={decision.motivation.to_prompt_context()}"
                        + (f", emotion={emotion_line}" if emotion_line else "")
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

    def _parse_speaking_drive(self, text: str, *, threshold: float) -> SpeakingDriveDecision | None:
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
        motivation = MotivationProfile(weights=self._motivation_weights)
        if isinstance(raw_motivation, dict):
            motivation = MotivationProfile(
                expression_drive=_clamp01(raw_motivation.get("expression_drive")),
                emotional_charge=_clamp01(raw_motivation.get("emotional_charge")),
                relational_need=_clamp01(raw_motivation.get("relational_need")),
                situational_relevance=_clamp01(raw_motivation.get("situational_relevance")),
                weights=self._motivation_weights,
            )

        # 八维情绪自评（缺失/畸形时不动当前状态——无自评不等于心情清零）
        raw_emotion = data.get("emotion")
        if isinstance(raw_emotion, dict):
            self.emotion_state.update(
                Emotion(
                    **{
                        name: _clamp01(raw_emotion.get(name))
                        for name in (
                            "anger",
                            "sorrow",
                            "fear",
                            "happy",
                            "love",
                            "surprised",
                            "disgust",
                            "shame",
                        )
                    }
                )
            )

        total_drive = motivation.total_drive
        message = str(data.get("message") or "").strip()
        delay = data.get("delay_seconds")
        enthusiasm = data.get("enthusiasm")
        return SpeakingDriveDecision(
            want_speak=total_drive >= threshold and bool(message),
            total_drive=total_drive,
            motivation=motivation,
            emotion=self.emotion_state.current,
            message=message,
            delay_seconds=delay if isinstance(delay, (int, float)) and delay > 0 else 300,
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
