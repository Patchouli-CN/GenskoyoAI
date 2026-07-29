"""动机评估器 - 计算角色说话的驱动力

古明地觉：动机就在你的潜意识里，让我帮你量化它们。
"""

import json
import re
from typing import TYPE_CHECKING

from msgspec import Struct

from ...utils.logger import logger
from .types import DECISION_MIN_MAX_TOKENS

if TYPE_CHECKING:
    from .model_client import ModelClient


class MotivationProfile(Struct):
    """动机画像"""

    expression_drive: float = 0.0  # 表达欲：有话想说的冲动
    emotional_charge: float = 0.0  # 情感驱动力：当前情绪想释放
    relational_need: float = 0.0  # 关系需求：想和对方互动
    situational_relevance: float = 0.0  # 情景相关性：话题和当前场景的匹配度

    @property
    def total_drive(self) -> float:
        """综合驱动力"""
        return (
            self.expression_drive * 0.3
            + self.emotional_charge * 0.35
            + self.relational_need * 0.2
            + self.situational_relevance * 0.15
        )

    def to_prompt_context(self) -> str:
        return (
            f"表达欲: {self.expression_drive:.2f} | "
            f"情感驱动: {self.emotional_charge:.2f} | "
            f"关系需求: {self.relational_need:.2f} | "
            f"情景相关: {self.situational_relevance:.2f}"
        )


class MotivationEvaluator:
    """动机评估器 - 量化角色说话的冲动"""

    def __init__(self, character_name: str, model_client: ModelClient):
        self.character_name = character_name
        self.model_client = model_client

    async def evaluate(
        self,
        thought: str,
        emotional_valence: float,
        topics_detail: list[dict],
        last_interaction_minutes: float = 5.0,
    ) -> MotivationProfile:
        """评估当前动机"""

        topics_desc = (
            "\n".join(
                f"- {t.get('name', '')} (情感: {t.get('emotional_valence', 0):.2f})"
                for t in topics_detail[:5]
            )
            if topics_detail
            else "无特定话题"
        )

        prompt = f"""分析 {self.character_name} 当前的心理动机：

内心思考：{thought}
相关话题及情感：
{topics_desc}
整体情感效价：{emotional_valence:.2f}（正=愉悦，负=低落）
距离上次互动：{last_interaction_minutes:.0f} 分钟

请从以下四个维度量化角色的动机（每项 0.0-1.0），只返回 JSON：
{{
    "expression_drive": 0.0,    // 表达欲：思考内容本身是否催生表达冲动
    "emotional_charge": 0.0,    // 情感驱动力：情绪是否需要一个出口
    "relational_need": 0.0,     // 关系需求：是否想拉近/回应对方
    "situational_relevance": 0.0  // 情景相关性：话题是否适合当前开口
}}

只输出 JSON。"""

        try:
            response = await self.model_client.chat(
                messages=[{"role": "user", "content": prompt}],
                # thinking 模型的思考消耗预算，抬下限防止正文被挤空
                options={
                    "temperature": 0.3,
                    "num_predict": max(200, DECISION_MIN_MAX_TOKENS),
                },
            )
            content = response.message.content
            text = content.strip() if isinstance(content, str) else ""
            match = re.search(r"\{[^{}]*\}", text)
            if match:
                data = json.loads(match.group())
                return MotivationProfile(
                    expression_drive=float(data.get("expression_drive", 0)),
                    emotional_charge=float(data.get("emotional_charge", 0)),
                    relational_need=float(data.get("relational_need", 0)),
                    situational_relevance=float(data.get("situational_relevance", 0)),
                )
        except Exception as e:
            logger.warning(f"动机评估失败: {e}")

        # 降级：基于情感效价的简单推断
        abs_val = abs(emotional_valence)
        return MotivationProfile(
            expression_drive=min(abs_val * 0.8, 1.0),
            emotional_charge=min(abs_val * 1.2, 1.0),
            relational_need=0.3,
            situational_relevance=0.5,
        )
