"""配置加载器与角色配置加载。"""

from pathlib import Path
from typing import Any

import yaml

from ..utils.logger import logger
from .character_validator import CharacterValidator
from .config_env import apply_env_overrides
from .config_merge import ConfigMerger
from .config_schema import (
    AppConfig,
    CharacterConfig,
    EmbeddingConfig,
    HealthConfig,
    InitiativeTimerConfig,
    LogLevel,
    MemoryConfig,
    ModelConfig,
    OocJudgeConfig,
    RepeatGuardConfig,
    ResourceControlConfig,
    SceneConfig,
    SessionConfig,
    ThinkEngineConfig,
    TopicGenerationConfig,
    WorldActorConfig,
    WorldConfig,
    WorldDirectorConfig,
    WorldPersistenceConfig,
    WorldTranscriptConfig,
)
from .config_validator import (
    _REMOVED_INITIATIVE_FALLBACK_KEYS,
    _REMOVED_MEMORY_EPISODIC_KEYS,
    ConfigDiagnostic,
    ConfigValidator,
)

# 角色配置缓存（LRU 简化版）：path -> (mtime, config)
_character_config_cache: dict[Path, tuple[float, CharacterConfig]] = {}
_CONFIG_CACHE_MAX_SIZE = 32

# world 节中需要展开为子 Struct 的嵌套键，不直接作为 WorldConfig 的标量 kwargs。
_WORLD_NESTED_KEYS = frozenset({"actors", "director", "transcript", "persistence"})


