"""一次性脱稿生成（OneShotGenerator）：不建租户/Agent，不进任何会话与记忆。

群友第一印象、冷却破例判定等「以角色口吻写一段」的需求，只需要一次
system + user 的模型调用——此前为此专门开 `nb2-meta` 元租户，白背一套
ThinkEngine / 后台管理器 / 语义记忆 / 持久化（2026-08-02 用户拍板删除：
「隔离性直接在里面调用一次 LLM 生成就好了，不会进 session 记忆」）。

每角色缓存一个 ModelClient 与角色系统提示词（Provider 构建有成本）；
无会话、无工作记忆、无思考引擎——调用即走，成本只在当次。
"""

from pathlib import Path
from typing import Any

from ...utils.logger import logger
from ..config import ConfigLoader
from ..config_schema import CharacterConfig
from .model_client import ModelClient
from .prompts import build_roleplay_system_prompt


class OneShotGenerator:
    """按角色缓存的一次性脱稿生成器（system 角色提示词 + user 任务提示词）。"""

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir
        self._cache: dict[str, tuple[ModelClient, str]] = {}  # character -> (client, system_prompt)

    async def generate(self, character: str, prompt: str) -> str:
        """以角色口吻对 prompt 做一次性生成；失败抛给调用方（不静默兜底）。"""
        client, system_prompt = self._resolve(character)
        response = await client.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            call_context="one_shot",
        )
        content = response.message.content
        return (content if isinstance(content, str) else str(content)).strip()

    async def get_quota(self, character: str) -> dict[str, Any] | None:
        """查询 Provider 账户额度（账户级；Provider 不支持返回 None）。"""
        client, _ = self._resolve(character)
        return await client.get_quota()

    # ==================== 装配（懒加载 + 缓存） ====================

    def _resolve(self, character: str) -> tuple[ModelClient, str]:
        cached = self._cache.get(character)
        if cached is not None:
            return cached
        loader = ConfigLoader()
        app_config = loader.load(self._config_path())
        character_config = self._load_character(loader, character)
        client = ModelClient(app_config.model, embedding_config=app_config.embedding)
        system_prompt = build_roleplay_system_prompt(
            character_config.name, character_config.system_prompt
        )
        logger.info(f"一次性脱稿生成器已装配（角色: {character_config.name}）")
        entry = (client, system_prompt)
        self._cache[character] = entry
        return entry

    def _config_path(self) -> Path:
        """本地配置优先，发行模板兜底（与 runtime service 同一约定）。"""
        local = self._root_dir / "config" / "local.yaml"
        if local.exists():
            return local
        return self._root_dir / "tmp" / "template-conf.yaml"

    def _load_character(self, loader: ConfigLoader, character: str) -> CharacterConfig:
        base = self._root_dir / "characters"
        candidates = [
            base / f"{character}.yaml",
            base / f"{character}.yml",
            base / "zh_cn" / f"{character}.yaml",
            base / "zh_cn" / f"{character}.yml",
            self._root_dir / character,
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return loader.load_character(candidate)
        raise FileNotFoundError(f"角色文件不存在: {character}（已搜索 {base} 及其 zh_cn）")
