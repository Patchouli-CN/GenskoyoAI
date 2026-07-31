"""world 层 LLM JSON 小工具：director / memory_projector / initiative 共用。

统一三处逐字重复的 JSON 提取、结构化输出能力探测与数值钳制。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.agent.types import ProviderCapability
from ..utils.logger import logger

_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str, *, log_prefix: str) -> dict[str, Any] | None:
    """从 LLM 输出提取并解析首个 JSON 对象；失败返回 None（记错误日志）。"""
    match = _JSON_OBJECT_PATTERN.search(text)
    raw = match.group(0) if match else text
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        preview = raw.replace("\r", "\\r").replace("\n", "\\n")[:300]
        logger.error(f"{log_prefix} JSON 解析失败: {error}; raw={preview!r}")
        return None
    if not isinstance(data, dict):
        logger.error(f"{log_prefix} JSON 不是对象: {type(data).__name__}")
        return None
    return data


def supports_structured_output(model_client: Any, *, log_prefix: str) -> bool:
    """模型客户端结构化输出能力探测（异常时保守返回 False）。"""
    supports = getattr(model_client, "supports", None)
    if callable(supports):
        try:
            return bool(supports(ProviderCapability.STRUCTURED_OUTPUT))
        except Exception as error:
            logger.warning(f"{log_prefix} 结构化输出能力判断失败: {error}")
    return False


def clamp_number(raw: Any, low: float, high: float, default: float) -> float:
    """数值钳制到 [low, high]；非数值（含 bool）回落 default。"""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return default
    return max(low, min(high, float(raw)))


def clamp01_number(raw: Any, default: float = 0.0) -> float:
    """数值钳制到 [0, 1]；非数值（含 bool）回落 default。"""
    return clamp_number(raw, 0.0, 1.0, default)
