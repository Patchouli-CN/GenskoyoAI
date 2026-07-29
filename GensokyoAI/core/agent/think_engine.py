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
from .types import ProviderCapability

# 决策 JSON 解析相关（从原 InitiativeTimer 迁移）
_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
_INITIATIVE_TIMER_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_schedule": {"type": "boolean"},
        "delay_seconds": {"type": "integer"},
        "summary": {"type": "string"},
        "reason": {"type": "string"},
        "enthusiasm": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "角色当前主动交流的热情度，0~1",
        },
    },
    "required": ["should_schedule", "delay_seconds", "summary", "reason"],
    "additionalProperties": False,
}
_INITIATIVE_TIMER_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "initiative_timer_decision",
        "strict": True,
        "schema": _INITIATIVE_TIMER_DECISION_SCHEMA,
    },
}

# 对话欲短期思考（§7.3）：四维动机 + 智能调度的结构化输出
_DRIVE_INITIATIVE_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_schedule": {"type": "boolean"},
        "delay_seconds": {"type": "integer"},
        "summary": {"type": "string"},
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
    "required": ["should_schedule", "delay_seconds", "summary", "reason", "motivation"],
    "additionalProperties": False,
}
_DRIVE_INITIATIVE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "drive_initiative_decision",
        "strict": True,
        "schema": _DRIVE_INITIATIVE_DECISION_SCHEMA,
    },
}