class ConfigLoader(ConfigMerger):
    """配置加载器"""

    def __init__(self):
        self._config: AppConfig | None = None
        self._validator = ConfigValidator()
        self._character_validator = CharacterValidator()
        super().__init__()

    @staticmethod
    def default_config_path() -> Path:
        """返回配置模板路径（发行文件；用户配置应为 config/local.yaml）。"""
        return Path(__file__).parent.parent.parent / "tmp" / "template-conf.yaml"

    def load(self, config_file: Path | None = None) -> AppConfig:
        """加载配置"""
        config = AppConfig()

        # 1. 加载默认配置
        default_file = self.default_config_path()
        if default_file.exists():
            config = self._load_yaml(default_file)

        # 2. 加载用户配置文件
        if config_file:
            if not config_file.exists():
                # 拼错路径不再静默以模板默认启动（难排查）
                logger.warning(f"指定的配置文件不存在，将使用默认配置: {config_file}")
            else:
                user_config = self._load_yaml(config_file)
                config = self.merge(config, user_config)

        # 3. 环境变量覆盖
        config = apply_env_overrides(config)

        # 4. 重新应用日志配置（确保使用最终配置）
        config._apply_logging_config()

        self._config = config
        return config

    def _load_yaml(self, path: Path) -> AppConfig:
        """从 YAML 加载配置"""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return self._dict_to_config(data)

    def validate_dict(self, data: dict[str, Any]) -> list[ConfigDiagnostic]:
        """返回配置字典的结构化诊断列表。"""
        return self._validator.validate_config_dict(self._normalize_config_aliases(data))

    def validate_character_dict(self, data: Any) -> list[ConfigDiagnostic]:
        """返回角色字典的结构化诊断列表。"""
        return self._character_validator.validate_character_dict(data)

    def validate_character_file(self, path: Path) -> list[ConfigDiagnostic]:
        """返回角色 YAML 文件的结构化诊断列表。"""
        return self._character_validator.validate_character_file(path)

    def _dict_to_config(self, data: dict[str, Any]) -> AppConfig:
        """字典转配置对象，并记录用户显式提供的字段。"""
        # 每次构建配置从零开始记录 provided 字段：避免跨 load 用 id() 键累积泄漏、
        # 以及旧配置被 GC 后 id 复用串扰（07#4）。merge 只读 override（最近一次
        # 构建）的 provided 集，base（默认配置）的条目本就不被读。
        self._provided_fields.clear()
        data = self._normalize_config_aliases(data)
        diagnostics = self._validator.validate_config_dict(data)
        self._validator.raise_for_errors(diagnostics)

        config = AppConfig()
        self._provided_fields[id(config)] = set(data.keys())

        if "log_level" in data:
            config.log_level = LogLevel(data["log_level"])

        if "log_console" in data:
            config.log_console = data["log_console"]
        if "log_file" in data and data["log_file"]:
            config.log_file = Path(data["log_file"])
        if "debug_silent_output" in data:
            config.debug_silent_output = bool(data["debug_silent_output"])
        if "event_trace_enabled" in data:
            config.event_trace_enabled = bool(data["event_trace_enabled"])

        if "model" in data:
            model_data = data["model"] or {}
            config.model = ModelConfig(**model_data)
            self._provided_fields[id(config.model)] = set(model_data.keys())
        if "embedding" in data:
            embedding_data = data["embedding"] or {}
            config.embedding = EmbeddingConfig(**embedding_data)
            self._provided_fields[id(config.embedding)] = set(embedding_data.keys())
        if "memory" in data:
            memory_data = data["memory"] or {}
            topic_generation_data = memory_data.get("topic_generation")
            memory_obj_data = dict(memory_data)
            memory_obj_data.pop("topic_generation", None)
            # 已删除的配置键：读取时丢弃（校验层另有迁移警告，清单单源在
            # config_validator._REMOVED_MEMORY_EPISODIC_KEYS），
            # 避免旧配置在 Struct 构造时报未知参数（与 initiative_timer 同一招）
            for removed_key in _REMOVED_MEMORY_EPISODIC_KEYS:
                memory_obj_data.pop(removed_key, None)
            config.memory = MemoryConfig(**memory_obj_data)
            if isinstance(topic_generation_data, dict):
                config.memory.topic_generation = TopicGenerationConfig(**topic_generation_data)
            # 用清洗后的 memory_obj_data（已 pop 掉 removed episodic 键）记录 provided，
            # 避免残留键进 provided 集、未来 merge 对已删字段 getattr 报错（07#15）
            self._provided_fields[id(config.memory)] = set(memory_obj_data.keys())
            if isinstance(topic_generation_data, dict):
                self._provided_fields[id(config.memory.topic_generation)] = set(
                    topic_generation_data.keys()
                )
        if "tool" in data:
            tool_data = data["tool"] or {}
            config.tool = self._dict_to_tool_config(tool_data)
            self._provided_fields[id(config.tool)] = set(tool_data.keys())
            if isinstance(tool_data.get("web_search"), dict):
                self._provided_fields[id(config.tool.web_search)] = set(
                    tool_data["web_search"].keys()
                )
                if isinstance(tool_data["web_search"].get("api"), dict):
                    self._provided_fields[id(config.tool.web_search.api)] = set(
                        tool_data["web_search"]["api"].keys()
                    )
        if "scene" in data:
            scene_data = data["scene"] or {}
            config.scene = SceneConfig(**scene_data)
            self._provided_fields[id(config.scene)] = set(scene_data.keys())
        if "session" in data:
            session_data = data["session"] or {}
            config.session = SessionConfig(**session_data)
            self._provided_fields[id(config.session)] = set(session_data.keys())

        if "think_engine" in data:
            think_engine_data = data["think_engine"] or {}
            config.think_engine = ThinkEngineConfig(**think_engine_data)
            self._provided_fields[id(config.think_engine)] = set(think_engine_data.keys())

        if "initiative_timer" in data:
            initiative_timer_data = dict(data["initiative_timer"] or {})
            # 已删除的配置键：读取时丢弃（校验层另有迁移警告，清单单源在
            # config_validator._REMOVED_INITIATIVE_FALLBACK_KEYS），
            # 避免旧配置在 Struct 构造时报未知参数
            for removed_key in _REMOVED_INITIATIVE_FALLBACK_KEYS:
                initiative_timer_data.pop(removed_key, None)
            config.initiative_timer = InitiativeTimerConfig(**initiative_timer_data)
            self._provided_fields[id(config.initiative_timer)] = set(initiative_timer_data.keys())

        if "repeat_guard" in data:
            repeat_guard_data = data["repeat_guard"] or {}
            config.repeat_guard = RepeatGuardConfig(**repeat_guard_data)
            self._provided_fields[id(config.repeat_guard)] = set(repeat_guard_data.keys())

        if "health" in data:
            health_data = data["health"] or {}
            config.health = HealthConfig(**health_data)
            self._provided_fields[id(config.health)] = set(health_data.keys())

        if "ooc_judge" in data:
            ooc_data = data["ooc_judge"] or {}
            config.ooc_judge = OocJudgeConfig(**ooc_data)
            self._provided_fields[id(config.ooc_judge)] = set(ooc_data.keys())

        if "resource_control" in data:
            resource_control_data = data["resource_control"] or {}
            config.resource_control = ResourceControlConfig(**resource_control_data)
            self._provided_fields[id(config.resource_control)] = set(resource_control_data.keys())

        if "world" in data:
            world_data = data["world"] or {}
            config.world = self._dict_to_world_config(world_data)
            self._provided_fields[id(config.world)] = set(world_data.keys())

        return config

    def _dict_to_world_config(self, data: dict[str, Any]) -> WorldConfig:
        """构造 WorldConfig，逐层展开 actors 列表与 director/transcript/persistence 子节。"""
        world_kwargs = {k: v for k, v in data.items() if k not in _WORLD_NESTED_KEYS}

        actors_data = data.get("actors")
        if isinstance(actors_data, list):
            world_kwargs["actors"] = [
                WorldActorConfig(**actor) if isinstance(actor, dict) else actor
                for actor in actors_data
            ]

        director_data = data.get("director")
        if isinstance(director_data, dict):
            world_kwargs["director"] = WorldDirectorConfig(**director_data)

        transcript_data = data.get("transcript")
        if isinstance(transcript_data, dict):
            world_kwargs["transcript"] = WorldTranscriptConfig(**transcript_data)

        persistence_data = data.get("persistence")
        if isinstance(persistence_data, dict):
            world_kwargs["persistence"] = WorldPersistenceConfig(**persistence_data)

        return WorldConfig(**world_kwargs)

    @staticmethod
    def _normalize_config_aliases(data: dict[str, Any]) -> dict[str, Any]:
        """规范化兼容配置字段别名。"""
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        initiative_timer_data = normalized.get("initiative_timer")
        if isinstance(initiative_timer_data, dict):
            normalized_timer_data = dict(initiative_timer_data)
            legacy_field = "allow_frontend_edit_message"
            current_field = "allow_frontend_edit_summary"
            if legacy_field in normalized_timer_data and current_field not in normalized_timer_data:
                normalized_timer_data[current_field] = normalized_timer_data[legacy_field]
            normalized_timer_data.pop(legacy_field, None)
            normalized["initiative_timer"] = normalized_timer_data

        return normalized

    def load_character(self, path: Path) -> CharacterConfig:
        """加载角色配置，带 LRU 缓存（基于文件修改时间）。"""
        global _character_config_cache

        # 检查缓存：文件未修改则直接返回缓存
        mtime = path.stat().st_mtime if path.exists() else 0
        if path in _character_config_cache:
            cached_mtime, cached_config = _character_config_cache[path]
            if cached_mtime == mtime:
                # 命中即刷新 LRU 位置（pop + 重插）：真「最近使用」淘汰，
                # 而非按插入顺序淘汰（07#12）
                _character_config_cache.pop(path)
                _character_config_cache[path] = (cached_mtime, cached_config)
                return cached_config

        # 加载并验证
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        diagnostics = self._character_validator.validate_character_dict(data)
        self._character_validator.raise_for_errors(diagnostics)
        config = self._character_validator.to_character_config(data)

        # 更新缓存（简单 LRU：超过大小则清空一半）
        if len(_character_config_cache) >= _CONFIG_CACHE_MAX_SIZE:
            # 简单策略：保留最近的 16 个
            items = list(_character_config_cache.items())
            _character_config_cache.clear()
            for k, v in items[_CONFIG_CACHE_MAX_SIZE // 2 :]:
                _character_config_cache[k] = v

        _character_config_cache[path] = (mtime, config)
        return config

    @staticmethod
    def clear_character_cache() -> None:
        """清空角色配置缓存。"""
        _character_config_cache.clear()

    @staticmethod
    def get_character_cache_info() -> dict[str, Any]:
        """获取缓存状态信息。"""
        return {
            "size": len(_character_config_cache),
            "max_size": _CONFIG_CACHE_MAX_SIZE,
            "cached_paths": [str(p) for p in _character_config_cache],
        }
