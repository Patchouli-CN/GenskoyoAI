"""注意力事务（AttentionThings）：候选预筛 → 一次性 LLM 判定 → 输出判定结果。

通用管线（2026-08-02 用户定稿「设计得足够通用」）：

- **事务种类可注册扩展**：一个 `AttentionKind` = 免费预筛（candidate）+
  判定 prompt（judge_prompt）+ 判定输出解析（parse）。提醒（reminder）
  是第一个种类（在 nb2 适配器注册）；新事务（如「对方求帮助」「该沉默」）
  按同一形状注册即可。
- **成本可控**：只有预筛命中的消息才花那一次判定调用（一次性脱稿生成
  OneShotGenerator，不进任何会话）；预筛是纯正则/关键词，零成本。
- **判定与处置分离**：本类只产出 `AttentionVerdict`；命中后做什么由
  接入方（如 nb2 的「自动登记提醒」代办）决定——不依赖主生成模型的
  工具纪律（群聊噪声下工具漏调六连的教训）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ...utils.logger import logger


class AttentionKind(Protocol):
    """一种注意力事务的判定契约（判定输出解析由种类自定 schema）。"""

    name: str

    def candidate(self, text: str) -> bool:
        """免费预筛：返回 True 才值得花一次 LLM 判定（纯正则/关键词）。"""
        ...

    def judge_prompt(self, text: str) -> str:
        """构造判定 prompt（要求模型只输出 JSON）。"""
        ...

    def parse(self, raw: str) -> dict[str, Any] | None:
        """解析判定输出；非命中/无法解析返回 None。"""
        ...


@dataclass(frozen=True)
class AttentionVerdict:
    """一条注意力判定结果（kind 名 + 种类自定的数据载荷）。"""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


class AttentionThings:
    """注意力事务管线：对每条消息做「预筛 → 判定 → 产出 verdict」。

    generate 为一次性脱稿生成通道（prompt -> 文本），由接入方注入
    （如 nb2 用 host.generate_meta_text 包装）。判定失败一律静默跳过
    （注意力是增强，绝不能拖垮主回复）。
    """

    def __init__(self, generate: Callable[[str], Awaitable[str]], *, enabled: bool = True) -> None:
        self._generate = generate
        self.enabled = enabled
        self._kinds: list[AttentionKind] = []

    def register(self, kind: AttentionKind) -> None:
        """注册一种注意力事务（重复注册同名忽略）。"""
        if any(existing.name == kind.name for existing in self._kinds):
            return
        self._kinds.append(kind)
        logger.debug(f"[attention] 注意力事务已注册: {kind.name}")

    async def inspect(self, text: str, *, only: set[str] | None = None) -> list[AttentionVerdict]:
        """对一段消息文本做注意力判定；返回命中的 verdict 列表（可能为空）。

        only：只跑指定名字的种类（如私聊不跑 reply_at，省一次判定调用）；
        None 表示全部种类。
        """
        if not self.enabled or not text.strip():
            return []
        verdicts: list[AttentionVerdict] = []
        for kind in self._kinds:
            if only is not None and kind.name not in only:
                continue
            if not kind.candidate(text):
                continue  # 预筛不过：零成本
            try:
                raw = await self._generate(kind.judge_prompt(text))
                data = kind.parse(raw)
            except Exception as error:
                logger.debug(f"[attention] {kind.name} 判定失败（忽略）: {error}")
                continue
            if data is not None:
                verdicts.append(AttentionVerdict(kind=kind.name, data=data))
        return verdicts