class DriveDecision(Struct):
    """对话欲路径的短期调度决策：四维动机画像 + 主动发言安排。"""

    should_schedule: bool
    delay_seconds: int
    summary: str
    reason: str = ""
    enthusiasm: float = 0.5
    motivation: MotivationProfile = field(default_factory=MotivationProfile)


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

        prompt = f"""
你现在处于静默状态，正在回顾与用户的过往。

你联想到了以下话题：
{walk_desc}

请在内心思考以下问题（不要输出给用户）：
1. 这些话题之间有什么联系？
2. 它们唤起了你怎样的情感？
3. 你是否有什么想主动对用户说的话或做的事？

只思考，不行动。记住你是{self.character_name}。
"""

        logger.debug(
            f"🧠 [ThinkEngine] {self.character_name} 正在长期思考，游走话题: {[t.name for t in walk]}"
        )

        try:
            response = await self.model_client.chat(
                messages=[{"role": "system", "content": prompt}],
                options={
                    "temperature": self.config.think_temperature,
                    "num_predict": self.config.think_max_tokens,
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

    async def decide_initiative(
        self,
        assistant_response: str,
        recent_messages: list[dict[str, Any]],
        *,
        min_delay_seconds: int = 30,
        max_delay_seconds: int = 1800,
        decision_max_tokens: int = 300,
        decision_temperature: float = 0.4,
        hesitation_round: int = 0,
        hesitation_max_rounds: int = 0,
    ) -> dict[str, Any] | None:
        """短期思考：AI 回复后，决定是否需要设置主动发言定时器。

        返回决策字典或 None（解析失败时）。
        """
        context_text = self._format_context_for_decision(recent_messages, assistant_response)

        hesitation_note = ""
        if hesitation_round > 0:
            remaining = hesitation_max_rounds - hesitation_round
            hesitation_note = f"（注意：这已是第 {hesitation_round} 次请你重新考虑是否主动发言"
            if remaining > 0:
                hesitation_note += f"，你还有 {remaining} 次犹豫机会"
            else:
                hesitation_note += "，这是最后一次机会，若仍不需要则放弃"
            hesitation_note += "。）\n"

        system_prompt = f"""你是 {self.character_name}。

现在不是对用户说话，而是在向 GensokyoAI 系统提交你的内部主动发言决定。
这个决定仍然必须由你以 {self.character_name} 的身份、性格、动机和当前上下文来完成；系统只负责读取你提交的机器可解析状态。

请判断你是否想在稍后主动补充一句话。要求：
- 这是内部决策提交，不是用户可见台词；不要把结果写成角色发言、对白、旁白或解释。
- 这里的"不设置定时器"表示你本轮明确放弃稍后主动补充；如果系统没有其他兜底，这会让你在用户再次输入前不再主动开口。
- 为了更拟真地保留角色的主动性，除非当前上下文确实没有任何自然、必要、符合角色的补充，否则优先设置一个短到中等延迟的定时器。
- 如果设置，只写"稍后主动发言意图的一句话摘要"，不要写完整可发送话术。
- 摘要只描述到点后要围绕什么思考和表达，真正说出口的话会在触发时重新生成。
- 延迟秒数必须在 {min_delay_seconds} 到 {max_delay_seconds} 之间。
- 额外输出一个 0~1 的 enthusiasm（热情度）：越高表示你当前越想主动继续聊，系统会把等待时间按 `delay_seconds * (1 - enthusiasm)` 缩短；如果不确定可填 0.5。
- 只输出一个原始 JSON 对象；不要输出 Markdown 代码块、角色引号、解释文本或任何前后缀。
{hesitation_note}
"""

        user_prompt = f"""你刚刚回复了用户：
{assistant_response}

近期对话上下文：
{context_text}

请根据以上上下文提交决定。输出必须且只能是下面的 JSON 对象，不要有任何其他内容：

{{
  "should_schedule": true/false,
  "delay_seconds": 120,
  "summary": "稍后主动发言意图的一句话摘要",
  "reason": "简短理由",
  "enthusiasm": 0.5
}}
"""

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        logger.trace(
            f"[ThinkEngine] 短期思考（主动决策）请求 messages:\n"
            f"{json.dumps(messages, ensure_ascii=False, indent=2, default=str)}"
        )

        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                max_tok = max(decision_max_tokens, 200)
                options: dict[str, Any] = {
                    "temperature": decision_temperature,
                    "num_predict": max_tok,
                    "max_tokens": max_tok,
                }
                if self._supports_structured_output():
                    options["response_format"] = _INITIATIVE_TIMER_RESPONSE_FORMAT

                response = await self.model_client.chat(
                    messages=messages,
                    options=options,
                )
                content = response.message.content
                text = content.strip() if isinstance(content, str) else ""
                logger.trace(f"[ThinkEngine] 短期思考原始响应: {text!r}")
                data = self._parse_decision_json(text)
                if data is not None:
                    logger.debug(f"[ThinkEngine] 短期思考决策解析成功: {data}")
                    return data

                # 解析失败，尝试一次重试
                if attempt < max_retries:
                    logger.warning("短期思考决策未返回合法 JSON，准备重试一次")
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
                logger.error(f"短期思考（主动决策）失败: {error}")
                return None

        return None

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
        thought_prompt = f"""你是 {self.character_name}。

主动定时器到点了，这表示你已经决定稍后要主动开口。

【待表达意图摘要】
{pending_summary}

【最近对话】
{recent_context or "无"}

请先进行说话前的内部思考：
- 根据当前上下文重新组织这次主动发言的重点。
- 不要判断要不要说；到点即代表要说。
- 不要写最终要发送给用户的完整话术。
- 只输出简短内部思考。"""

        logger.trace(f"[ThinkEngine] 说话前思考 prompt:\n{thought_prompt}")

        try:
            response = await self.model_client.chat(
                messages=[{"role": "system", "content": thought_prompt}],
                options={
                    "temperature": temperature,
                    "num_predict": max_tokens,
                    "max_tokens": max_tokens,
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

    async def decide_drive_initiative(
        self,
        assistant_response: str,
        recent_messages: list[dict[str, Any]],
        *,
        drive: float,
        mood: float,
        min_delay_seconds: int,
        max_delay_seconds: int,
        decision_max_tokens: int,
        decision_temperature: float,
    ) -> DriveDecision | None:
        """对话欲路径的短期思考：四维动机评估 + 智能调度，一次 LLM 完成。

        与旧路径 `decide_initiative` 的区别：注入当前对话欲/心情状态，输出附带
        四维动机画像（回灌累积器）；AI 决定不发言即不发言——没有兜底与强制。
        """
        context_text = self._format_context_for_decision(recent_messages, assistant_response)

        system_prompt = f"""你是 {self.character_name}。

现在不是对用户说话，而是在向 GensokyoAI 系统提交你的内部主动发言决定。
这个决定仍然必须由你以 {self.character_name} 的身份、性格、动机和当前上下文来完成；系统只负责读取你提交的机器可解析状态。

你当前的内在状态（由对话欲模型随时间持续累积）：
- 对话欲强度：{drive:.2f}（0=完全不想说，越高越想主动开口）
- 心情效价：{mood:+.2f}（正=愉悦，负=低落）

请判断你是否想在稍后主动补充一句话。要求：
- 这是内部决策提交，不是用户可见台词；不要把结果写成角色发言、对白、旁白或解释。
- 没有强制安排：当前上下文确实没有自然、必要、符合角色的补充时，坦诚选择不设置定时器——系统尊重你的决定，不会替你强行安排。
- 如果设置，只写"稍后主动发言意图的一句话摘要"，不要写完整可发送话术。
- 摘要只描述到点后要围绕什么思考和表达，真正说出口的话会在触发时重新生成。
- 延迟秒数必须在 {min_delay_seconds} 到 {max_delay_seconds} 之间。
- 额外输出一个 0~1 的 enthusiasm（热情度）：越高表示你当前越想主动继续聊，系统会把等待时间按 `delay_seconds * (1 - enthusiasm)` 缩短；如果不确定可填 0.5。
- 同时用四个维度量化你此刻的动机（各 0~1）：
  expression_drive（表达欲：思考内容本身催生的表达冲动）、
  emotional_charge（情感驱动力：情绪是否需要一个出口）、
  relational_need（关系需求：是否想拉近/回应对方）、
  situational_relevance（情景相关性：此刻开口是否合时宜）。
- 只输出一个原始 JSON 对象；不要输出 Markdown 代码块、角色引号、解释文本或任何前后缀。
"""

        user_prompt = f"""你刚刚回复了用户：
{assistant_response}

近期对话上下文：
{context_text}

请根据以上上下文提交决定。输出必须且只能是下面的 JSON 对象，不要有任何其他内容：

{{
  "should_schedule": true/false,
  "delay_seconds": 120,
  "summary": "稍后主动发言意图的一句话摘要",
  "reason": "简短理由",
  "enthusiasm": 0.5,
  "motivation": {{
    "expression_drive": 0.0,
    "emotional_charge": 0.0,
    "relational_need": 0.0,
    "situational_relevance": 0.0
  }}
}}
"""

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        logger.trace(
            f"[ThinkEngine] 对话欲短期思考请求 messages:\n"
            f"{json.dumps(messages, ensure_ascii=False, indent=2, default=str)}"
        )

        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                max_tok = max(decision_max_tokens, 200)
                options: dict[str, Any] = {
                    "temperature": decision_temperature,
                    "num_predict": max_tok,
                    "max_tokens": max_tok,
                }
                if self._supports_structured_output():
                    options["response_format"] = _DRIVE_INITIATIVE_RESPONSE_FORMAT

                response = await self.model_client.chat(
                    messages=messages,
                    options=options,
                )
                content = response.message.content
                text = content.strip() if isinstance(content, str) else ""
                logger.trace(f"[ThinkEngine] 对话欲短期思考原始响应: {text!r}")
                decision = self._parse_drive_decision(text)
                if decision is not None:
                    logger.debug(
                        f"[ThinkEngine] 对话欲决策: schedule={decision.should_schedule}, "
                        f"motivation={decision.motivation.to_prompt_context()}"
                    )
                    return decision

                if attempt < max_retries:
                    logger.warning("对话欲短期思考未返回合法 JSON，准备重试一次")
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
                logger.error(f"对话欲短期思考失败: {error}")
                return None

        return None

    @staticmethod
    def _parse_drive_decision(text: str) -> DriveDecision | None:
        """解析对话欲决策 JSON；motivation 缺失/畸形时按零动机处理。"""
        match = _JSON_OBJECT_PATTERN.search(text)
        raw = match.group(0) if match else text
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            preview = raw.replace("\r", "\\r").replace("\n", "\\n")[:300]
            logger.error(f"对话欲决策 JSON 解析失败: {error}; raw={preview!r}")
            return None
        if not isinstance(data, dict):
            return None
        if "should_schedule" not in data:
            logger.error(f"对话欲决策缺少 should_schedule: {data!r}")
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

        delay = data.get("delay_seconds")
        enthusiasm = data.get("enthusiasm")
        return DriveDecision(
            should_schedule=bool(data.get("should_schedule")),
            delay_seconds=delay if isinstance(delay, int) and delay > 0 else 300,
            summary=str(data.get("summary") or "").strip(),
            reason=str(data.get("reason") or "").strip(),
            enthusiasm=max(0.0, min(1.0, float(enthusiasm)))
            if isinstance(enthusiasm, int | float) and not isinstance(enthusiasm, bool)
            else 0.5,
            motivation=motivation,
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

    @staticmethod
    def _parse_decision_json(text: str) -> dict[str, Any] | None:
        match = _JSON_OBJECT_PATTERN.search(text)
        raw = match.group(0) if match else text
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            preview = raw.replace("\r", "\\r").replace("\n", "\\n")[:300]
            logger.error(f"决策 JSON 解析失败: {error}; raw={preview!r}")
            return None
        return data if isinstance(data, dict) else None
