"""配置管理兼容入口。

新代码优先从 config_schema、config_loader、config_merge、config_env 导入；
旧代码仍可继续从本模块导入 AppConfig、ConfigLoader 等名称。
"""

from .character_package import CharacterPackageOptions, CharacterPackageService
from .character_validator import CharacterValidator
from .config_env import adapter_config_dir, apply_env_overrides
from .config_loader import ConfigLoader
from .config_merge import ConfigMerger
from .config_schema import (
    AppConfig,
    AuthConfig,
    BeginScene,
    CharacterConfig,
    EmbeddingConfig,
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
    ToolConfig,
    TopicGenerationConfig,
    WebSearchAPIConfig,
    WebSearchToolConfig,
    WorldActorConfig,
    WorldConfig,
    WorldDirectorConfig,
    WorldPersistenceConfig,
    WorldTranscriptConfig,
)
from .config_validator import ConfigDiagnostic, ConfigValidationError, ConfigValidator

__all__ = [
    "AppConfig",
    "AuthConfig",
    "BeginScene",
    "CharacterConfig",
    "CharacterPackageOptions",
    "CharacterPackageService",
    "CharacterValidator",
    "ConfigLoader",
    "ConfigDiagnostic",
    "ConfigMerger",
    "ConfigValidationError",
    "ConfigValidator",
    "EmbeddingConfig",
    "InitiativeTimerConfig",
    "LogLevel",
    "MemoryConfig",
    "ModelConfig",
    "OocJudgeConfig",
    "RepeatGuardConfig",
    "ResourceControlConfig",
    "SceneConfig",
    "SessionConfig",
    "ThinkEngineConfig",
    "ToolConfig",
    "TopicGenerationConfig",
    "WebSearchAPIConfig",
    "WebSearchToolConfig",
    "WorldActorConfig",
    "WorldConfig",
    "WorldDirectorConfig",
    "WorldPersistenceConfig",
    "WorldTranscriptConfig",
    "adapter_config_dir",
    "apply_env_overrides",
]
