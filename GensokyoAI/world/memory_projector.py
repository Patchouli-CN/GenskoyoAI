"""WorldMemoryProjector - 把公开表演投影为各在场角色的私有视角记忆。

一次自动表演段落结束（wait_user）后，用一次批量结构化模型调用为该场景
参与者生成各自视角的 summary / importance / emotional_valence / topic_name，
由 World 写入各 Actor 的语义记忆（话题图）。

铁律：
- 只给亲历/在场角色写入；不在场角色不会知道该场景发生的事（防穿帮）。
- 模型调用失败时使用确定性的公开事实摘要；任何失败都不阻塞用户回复。
"""

from __future__ import annotations

import json
import re
from typing import Any

from msgspec import Struct

from ..core.agent.model_client import ModelClient
from ..core.agent.prompts import build_memory_projection_prompts
from ..core.agent.types import DECISION_MIN_MAX_TOKENS, ProviderCapability
from ..utils.logger import logger
from .types import ActorBrief

_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)

_PROJECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "actor_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "emotional_valence": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                    "topic_name": {"type": "string"},
                },
                "required": [
                    "actor_id",
                    "summary",
                    "importance",
                    "emotional_valence",
                    "topic_name",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}
_PROJECTION_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "world_memory_projection",
        "strict": True,
        "schema": _PROJECTION_SCHEMA,
    },
}

# 降级记忆的固定参数：公开事实、低重要性、中性情感
_FALLBACK_IMPORTANCE = 0.3
_FALLBACK_DIGEST_LIMIT = 120


class PerspectiveMemory(Struct):
    """单个角色的投影结果：写入其语义记忆的一条私有视角记忆。"""

    actor_id: str
    summary: str
    importance: float = _FALLBACK_IMPORTANCE
    emotional_valence: float = 0.0
    topic_name: str = ""


class WorldMemoryProjector:
    """把一段公开表演批量投影为各在场角色的私有视角记忆。"""

    def __init__(
        self,
        model_client: ModelClient,
        *,
        temperature: float = 0.3,
        max_tokens: int = 640,
    ) -> None:
        self._model_client = model_client
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def project(
        self,
        *,
        scene_id: str,
        scene_name: str,
        transcript_text: str,
        participants: list[ActorBrief],
    ) -> list[PerspectiveMemory]:
        """批量生成各参与者视角记忆；任何失败都回退为公开事实摘要。"""
        if not participants or not transcript_text.strip():
            return []
        try:
            memories = await self._request_projection(
                scene_id=scene_id,
                scene_name=scene_name,
                transcript_text=transcript_text,
                participants=participants,
            )
        except Exception as error:
            logger.warning(f"[WorldMemoryProjector] 投影调用失败，使用降级摘要: {error}")
            memories = None
        if memories is None:
            return self._fallback(scene_name, transcript_text, participants)
        return memories

    # ==================== 模型调用与解析 ====================

    async def _request_projection(
        self,
        *,
        scene_id: str,
        scene_name: str,
        transcript_text: str,
        participants: list[ActorBrief],
    ) -> list[PerspectiveMemory] | None:
        messages = self._build_messages(
            scene_id=scene_id,
            scene_name=scene_name,
            transcript_text=transcript_text,
            participants=participants,
        )
        options: dict[str, Any] = {
            "temperature": self._temperature,
            # thinking 模型的思考消耗预算，抬下限防止批量投影正文被挤空
            "num_predict": max(self._max_tokens, DECISION_MIN_MAX_TOKENS),
            "max_tokens": max(self._max_tokens, DECISION_MIN_MAX_TOKENS),
        }
        if self._supports_structured_output():
            options["response_format"] = _PROJECTION_RESPONSE_FORMAT
        response = await self._model_client.chat(messages=messages, options=options)
        content = response.message.content
        text = content.strip() if isinstance(content, str) else ""
        data = self._parse_json(text)
        if data is None:
            return None
        return self._validate(data, participants)

    def _build_messages(
        self,
        *,
        scene_id: str,
        scene_name: str,
        transcript_text: str,
        participants: list[ActorBrief],
    ) -> list[dict[str, str]]:
        participant_lines = "\n".join(
            f"- {brief.actor_id}（{brief.display_name}）" for brief in participants
        )
        system_prompt, user_prompt = build_memory_projection_prompts(
            scene_name=scene_name,
            scene_id=scene_id,
            participant_lines=participant_lines,
            transcript_text=transcript_text,
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        match = _JSON_OBJECT_PATTERN.search(text)
        raw = match.group(0) if match else text
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            preview = raw.replace("\r", "\\r").replace("\n", "\\n")[:300]
            logger.error(f"[WorldMemoryProjector] JSON 解析失败: {error}; raw={preview!r}")
            return None
        return data if isinstance(data, dict) else None

    def _validate(
        self, data: dict[str, Any], participants: list[ActorBrief]
    ) -> list[PerspectiveMemory] | None:
        """校验投影结果：只保留在场角色的有效条目；一条都没有视为失败。"""
        participant_ids = {brief.actor_id for brief in participants}
        raw_memories = data.get("memories")
        if not isinstance(raw_memories, list):
            return None
        memories: list[PerspectiveMemory] = []
        for item in raw_memories:
            if not isinstance(item, dict):
                continue
            actor_id = item.get("actor_id")
            summary = item.get("summary")
            if (
                not isinstance(actor_id, str)
                or actor_id not in participant_ids
                or not isinstance(summary, str)
                or not summary.strip()
            ):
                continue
            topic_name = item.get("topic_name")
            memories.append(
                PerspectiveMemory(
                    actor_id=actor_id,
                    summary=summary.strip(),
                    importance=self._clamp(item.get("importance"), 0.0, 1.0, 0.5),
                    emotional_valence=self._clamp(item.get("emotional_valence"), -1.0, 1.0, 0.0),
                    topic_name=topic_name.strip() if isinstance(topic_name, str) else "",
                )
            )
        return memories or None

    @staticmethod
    def _clamp(raw: Any, low: float, high: float, default: float) -> float:
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            return default
        return max(low, min(high, float(raw)))

    # ==================== 降级 ====================

    @staticmethod
    def _fallback(
        scene_name: str, transcript_text: str, participants: list[ActorBrief]
    ) -> list[PerspectiveMemory]:
        """确定性的公开事实摘要：模型失败时每个在场角色仍有一份记忆可写。"""
        digest = " ".join(transcript_text.split())[:_FALLBACK_DIGEST_LIMIT]
        return [
            PerspectiveMemory(
                actor_id=brief.actor_id,
                summary=f"我在{scene_name}亲历了一段对话：{digest}",
                importance=_FALLBACK_IMPORTANCE,
                emotional_valence=0.0,
                topic_name=scene_name,
            )
            for brief in participants
        ]

    def _supports_structured_output(self) -> bool:
        supports = getattr(self._model_client, "supports", None)
        if callable(supports):
            try:
                return bool(supports(ProviderCapability.STRUCTURED_OUTPUT))
            except Exception as error:
                logger.warning(f"[WorldMemoryProjector] 结构化输出能力判断失败: {error}")
        return False
