# GensokyoAI/scene/manager.py

"""场景管理器 - 加载全局场景库、管理当前场景、支持异步切换。

设计要点：
- 场景库全局共享，从 scenes/*.yaml（含子目录）加载，所有角色可用。
- 当前场景是会话级状态，持久化在 SessionContext.metadata["current_scene_id"]。
- 场景上下文只在"对话开始"注入一次（新会话首轮 / resume 后首轮），
  之后靠模型自身记忆，遗忘时由模型主动调用 get_current_scene。
- 所有对外方法均为异步，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..core.release_resources import resolve_resource_path
from ..utils.logger import logger
from .types import Scene

if TYPE_CHECKING:
    from ..core.config import SceneConfig


class SceneError(Exception):
    """场景操作异常。"""


class SceneManager:
    """场景管理器 - 全局场景库 + 当前场景状态。"""

    def __init__(self, config: SceneConfig) -> None:
        self.config = config
        self._enabled = config.enabled
        self._library: dict[str, Scene] = {}
        self._current_scene_id: str | None = None
        self._loaded = False
        self._lock = asyncio.Lock()
        # 首轮注入标志：新会话/resume 后置 False，注入一次后置 True
        self._context_injected = False
        # 角色 begin_scene 指定的初始场景（优先级低于会话已存场景，高于配置默认场景）
        self._character_begin_scene_id: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current_scene_id(self) -> str | None:
        return self._current_scene_id

    # ==================== 场景库加载 ====================

    async def load_library(self) -> dict[str, Scene]:
        """异步加载全局场景库（幂等）。"""
        async with self._lock:
            if self._loaded:
                return self._library
            if not self._enabled:
                self._loaded = True
                return self._library

            library_path = Path(self.config.library_path)
            if library_path == Path("scenes"):
                library_path = resolve_resource_path(None, "scenes")
            if not library_path.exists():
                logger.warning(f"场景库目录不存在: {library_path}")
                self._loaded = True
                return self._library

            # 文件 IO 放到线程，避免阻塞事件循环
            scenes = await asyncio.to_thread(self._load_library_sync, library_path)
            self._library = scenes
            self._loaded = True
            logger.info(f"场景库已加载: {len(scenes)} 个场景，来源 {library_path}")
            return self._library

    @staticmethod
    def _load_library_sync(library_path: Path) -> dict[str, Scene]:
        """同步扫描目录加载所有场景 YAML（在线程中调用）。"""
        scenes: dict[str, Scene] = {}
        for yaml_file in sorted(library_path.rglob("*.y*ml")):
            if yaml_file.name.startswith("_"):
                continue
            try:
                with open(yaml_file, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if not isinstance(data, dict):
                    logger.warning(f"跳过非法场景文件（顶层非映射）: {yaml_file}")
                    continue
                scene_id = str(data.get("id") or yaml_file.stem)
                if scene_id in scenes:
                    logger.warning(f"场景 id 重复，后者覆盖前者: {scene_id} ({yaml_file})")
                scenes[scene_id] = Scene.from_dict(scene_id, data)
            except Exception as e:
                logger.warning(f"加载场景文件失败 {yaml_file}: {e}")
        return scenes

    # ==================== 查询 ====================

    async def get_scene(self, scene_id: str) -> Scene | None:
        """按 id 获取场景定义。"""
        await self.load_library()
        return self._library.get(scene_id)

    async def get_current_scene(self) -> Scene | None:
        """获取当前场景；未设置或场景库禁用时返回 None。"""
        if not self._enabled or not self._current_scene_id:
            return None
        await self.load_library()
        return self._library.get(self._current_scene_id)

    async def list_scenes(self) -> list[Scene]:
        """列出全部场景。"""
        await self.load_library()
        return list(self._library.values())

    # ==================== 切换 ====================

    async def switch_scene(self, scene_id: str) -> Scene:
        """切换当前场景，返回切换后的场景。

        Raises:
            SceneError: 场景库禁用、场景不存在，或（开启连通性校验时）目标场景
                        不在当前场景的 connected_scenes 中。
        """
        if not self._enabled:
            raise SceneError("场景功能未启用")

        await self.load_library()

        target = self._library.get(scene_id)
        if target is None:
            available = "、".join(sorted(self._library)) or "（空）"
            raise SceneError(f"场景不存在: {scene_id}；可用场景: {available}")

        if self.config.enforce_connectivity and self._current_scene_id:
            current = self._library.get(self._current_scene_id)
            if (
                current is not None
                and current.connected_scenes
                and scene_id not in current.connected_scenes
            ):
                allowed = "、".join(current.connected_scenes)
                raise SceneError(
                    f"无法从「{current.name}」直接前往「{target.name}」；可前往：{allowed}"
                )

        self._current_scene_id = scene_id
        logger.info(f"场景已切换: {target.name} ({scene_id})")
        return target

    # ==================== 会话状态同步 ====================

    def set_current_scene_id(self, scene_id: str | None) -> None:
        """直接设置当前场景 id（用于从会话 metadata 恢复，不做校验）。"""
        self._current_scene_id = scene_id

    def reset_for_session(self, scene_id: str | None) -> None:
        """会话创建/恢复时重置当前场景与首轮注入标志。"""
        self._current_scene_id = scene_id
        self._context_injected = False

    def set_character_begin_scene(self, scene_id: str | None) -> None:
        """设置角色 begin_scene 指定的初始场景 id。"""
        self._character_begin_scene_id = scene_id

    async def resolve_initial_scene(self, session_scene_id: str | None) -> str | None:
        """确定会话开始时的当前场景 id。

        优先级：会话已保存的场景 > 角色 begin_scene 指定的场景 > 配置默认场景 > None。
        """
        if not self._enabled:
            return None
        await self.load_library()

        if session_scene_id and session_scene_id in self._library:
            return session_scene_id

        character_id = self._character_begin_scene_id
        if character_id and character_id in self._library:
            return character_id

        default_id = self.config.default_scene
        if default_id and default_id in self._library:
            return default_id
        return None

    # ==================== 上下文注入 ====================

    async def render_available_scenes(self, current_scene: Scene | None = None) -> str:
        """渲染"可前往的场景"清单（带 id），让模型准确知道能切到哪些场景。

        - enforce_connectivity=False（默认，可自由飞行往来）：列出全部场景，可任意前往。
        - enforce_connectivity=True（仅限只能步行的角色）：只列出当前场景的相邻场景。
        """
        await self.load_library()
        if not self._library:
            return ""

        if self.config.enforce_connectivity and current_scene is not None:
            allowed_ids = current_scene.connected_scenes
            scenes = [self._library[sid] for sid in allowed_ids if sid in self._library]
            if not scenes:
                return "【可前往的场景】当前场景没有相邻场景，暂时无法前往别处。"
            listing = "、".join(f"{s.name}({s.id})" for s in scenes)
            return (
                "【可前往的场景】你只能步行前往与当前场景相邻的地点，"
                f"用 scene_switch 切换：{listing}。"
            )

        # 自由往来：列出全部场景
        listing = "、".join(f"{s.name}({s.id})" for s in self._library.values())
        return (
            "【可前往的场景】你可以自由前往幻想乡的任意地点，"
            f"想移动时用 scene_switch 切换到对应 id：{listing}。"
        )

    async def render_scene_with_options(self, scene: Scene) -> str:
        """组合"当前场景描述 + 可前往场景清单"，供首轮注入与 get_current_scene 复用。"""
        parts = [scene.render()]
        options = await self.render_available_scenes(scene)
        if options:
            parts.append(options)
        return "\n".join(parts)

    async def build_injection_context(self) -> str | None:
        """若本会话尚未注入过场景上下文，返回当前场景描述+可前往清单并置位；否则返回 None。"""
        if not self._enabled or self._context_injected:
            return None
        scene = await self.get_current_scene()
        if scene is None:
            return None
        self._context_injected = True
        return await self.render_scene_with_options(scene)

    def mark_context_injected(self) -> None:
        """显式标记本会话已注入过场景上下文。"""
        self._context_injected = True
