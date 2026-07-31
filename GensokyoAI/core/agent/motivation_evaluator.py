"""动机画像 - 四维心情模型的量化结构。

古明地觉：动机就在你的潜意识里，让我帮你量化它们。

评估与决策已统一收归 ThinkEngine（模块化决策区，见
`ThinkEngine.evaluate_speaking_drive`）；本模块只保留共享的数据结构。
"""

from msgspec import Struct, field

from ..config_schema import MotivationWeightsConfig


class MotivationProfile(Struct):
    """动机画像；weights 来自角色卡 motivation_weights（默认通用人格基线）。"""

    expression_drive: float = 0.0  # 表达欲：有话想说的冲动
    emotional_charge: float = 0.0  # 情感驱动力：当前情绪想释放
    relational_need: float = 0.0  # 关系需求：想和对方互动
    situational_relevance: float = 0.0  # 情景相关性：话题和当前场景的匹配度
    weights: MotivationWeightsConfig = field(default_factory=MotivationWeightsConfig)

    @property
    def total_drive(self) -> float:
        """综合驱动力（按角色卡权重加权）"""
        w = self.weights
        return (
            self.expression_drive * w.expression_drive
            + self.emotional_charge * w.emotional_charge
            + self.relational_need * w.relational_need
            + self.situational_relevance * w.situational_relevance
        )

    def to_prompt_context(self) -> str:
        return (
            f"表达欲: {self.expression_drive:.2f} | "
            f"情感驱动: {self.emotional_charge:.2f} | "
            f"关系需求: {self.relational_need:.2f} | "
            f"情景相关: {self.situational_relevance:.2f}"
        )
