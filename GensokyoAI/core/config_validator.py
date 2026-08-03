"""配置诊断与校验工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from msgspec import Struct

from ..utils.url_security import UnsafeUrlError, validate_external_url
from .agent.providers.specs import PROVIDER_SPECS, ProviderSpec
from .config_schema import (
    EmbeddingConfig,
    HealthConfig,
    InitiativeTimerConfig,
    LogLevel,
    MemoryConfig,
    ModelConfig,
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
from .schema_versions import CONFIG_SCHEMA_VERSION

DiagnosticSeverity = Literal["error", "warning"]

# 已删除的 initiative_timer 配置键：
# 旧配置仍被接受（不报未知字段错误），由 DEPRECATED_FIELDS 给出迁移警告。
_REMOVED_INITIATIVE_FALLBACK_KEYS = {
    # 强制 fallback 链（阶段 7 删除）
    "fallback_on_no_schedule",
    "fallback_delay_seconds",
    "fallback_summary",
    "fallback_reason",
    # hesitation 犹豫链与对话欲累积器（2026-07-30 用户定稿删除）
    "hesitation_enabled",
    "hesitation_max_rounds",
    "hesitation_delay_seconds",
    "drive_enabled",
    "drive_turn_increment",
    "drive_motivation_boost",
    "drive_emotion_boost",
    "drive_scene_boost",
    "drive_silence_rate_per_minute",
    "drive_vent_factor",
    "mood_half_life_positive_minutes",
    "mood_half_life_negative_minutes",
}

# 已删除的 memory 配置键（情景记忆子系统 2026-08-01 删除）：
# 旧配置仍被接受（不报未知字段错误），由 DEPRECATED_FIELDS 给出迁移警告。
_REMOVED_MEMORY_EPISODIC_KEYS = {
    "episodic_threshold",
    "episodic_summary_model",
    "episodic_keep_recent",
}


class ConfigDiagnostic(Struct, frozen=True):
    """单条配置诊断。"""

    code: str
    path: str
    severity: DiagnosticSeverity
    message: str
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "path": self.path,
            "severity": self.severity,
            "message": self.message,
        }
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        return payload


class ConfigValidationError(ValueError):
    """配置校验异常，携带结构化诊断列表。"""

    def __init__(self, diagnostics: list[ConfigDiagnostic]) -> None:
        self.diagnostics = diagnostics
        message = self._format_message(diagnostics)
        super().__init__(message)

    @staticmethod
    def _format_message(diagnostics: list[ConfigDiagnostic]) -> str:
        if not diagnostics:
            return "Config validation failed"
        first = diagnostics[0]
        suffix = f" Suggestion: {first.suggestion}" if first.suggestion else ""
        more = f" ({len(diagnostics)} diagnostics total)" if len(diagnostics) > 1 else ""
        return f"{first.path}: {first.message}.{suffix}{more}"

    def to_dict(self) -> dict[str, Any]:
        return {"diagnostics": [item.to_dict() for item in self.diagnostics]}


class ConfigValidator:
    """配置字典和 Runtime 覆盖参数校验器。"""

    MODEL_OVERRIDE_FIELDS = {
        "provider",
        "name",
        "base_url",
        "api_path",
        "api_key",
        "extra_headers",
        "model_capabilities_add",
        "model_capabilities_remove",
        "web_search_enabled",
        "web_search_strategy",
        "web_search_context_size",
        "web_search_user_location",
        "web_search_allow_fallback",
        "web_search_metadata",
        "stream",
        "think",
        "thinking_enabled",
        "reasoning_effort",
        "temperature",
        "top_p",
        "max_tokens",
        "timeout",
        "use_proxy",
        "retry_max_attempts",
        "retry_initial_delay",
        "retry_backoff_factor",
        "retry_status_codes",
    }
    EMBEDDING_OVERRIDE_FIELDS = {
        "provider",
        "name",
        "base_url",
        "api_key",
        "dimensions",
        "encoding_format",
        "timeout",
        "use_proxy",
    }
    DEPRECATED_FIELDS: dict[str, tuple[str, str]] = {
        "initiative_timer.fallback_on_no_schedule": (
            "initiative_timer.drive_threshold",
            "强制兜底链已删除：AI 决定不发言时不再强行安排。",
        ),
        "initiative_timer.fallback_delay_seconds": (
            "initiative_timer.drive_threshold",
            "强制兜底链已删除：AI 决定不发言时不再强行安排。",
        ),
        "initiative_timer.fallback_summary": (
            "initiative_timer.drive_threshold",
            "强制兜底链已删除：AI 决定不发言时不再强行安排。",
        ),
        "initiative_timer.fallback_reason": (
            "initiative_timer.drive_threshold",
            "强制兜底链已删除：AI 决定不发言时不再强行安排。",
        ),
        "initiative_timer.hesitation_enabled": (
            "initiative_timer.drive_threshold",
            "犹豫链已删除：对话欲改为四维打分 + 阈值二元判断，AI 决定不说就是不说。",
        ),
        "initiative_timer.hesitation_max_rounds": (
            "initiative_timer.drive_threshold",
            "犹豫链已删除：对话欲改为四维打分 + 阈值二元判断。",
        ),
        "initiative_timer.hesitation_delay_seconds": (
            "initiative_timer.drive_threshold",
            "犹豫链已删除：对话欲改为四维打分 + 阈值二元判断。",
        ),
        "initiative_timer.drive_enabled": (
            "initiative_timer.drive_threshold",
            "对话欲累积器已删除：改为四维心情打分 + drive_threshold 阈值判断（唯一路径）。",
        ),
        "initiative_timer.drive_turn_increment": (
            "initiative_timer.drive_threshold",
            "对话欲累积器已删除：改为四维心情打分 + drive_threshold 阈值判断。",
        ),
        "initiative_timer.drive_motivation_boost": (
            "initiative_timer.drive_threshold",
            "对话欲累积器已删除：改为四维心情打分 + drive_threshold 阈值判断。",
        ),
        "initiative_timer.drive_emotion_boost": (
            "initiative_timer.drive_threshold",
            "对话欲累积器已删除：改为四维心情打分 + drive_threshold 阈值判断。",
        ),
        "initiative_timer.drive_scene_boost": (
            "initiative_timer.drive_threshold",
            "对话欲累积器已删除：改为四维心情打分 + drive_threshold 阈值判断。",
        ),
        "initiative_timer.drive_silence_rate_per_minute": (
            "initiative_timer.drive_threshold",
            "对话欲累积器已删除：改为四维心情打分 + drive_threshold 阈值判断。",
        ),
        "initiative_timer.drive_vent_factor": (
            "initiative_timer.drive_threshold",
            "对话欲累积器已删除：改为四维心情打分 + drive_threshold 阈值判断。",
        ),
        "initiative_timer.mood_half_life_positive_minutes": (
            "initiative_timer.drive_threshold",
            "对话欲累积器已删除：改为四维心情打分 + drive_threshold 阈值判断。",
        ),
        "initiative_timer.mood_half_life_negative_minutes": (
            "initiative_timer.drive_threshold",
            "对话欲累积器已删除：改为四维心情打分 + drive_threshold 阈值判断。",
        ),
        "memory.episodic_threshold": (
            "memory.distill_turns",
            "情景记忆子系统已删除：历史摘要职责由定期记忆蒸馏接替（§8.29）。",
        ),
        "memory.episodic_summary_model": (
            "memory.distill_turns",
            "情景记忆子系统已删除：历史摘要职责由定期记忆蒸馏接替（§8.29）。",
        ),
        "memory.episodic_keep_recent": (
            "memory.distill_turns",
            "情景记忆子系统已删除：历史摘要职责由定期记忆蒸馏接替（§8.29）。",
        ),
    }

    def validate_config_dict(self, data: dict[str, Any]) -> list[ConfigDiagnostic]:
        diagnostics: list[ConfigDiagnostic] = []
        self._validate_top_level(data, diagnostics)
        if not isinstance(data, dict):
            return diagnostics

        if "config_schema_version" in data:
            self._validate_config_schema_version(data["config_schema_version"], diagnostics)
        if "log_level" in data:
            self._validate_enum(
                "log_level", data["log_level"], {item.value for item in LogLevel}, diagnostics
            )
        if "model" in data:
            self._validate_model_data(data.get("model") or {}, diagnostics)
        if "embedding" in data:
            self._validate_embedding_data(data.get("embedding") or {}, diagnostics)
        if "memory" in data:
            self._validate_memory_data(data.get("memory") or {}, diagnostics)
        if "tool" in data:
            self._validate_tool_data(data.get("tool") or {}, diagnostics)
        if "scene" in data:
            self._validate_scene_data(data.get("scene") or {}, diagnostics)
        if "session" in data:
            self._validate_session_data(data.get("session") or {}, diagnostics)
        if "think_engine" in data:
            self._validate_think_engine_data(data.get("think_engine") or {}, diagnostics)
        if "initiative_timer" in data:
            self._validate_initiative_timer_data(data.get("initiative_timer") or {}, diagnostics)
        if "repeat_guard" in data:
            self._validate_repeat_guard_data(data.get("repeat_guard") or {}, diagnostics)
        if "health" in data:
            self._validate_health_data(data.get("health") or {}, diagnostics)
        if "resource_control" in data:
            self._validate_resource_control_data(data.get("resource_control") or {}, diagnostics)
        if "world" in data:
            self._validate_world_data(data.get("world") or {}, diagnostics)
        self._validate_deprecated_fields(data, diagnostics)
        return diagnostics

    def validate_model_overrides(self, overrides: dict[str, Any]) -> list[ConfigDiagnostic]:
        # Runtime overrides historically ignored unknown keys. Keep that compatibility
        # while still validating every accepted override value before applying it.
        data = {
            key: value
            for key, value in overrides.items()
            if value != "" and key in self.MODEL_OVERRIDE_FIELDS
        }
        diagnostics: list[ConfigDiagnostic] = []
        self._validate_model_values("model", data, diagnostics)
        return diagnostics

    def validate_embedding_overrides(self, overrides: dict[str, Any]) -> list[ConfigDiagnostic]:
        # Runtime overrides historically ignored unknown keys. Keep that compatibility
        # while still validating every accepted override value before applying it.
        data = {
            key: value
            for key, value in overrides.items()
            if value != "" and key in self.EMBEDDING_OVERRIDE_FIELDS
        }
        diagnostics: list[ConfigDiagnostic] = []
        self._validate_embedding_values("embedding", data, diagnostics)
        return diagnostics

    @staticmethod
    def raise_for_errors(diagnostics: list[ConfigDiagnostic]) -> None:
        errors = [item for item in diagnostics if item.severity == "error"]
        if errors:
            raise ConfigValidationError(errors)

    def _validate_config_schema_version(
        self, value: Any, diagnostics: list[ConfigDiagnostic]
    ) -> None:
        path = "config_schema_version"
        if not isinstance(value, int) or isinstance(value, bool):
            diagnostics.append(
                self._error(
                    path,
                    "config_schema_version must be an integer",
                    f"请填写当前支持的配置 schema 版本 {CONFIG_SCHEMA_VERSION}，或删除该字段使用当前默认版本。",
                    code="config.schema_version.type",
                )
            )
            return
        if value > CONFIG_SCHEMA_VERSION:
            diagnostics.append(
                self._error(
                    path,
                    f"Config schema version {value} is newer than supported version {CONFIG_SCHEMA_VERSION}",
                    "请升级 GensokyoAI，或使用当前版本支持的配置文件。",
                    code="config.schema_version.unsupported",
                )
            )
        elif value < CONFIG_SCHEMA_VERSION:
            diagnostics.append(
                self._warning(
                    path,
                    f"Config schema version {value} is older than current version {CONFIG_SCHEMA_VERSION}",
                    "建议对照 docs/user_guide.md 检查新增配置；当前版本会按兼容规则读取。",
                    code="config.schema_version.outdated",
                )
            )

    def _validate_top_level(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        if not isinstance(data, dict):
            diagnostics.append(
                ConfigDiagnostic(
                    code="config.type.invalid",
                    path="$",
                    severity="error",
                    message="Config root must be an object",
                    suggestion="请确认 YAML 顶层是 key/value 对象。",
                )
            )
            return
        self._validate_unknown_fields("$", data, self._known_top_level_fields(), diagnostics)

    def _validate_model_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        self._validate_object("model", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "model", data, self._struct_field_names(ModelConfig), diagnostics
        )
        self._validate_model_values("model", data, diagnostics)

    @staticmethod
    def _error(
        path: str,
        message: str,
        suggestion: str | None = None,
        *,
        code: str | None = None,
    ) -> ConfigDiagnostic:
        return ConfigDiagnostic(
            code=code or "config.invalid",
            path=path,
            severity="error",
            message=message,
            suggestion=suggestion,
        )

    @staticmethod
    def _warning(
        path: str,
        message: str,
        suggestion: str | None = None,
        *,
        code: str | None = None,
    ) -> ConfigDiagnostic:
        return ConfigDiagnostic(
            code=code or "config.warning",
            path=path,
            severity="warning",
            message=message,
            suggestion=suggestion,
        )

    def _validate_url_field(
        self,
        path: str,
        value: Any,
        diagnostics: list[ConfigDiagnostic],
        *,
        allow_private: bool = False,
        code: str = "config.url.unsafe",
    ) -> None:
        """校验 URL 字段是否安全；None/空字符串跳过。"""

        if value is None or value == "":
            return
        if not isinstance(value, str):
            diagnostics.append(
                self._error(
                    path,
                    f"{path} must be a string",
                    suggestion="请填写以 http:// 或 https:// 开头的 URL。",
                    code="config.url.type",
                )
            )
            return
        try:
            validate_external_url(value, allow_private=allow_private)
        except UnsafeUrlError as exc:
            diagnostics.append(
                self._error(
                    path,
                    str(exc),
                    suggestion="请使用公网可访问的 http/https 地址；本地服务请通过配置显式放行。",
                    code=code,
                )
            )

    def _validate_model_values(
        self, section: str, data: dict[str, Any], diagnostics: list[ConfigDiagnostic]
    ) -> None:
        self._validate_numeric_range(
            f"{section}.temperature", data.get("temperature"), diagnostics, minimum=0, maximum=2
        )
        self._validate_numeric_range(
            f"{section}.top_p", data.get("top_p"), diagnostics, minimum=0, maximum=1
        )
        self._validate_numeric_range(
            f"{section}.max_tokens", data.get("max_tokens"), diagnostics, minimum=0
        )
        self._validate_numeric_range(
            f"{section}.timeout", data.get("timeout"), diagnostics, minimum=0.001
        )
        self._validate_numeric_range(
            f"{section}.retry_max_attempts", data.get("retry_max_attempts"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            f"{section}.retry_initial_delay",
            data.get("retry_initial_delay"),
            diagnostics,
            minimum=0,
        )
        self._validate_numeric_range(
            f"{section}.retry_backoff_factor",
            data.get("retry_backoff_factor"),
            diagnostics,
            minimum=1,
        )
        self._validate_int_list(
            f"{section}.retry_status_codes",
            data.get("retry_status_codes"),
            diagnostics,
            minimum=100,
            maximum=599,
        )
        self._validate_numeric_range(
            f"{section}.price_input_per_million",
            data.get("price_input_per_million"),
            diagnostics,
            minimum=0,
        )
        self._validate_numeric_range(
            f"{section}.price_output_per_million",
            data.get("price_output_per_million"),
            diagnostics,
            minimum=0,
        )
        self._validate_numeric_range(
            f"{section}.price_input_cached_per_million",
            data.get("price_input_cached_per_million"),
            diagnostics,
            minimum=0,
        )
        self._validate_numeric_range(
            f"{section}.price_input_cache_write_per_million",
            data.get("price_input_cache_write_per_million"),
            diagnostics,
            minimum=0,
        )
        self._validate_enum(
            f"{section}.web_search_strategy",
            data.get("web_search_strategy"),
            {"off", "explicit", "auto"},
            diagnostics,
        )
        self._validate_string_list(
            f"{section}.model_capabilities_add", data.get("model_capabilities_add"), diagnostics
        )
        self._validate_string_list(
            f"{section}.model_capabilities_remove",
            data.get("model_capabilities_remove"),
            diagnostics,
        )

        provider = data.get("provider")
        if provider is not None:
            if not isinstance(provider, str) or not provider.strip():
                diagnostics.append(
                    self._error(
                        f"{section}.provider",
                        "Provider must be a non-empty string",
                        "请填写模型服务名称，例如 ollama、openai 或 deepseek。",
                    )
                )
            elif provider not in PROVIDER_SPECS:
                diagnostics.append(
                    self._warning(
                        f"{section}.provider",
                        f"Unknown provider '{provider}'",
                        "如果这是自定义 Provider，请确认已注册；否则请检查 provider 拼写。",
                        code="config.provider.unknown",
                    )
                )
        # Provider 元数据（providers/specs.py）——字段约束、api_key 要求与专属规则的唯一事实源
        spec = PROVIDER_SPECS.get(provider) if isinstance(provider, str) else None

        # SSRF 防护：base_url 不能指向内网 / 元数据服务（本地服务如 ollama 由 spec 放行）
        base_url = data.get("base_url")
        self._validate_url_field(
            f"{section}.base_url",
            base_url,
            diagnostics,
            allow_private=bool(spec and spec.allow_private_base_url),
            code="config.model.base_url.unsafe",
        )

        if data.get("web_search_enabled") is True and data.get("web_search_strategy") == "off":
            diagnostics.append(
                self._error(
                    f"{section}.web_search_strategy",
                    "web_search_enabled is true but web_search_strategy is off",
                    "启用 Provider 内置联网搜索时，请将 web_search_strategy 设置为 explicit 或 auto。",
                    code="config.model.web_search_conflict",
                )
            )
        if data.get("api_path") is not None and not data.get("base_url"):
            diagnostics.append(
                self._warning(
                    f"{section}.api_path",
                    "api_path is usually meaningful only with base_url",
                    "如果使用自定义代理路径，请同时配置 base_url。",
                    code="config.model.api_path_without_base_url",
                )
            )
        if data.get("auth") is not None and data.get("api_key"):
            diagnostics.append(
                self._warning(
                    f"{section}.auth",
                    "Both api_key and auth are configured",
                    "请确认是否确实需要同时配置静态 API Key 与动态认证。",
                    code="config.model.auth_overlap",
                )
            )
        if (
            spec is not None
            and spec.requires_api_key
            and not data.get("api_key")
            and not data.get("auth")
        ):
            diagnostics.append(
                self._warning(
                    f"{section}.api_key",
                    f"Provider '{provider}' usually requires api_key or auth",
                    "请通过配置文件或环境变量提供 API Key；如果由网关注入认证，可忽略此警告。",
                    code="config.model.api_key_missing",
                )
            )
        if spec is not None and isinstance(provider, str):
            self._validate_provider_field_matrix(section, provider, data, spec, diagnostics)
            if spec.extra_rule is not None:
                for field_name, technical, suggestion, code, severity in spec.extra_rule(data):
                    factory = self._error if severity == "error" else self._warning
                    diagnostics.append(
                        factory(f"{section}.{field_name}", technical, suggestion, code=code)
                    )

    def _validate_provider_field_matrix(
        self,
        section: str,
        provider: str,
        data: dict[str, Any],
        spec: ProviderSpec,
        diagnostics: list[ConfigDiagnostic],
    ) -> None:
        configured_fields = {key for key, value in data.items() if value not in (None, "", [], {})}
        web_search_fields = {
            "web_search_enabled",
            "web_search_strategy",
            "web_search_context_size",
            "web_search_user_location",
            "web_search_metadata",
            "web_search_allow_fallback",
        }
        unsupported_fields = configured_fields & spec.unsupported
        if data.get("web_search_enabled") is not True:
            unsupported_fields -= web_search_fields
        for field_name in sorted(unsupported_fields):
            diagnostics.append(
                self._provider_unsupported_field_diagnostic(section, provider, field_name, spec)
            )
        for field_name in sorted(configured_fields & spec.discouraged):
            diagnostics.append(
                self._warning(
                    f"{section}.{field_name}",
                    f"Field '{field_name}' is not normally used by provider '{provider}'",
                    f"请确认 {provider} 是否真的需要 {field_name}；如果不是自定义网关场景，建议移除该字段。",
                    code="config.provider.field_discouraged",
                )
            )
        configured_web_search_fields = configured_fields & web_search_fields
        unsupported_web_search_fields = configured_web_search_fields - spec.supported_web_search
        if data.get("web_search_enabled") is True and unsupported_web_search_fields:
            severity = "error" if unsupported_web_search_fields & spec.unsupported else "warning"
            diagnostic_factory = self._error if severity == "error" else self._warning
            diagnostics.append(
                diagnostic_factory(
                    f"{section}.web_search_enabled",
                    f"Provider '{provider}' does not expose built-in web search with the configured fields",
                    "如果需要联网搜索，请改用 openai_responses / gemini 的内置搜索，或启用 tool.web_search 自有搜索工具。",
                    code="config.provider.web_search_unsupported",
                )
            )

    def _provider_unsupported_field_diagnostic(
        self, section: str, provider: str, field_name: str, spec: ProviderSpec
    ) -> ConfigDiagnostic:
        custom = spec.unsupported_messages.get(field_name)
        if custom:
            technical, suggestion, code = custom
            return self._error(f"{section}.{field_name}", technical, suggestion, code=code)
        return self._error(
            f"{section}.{field_name}",
            f"Field '{field_name}' is not supported by provider '{provider}'",
            f"{provider} Provider 不支持 {field_name}；请移除该字段，或改用支持该能力的 Provider。",
            code="config.provider.field_unsupported",
        )

    def _validate_embedding_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        self._validate_object("embedding", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "embedding", data, self._struct_field_names(EmbeddingConfig), diagnostics
        )
        self._validate_embedding_values("embedding", data, diagnostics)

    def _validate_embedding_values(
        self, section: str, data: dict[str, Any], diagnostics: list[ConfigDiagnostic]
    ) -> None:
        self._validate_numeric_range(
            f"{section}.timeout", data.get("timeout"), diagnostics, minimum=0.001
        )
        self._validate_numeric_range(
            f"{section}.dimensions", data.get("dimensions"), diagnostics, minimum=1
        )
        # SSRF 防护：embedding base_url 同样不能指向内网 / 元数据服务
        self._validate_url_field(
            f"{section}.base_url",
            data.get("base_url"),
            diagnostics,
            code="config.embedding.base_url.unsafe",
        )

    def _validate_memory_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        self._validate_object("memory", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "memory",
            data,
            self._struct_field_names(MemoryConfig)
            # 已删除的情景记忆配置键：不作为未知字段报错，
            # 由 DEPRECATED_FIELDS 给出迁移警告（见类常量）
            | _REMOVED_MEMORY_EPISODIC_KEYS,
            diagnostics,
        )
        self._validate_numeric_range(
            "memory.working_max_turns", data.get("working_max_turns"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "memory.semantic_top_k", data.get("semantic_top_k"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "memory.semantic_max_topics", data.get("semantic_max_topics"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "memory.semantic_similarity_threshold",
            data.get("semantic_similarity_threshold"),
            diagnostics,
            minimum=0,
            maximum=1,
        )
        self._validate_numeric_range(
            "memory.distill_turns", data.get("distill_turns"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "memory.topic_half_life_hours",
            data.get("topic_half_life_hours"),
            diagnostics,
            minimum=0.1,
        )
        self._validate_numeric_range(
            "memory.topic_decay_threshold",
            data.get("topic_decay_threshold"),
            diagnostics,
            minimum=0,
            maximum=1,
        )
        self._validate_numeric_range(
            "memory.topic_pin_importance",
            data.get("topic_pin_importance"),
            diagnostics,
            minimum=0,
        )
        topic_generation = data.get("topic_generation")
        if topic_generation is not None:
            self._validate_object("memory.topic_generation", topic_generation, diagnostics)
            if isinstance(topic_generation, dict):
                self._validate_unknown_fields(
                    "memory.topic_generation",
                    topic_generation,
                    self._struct_field_names(TopicGenerationConfig),
                    diagnostics,
                )
                self._validate_numeric_range(
                    "memory.topic_generation.name_max_length",
                    topic_generation.get("name_max_length"),
                    diagnostics,
                    minimum=1,
                )
                self._validate_numeric_range(
                    "memory.topic_generation.summary_max_length",
                    topic_generation.get("summary_max_length"),
                    diagnostics,
                    minimum=1,
                )

    def _validate_tool_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        self._validate_object("tool", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "tool",
            data,
            {"enabled", "builtin_tools", "custom_tools_path", "web_search"},
            diagnostics,
        )
        self._validate_string_list("tool.builtin_tools", data.get("builtin_tools"), diagnostics)
        web_search = data.get("web_search")
        if web_search is not None:
            self._validate_object("tool.web_search", web_search, diagnostics)
            if isinstance(web_search, dict):
                self._validate_unknown_fields(
                    "tool.web_search",
                    web_search,
                    {
                        "enabled",
                        "provider",
                        "max_results",
                        "timeout",
                        "cache_ttl_seconds",
                        "trigger_strategy",
                        "freshness_keywords",
                        "prefer_for_characters",
                        "prefer_for_scenarios",
                        "user_agent",
                        "region",
                        "safe_search",
                        "snippet_max_length",
                        "api",
                    },
                    diagnostics,
                )
                self._validate_enum(
                    "tool.web_search.provider",
                    web_search.get("provider"),
                    {"ddg", "bing", "api", "mixed"},
                    diagnostics,
                )
                self._validate_enum(
                    "tool.web_search.trigger_strategy",
                    web_search.get("trigger_strategy"),
                    {"off", "explicit", "auto"},
                    diagnostics,
                )
                self._validate_numeric_range(
                    "tool.web_search.max_results",
                    web_search.get("max_results"),
                    diagnostics,
                    minimum=1,
                )
                self._validate_numeric_range(
                    "tool.web_search.timeout", web_search.get("timeout"), diagnostics, minimum=0.001
                )
                self._validate_numeric_range(
                    "tool.web_search.cache_ttl_seconds",
                    web_search.get("cache_ttl_seconds"),
                    diagnostics,
                    minimum=0,
                )
                self._validate_numeric_range(
                    "tool.web_search.snippet_max_length",
                    web_search.get("snippet_max_length"),
                    diagnostics,
                    minimum=1,
                )
                self._validate_web_search_api(web_search.get("api"), diagnostics)

    def _validate_web_search_api(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        if data is None:
            return
        self._validate_object("tool.web_search.api", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "tool.web_search.api",
            data,
            {
                "endpoint",
                "method",
                "api_key",
                "api_key_header",
                "api_key_prefix",
                "headers",
                "request_template",
                "query_params",
                "results_path",
                "title_path",
                "url_path",
                "snippet_path",
                "published_at_path",
            },
            diagnostics,
        )
        self._validate_enum(
            "tool.web_search.api.method", data.get("method"), {"GET", "POST"}, diagnostics
        )
        # SSRF 防护：通用搜索 API endpoint 不能指向内网 / 元数据服务
        self._validate_url_field(
            "tool.web_search.api.endpoint",
            data.get("endpoint"),
            diagnostics,
            code="config.tool.web_search.api.endpoint.unsafe",
        )

    def _validate_scene_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        self._validate_object("scene", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "scene", data, self._struct_field_names(SceneConfig), diagnostics
        )
        default_scene = data.get("default_scene")
        if default_scene is not None and not isinstance(default_scene, str):
            diagnostics.append(
                self._error(
                    "scene.default_scene",
                    "scene.default_scene 必须是字符串场景 id 或留空。",
                    code="config.scene.default_scene_type",
                )
            )

    def _validate_world_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        """校验 world 节结构。不读取角色文件/场景库——那些在初始化阶段做。"""
        self._validate_object("world", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "world", data, self._struct_field_names(WorldConfig), diagnostics
        )

        # 顶层标量字段类型（msgspec 构造期不做类型兜底，垃圾类型会穿透到运行时）
        self._validate_field_type("world.id", data.get("id"), str, diagnostics)
        self._validate_field_type("world.enabled", data.get("enabled"), bool, diagnostics)
        self._validate_field_type(
            "world.user_initial_scene", data.get("user_initial_scene"), str, diagnostics
        )
        self._validate_field_type(
            "world.project_perspective_memories",
            data.get("project_perspective_memories"),
            bool,
            diagnostics,
        )
        self._validate_field_type(
            "world.user_follows_current_actor",
            data.get("user_follows_current_actor"),
            bool,
            diagnostics,
        )

        enabled = bool(data.get("enabled", False))
        actors = data.get("actors")
        seen_ids: set[str] = set()
        enabled_ids: set[str] = set()
        enabled_count = 0

        if actors is not None and not isinstance(actors, list):
            diagnostics.append(
                self._error(
                    "world.actors",
                    "world.actors 必须是角色配置列表。",
                    code="config.world.actors_type",
                )
            )
        elif isinstance(actors, list):
            for index, actor in enumerate(actors):
                enabled_count += self._validate_world_actor(
                    index, actor, seen_ids, enabled_ids, diagnostics
                )

        # 仅在 world 启用时强校验 roster 完整性，避免关闭态误报
        if enabled and enabled_count == 0:
            diagnostics.append(
                self._error(
                    "world.actors",
                    "world.enabled 为 true 时至少需要一个 enabled 的 actor。",
                    code="config.world.no_enabled_actor",
                )
            )

        protagonist = data.get("protagonist")
        if isinstance(protagonist, str) and protagonist != "__user__":
            # protagonist 必须指向 enabled 的 actor：disabled actor 不会被装配，
            # 指向它会在初始化阶段解析不到角色
            if protagonist not in seen_ids:
                diagnostics.append(
                    self._error(
                        "world.protagonist",
                        "world.protagonist 必须是 '__user__' 或 actors 中某个 id。",
                        code="config.world.protagonist_unknown",
                    )
                )
            elif protagonist not in enabled_ids:
                diagnostics.append(
                    self._error(
                        "world.protagonist",
                        f"world.protagonist 指向的 actor '{protagonist}' 是 disabled 状态。",
                        code="config.world.protagonist_disabled",
                    )
                )
        elif protagonist is not None and not isinstance(protagonist, str):
            diagnostics.append(
                self._error(
                    "world.protagonist",
                    "world.protagonist 必须是字符串。",
                    code="config.world.protagonist_type",
                )
            )

        self._validate_world_director(data.get("director"), diagnostics)
        self._validate_world_transcript(data.get("transcript"), diagnostics)
        persistence = data.get("persistence")
        if persistence is not None and not isinstance(persistence, dict):
            diagnostics.append(
                self._error(
                    "world.persistence",
                    "world.persistence 必须是对象。",
                    code="config.world.persistence_type",
                )
            )
        elif isinstance(persistence, dict):
            self._validate_unknown_fields(
                "world.persistence",
                persistence,
                self._struct_field_names(WorldPersistenceConfig),
                diagnostics,
            )

        # 交叉校验：注入条数超过分片上限时永远达不到，静默缩水
        transcript = data.get("transcript")
        if isinstance(transcript, dict):
            context_entries = transcript.get("context_entries")
            max_entries = transcript.get("max_entries_per_scene")
            if (
                isinstance(context_entries, int)
                and isinstance(max_entries, int)
                and context_entries > max_entries
            ):
                diagnostics.append(
                    self._warning(
                        "world.transcript.context_entries",
                        "context_entries 超过 max_entries_per_scene，"
                        "实际注入条数将被分片上限截断。",
                        code="config.world.transcript_entries_exceed_max",
                    )
                )

    def _validate_world_actor(
        self,
        index: int,
        actor: Any,
        seen_ids: set[str],
        enabled_ids: set[str],
        diagnostics: list[ConfigDiagnostic],
    ) -> int:
        """校验单个 actor，返回其计入 enabled 的数量（0/1）。"""
        path = f"world.actors[{index}]"
        if not isinstance(actor, dict):
            diagnostics.append(
                self._error(path, f"{path} 必须是对象。", code="config.world.actor_type")
            )
            return 0
        self._validate_unknown_fields(
            path, actor, self._struct_field_names(WorldActorConfig), diagnostics
        )

        actor_id = actor.get("id")
        if not isinstance(actor_id, str) or not actor_id.strip():
            diagnostics.append(
                self._error(
                    f"{path}.id", f"{path}.id 必须是非空字符串。", code="config.world.actor_id"
                )
            )
        elif actor_id in seen_ids:
            diagnostics.append(
                self._error(
                    f"{path}.id",
                    f"actor id '{actor_id}' 重复，roster 内必须唯一。",
                    code="config.world.actor_id_duplicate",
                )
            )
        else:
            seen_ids.add(actor_id)

        char_file = actor.get("character_file")
        if char_file is not None and not isinstance(char_file, (str, Path)):
            diagnostics.append(
                self._error(
                    f"{path}.character_file",
                    f"{path}.character_file 必须是字符串路径。",
                    code="config.world.actor_character_file_type",
                )
            )

        self._validate_field_type(f"{path}.enabled", actor.get("enabled"), bool, diagnostics)
        self._validate_field_type(
            f"{path}.initial_scene", actor.get("initial_scene"), str, diagnostics
        )

        is_enabled = bool(actor.get("enabled", True))
        if is_enabled and isinstance(actor_id, str) and actor_id.strip():
            enabled_ids.add(actor_id)
        return 1 if is_enabled else 0

    def _validate_world_director(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        if data is None:
            return
        if not isinstance(data, dict):
            diagnostics.append(
                self._error(
                    "world.director",
                    "world.director 必须是对象。",
                    code="config.world.director_type",
                )
            )
            return
        self._validate_unknown_fields(
            "world.director", data, self._struct_field_names(WorldDirectorConfig), diagnostics
        )
        self._validate_numeric_range(
            "world.director.temperature",
            data.get("temperature"),
            diagnostics,
            minimum=0.0,
            maximum=2.0,
        )
        self._validate_numeric_range(
            "world.director.max_tokens", data.get("max_tokens"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "world.director.max_auto_turns", data.get("max_auto_turns"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "world.director.max_same_actor_turns",
            data.get("max_same_actor_turns"),
            diagnostics,
            minimum=1,
        )
        self._validate_enum(
            "world.director.fallback_action",
            data.get("fallback_action"),
            {"wait_user", "continue"},
            diagnostics,
        )

    def _validate_world_transcript(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        if data is None:
            return
        if not isinstance(data, dict):
            diagnostics.append(
                self._error(
                    "world.transcript",
                    "world.transcript 必须是对象。",
                    code="config.world.transcript_type",
                )
            )
            return
        self._validate_unknown_fields(
            "world.transcript", data, self._struct_field_names(WorldTranscriptConfig), diagnostics
        )
        self._validate_numeric_range(
            "world.transcript.context_entries", data.get("context_entries"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "world.transcript.max_entries_per_scene",
            data.get("max_entries_per_scene"),
            diagnostics,
            minimum=1,
        )

    def _validate_session_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        self._validate_object("session", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "session", data, self._struct_field_names(SessionConfig), diagnostics
        )
        self._validate_numeric_range(
            "session.max_sessions", data.get("max_sessions"), diagnostics, minimum=1
        )

    def _validate_think_engine_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        self._validate_object("think_engine", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "think_engine", data, self._struct_field_names(ThinkEngineConfig), diagnostics
        )
        self._validate_numeric_range(
            "think_engine.think_interval_minutes",
            data.get("think_interval_minutes"),
            diagnostics,
            minimum=1,
        )
        self._validate_numeric_range(
            "think_engine.random_walk_steps_min",
            data.get("random_walk_steps_min"),
            diagnostics,
            minimum=0,
        )
        self._validate_numeric_range(
            "think_engine.random_walk_steps_max",
            data.get("random_walk_steps_max"),
            diagnostics,
            minimum=0,
        )
        self._validate_numeric_range(
            "think_engine.emotional_trigger_threshold",
            data.get("emotional_trigger_threshold"),
            diagnostics,
            minimum=0,
            maximum=1,
        )
        self._validate_numeric_range(
            "think_engine.emotional_priority_probability",
            data.get("emotional_priority_probability"),
            diagnostics,
            minimum=0,
            maximum=1,
        )
        self._validate_numeric_range(
            "think_engine.think_cooldown_minutes",
            data.get("think_cooldown_minutes"),
            diagnostics,
            minimum=0,
        )
        self._validate_numeric_range(
            "think_engine.think_temperature",
            data.get("think_temperature"),
            diagnostics,
            minimum=0,
            maximum=2,
        )
        self._validate_numeric_range(
            "think_engine.think_max_tokens", data.get("think_max_tokens"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "think_engine.initiative_temperature",
            data.get("initiative_temperature"),
            diagnostics,
            minimum=0,
            maximum=2,
        )
        self._validate_numeric_range(
            "think_engine.initiative_max_tokens",
            data.get("initiative_max_tokens"),
            diagnostics,
            minimum=0,
        )
        min_steps = data.get("random_walk_steps_min")
        max_steps = data.get("random_walk_steps_max")
        if (
            isinstance(min_steps, (int, float))
            and isinstance(max_steps, (int, float))
            and min_steps > max_steps
        ):
            diagnostics.append(
                self._error(
                    "think_engine.random_walk_steps_max",
                    "random_walk_steps_max must be >= random_walk_steps_min",
                    "请确保随机游走最大步数不小于最小步数。",
                    code="config.range.cross_field",
                )
            )

    def _validate_initiative_timer_data(
        self, data: Any, diagnostics: list[ConfigDiagnostic]
    ) -> None:
        self._validate_object("initiative_timer", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "initiative_timer",
            data,
            self._struct_field_names(InitiativeTimerConfig)
            # 已删除的强制 fallback 链配置键：不作为未知字段报错，
            # 由 DEPRECATED_FIELDS 给出迁移警告（见类常量）
            | _REMOVED_INITIATIVE_FALLBACK_KEYS,
            diagnostics,
        )
        self._validate_numeric_range(
            "initiative_timer.min_delay_seconds",
            data.get("min_delay_seconds"),
            diagnostics,
            minimum=1,
        )
        self._validate_numeric_range(
            "initiative_timer.max_delay_seconds",
            data.get("max_delay_seconds"),
            diagnostics,
            minimum=1,
        )
        self._validate_numeric_range(
            "initiative_timer.decision_temperature",
            data.get("decision_temperature"),
            diagnostics,
            minimum=0,
            maximum=2,
        )
        self._validate_numeric_range(
            "initiative_timer.decision_max_tokens",
            data.get("decision_max_tokens"),
            diagnostics,
            minimum=200,
        )
        self._validate_numeric_range(
            "initiative_timer.max_pending_summary_chars",
            data.get("max_pending_summary_chars"),
            diagnostics,
            minimum=1,
        )
        self._validate_numeric_range(
            "initiative_timer.hesitation_max_rounds",
            data.get("hesitation_max_rounds"),
            diagnostics,
            minimum=0,
            maximum=10,
        )
        self._validate_hesitation_delay_seconds(data.get("hesitation_delay_seconds"), diagnostics)
        self._validate_numeric_range(
            "initiative_timer.max_initiative_times",
            data.get("max_initiative_times"),
            diagnostics,
            minimum=1,
            maximum=100,
        )
        min_delay = data.get("min_delay_seconds")
        max_delay = data.get("max_delay_seconds")
        if (
            isinstance(min_delay, (int, float))
            and isinstance(max_delay, (int, float))
            and min_delay > max_delay
        ):
            diagnostics.append(
                self._error(
                    "initiative_timer.max_delay_seconds",
                    "max_delay_seconds must be >= min_delay_seconds",
                    "请确保主动定时器最大延迟不小于最小延迟。",
                    code="config.range.cross_field",
                )
            )
        if (
            data.get("allow_frontend_edit_summary") is True
            and data.get("expose_pending_summary") is False
        ):
            diagnostics.append(
                self._error(
                    "initiative_timer.expose_pending_summary",
                    "expose_pending_summary must be true when allow_frontend_edit_summary is true",
                    "前端需要看到积存摘要后才能编辑它。",
                    code="config.initiative_timer.edit_without_expose",
                )
            )

    def _validate_repeat_guard_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        self._validate_object("repeat_guard", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "repeat_guard", data, self._struct_field_names(RepeatGuardConfig), diagnostics
        )
        self._validate_numeric_range(
            "repeat_guard.similarity",
            data.get("similarity"),
            diagnostics,
            minimum=0,
            maximum=1,
        )
        self._validate_numeric_range(
            "repeat_guard.history_size", data.get("history_size"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "repeat_guard.warn_streak", data.get("warn_streak"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "repeat_guard.mute_streak", data.get("mute_streak"), diagnostics, minimum=1
        )
        self._validate_numeric_range(
            "repeat_guard.mute_minutes", data.get("mute_minutes"), diagnostics, minimum=1
        )
        warn_streak = data.get("warn_streak")
        mute_streak = data.get("mute_streak")
        if (
            isinstance(warn_streak, (int, float))
            and isinstance(mute_streak, (int, float))
            and warn_streak > mute_streak
        ):
            diagnostics.append(
                self._error(
                    "repeat_guard.mute_streak",
                    "mute_streak must be >= warn_streak",
                    "「不理」阈值应不小于厌烦阈值，否则角色还没来得及冷淡就直接不理人了。",
                    code="config.range.cross_field",
                )
            )

    def _validate_health_data(self, data: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        self._validate_object("health", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "health", data, self._struct_field_names(HealthConfig), diagnostics
        )
        self._validate_numeric_range(
            "health.quota_warn_yuan", data.get("quota_warn_yuan"), diagnostics, minimum=0
        )
        self._validate_numeric_range(
            "health.quota_crit_yuan", data.get("quota_crit_yuan"), diagnostics, minimum=0
        )
        warn_yuan = data.get("quota_warn_yuan")
        if isinstance(warn_yuan, (int, float)) and warn_yuan <= 0:
            # 警告阈值 0 会让 health.py 的 index 走 else 分支恒为 100（余额耗尽也报满分）
            diagnostics.append(
                self._error(
                    "health.quota_warn_yuan",
                    "quota_warn_yuan must be > 0",
                    "警告阈值必须大于 0，否则健康指数恒为 100 失去意义。",
                    code="config.range.minimum",
                )
            )
        crit_yuan = data.get("quota_crit_yuan")
        if (
            isinstance(warn_yuan, (int, float))
            and isinstance(crit_yuan, (int, float))
            and crit_yuan > warn_yuan
        ):
            diagnostics.append(
                self._error(
                    "health.quota_crit_yuan",
                    "quota_crit_yuan must be <= quota_warn_yuan",
                    "临界阈值应不大于警告阈值，否则🟡就被🟢吞掉了。",
                    code="config.range.cross_field",
                )
            )

    @staticmethod
    def _validate_hesitation_delay_seconds(value: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        if value is None:
            return
        if isinstance(value, str) and value.strip().lower() == "auto":
            return
        if isinstance(value, (int, float)) and value >= 1:
            return
        diagnostics.append(
            ConfigDiagnostic(
                code="config.hesitation_delay_seconds.invalid",
                path="initiative_timer.hesitation_delay_seconds",
                severity="error",
                message="hesitation_delay_seconds must be 'auto' or an integer >= 1",
                suggestion="犹豫延迟须为 'auto' 或不小于 1 的整数秒数",
            )
        )

    def _validate_resource_control_data(
        self, data: Any, diagnostics: list[ConfigDiagnostic]
    ) -> None:
        self._validate_object("resource_control", data, diagnostics)
        if not isinstance(data, dict):
            return
        self._validate_unknown_fields(
            "resource_control",
            data,
            self._struct_field_names(ResourceControlConfig),
            diagnostics,
        )
        non_negative_fields = {
            "runtime_queue_size",
            "acquire_timeout_seconds",
            "default_timeout_seconds",
            "dependency_install_timeout_seconds",
        }
        positive_fields = self._struct_field_names(ResourceControlConfig) - {
            "enabled",
            "overflow_policy",
            *non_negative_fields,
        }
        for field_name in sorted(positive_fields):
            self._validate_numeric_range(
                f"resource_control.{field_name}",
                data.get(field_name),
                diagnostics,
                minimum=1,
            )
        for field_name in sorted(non_negative_fields):
            self._validate_numeric_range(
                f"resource_control.{field_name}",
                data.get(field_name),
                diagnostics,
                minimum=0,
            )
        self._validate_enum(
            "resource_control.overflow_policy",
            data.get("overflow_policy"),
            {"reject", "wait"},
            diagnostics,
        )
        if data.get("overflow_policy") == "wait" and data.get("acquire_timeout_seconds") == 0:
            diagnostics.append(
                self._error(
                    "resource_control.acquire_timeout_seconds",
                    "acquire_timeout_seconds must be > 0 when overflow_policy is wait",
                    "排队等待策略需要设置大于 0 的 acquire_timeout_seconds，或改用 reject 策略。",
                    code="config.resource_control.wait_without_timeout",
                )
            )
        if data.get("overflow_policy") == "reject" and data.get("runtime_queue_size", 0) > 0:
            diagnostics.append(
                self._warning(
                    "resource_control.runtime_queue_size",
                    "runtime_queue_size has no effect when overflow_policy is reject",
                    "reject 策略会快速拒绝，通常可将 runtime_queue_size 设为 0，或改用 wait 策略。",
                    code="config.resource_control.queue_unused",
                )
            )
        runtime_limit = data.get("runtime_max_concurrent")
        for field_name in (
            "session_max_concurrent",
            "stream_max_concurrent",
            "model_max_concurrent",
            "tool_max_concurrent",
            "web_search_max_concurrent",
            "image_generation_max_concurrent",
            "dependency_install_max_concurrent",
        ):
            nested_limit = data.get(field_name)
            if (
                isinstance(runtime_limit, (int, float))
                and not isinstance(runtime_limit, bool)
                and isinstance(nested_limit, (int, float))
                and not isinstance(nested_limit, bool)
                and nested_limit > runtime_limit
            ):
                diagnostics.append(
                    self._warning(
                        f"resource_control.{field_name}",
                        f"{field_name} is greater than runtime_max_concurrent and will be capped by the runtime gate",
                        "子资源并发上限大于 Runtime 总并发时不会真正生效，建议小于或等于 runtime_max_concurrent。",
                        code="config.resource_control.limit_shadowed",
                    )
                )
        default_timeout = data.get("default_timeout_seconds")
        dependency_timeout = data.get("dependency_install_timeout_seconds")
        if (
            isinstance(default_timeout, (int, float))
            and not isinstance(default_timeout, bool)
            and isinstance(dependency_timeout, (int, float))
            and not isinstance(dependency_timeout, bool)
            and dependency_timeout < default_timeout
        ):
            diagnostics.append(
                self._warning(
                    "resource_control.dependency_install_timeout_seconds",
                    "dependency_install_timeout_seconds is shorter than default_timeout_seconds",
                    "依赖安装通常比普通请求更慢，建议保持 dependency_install_timeout_seconds 不小于 default_timeout_seconds。",
                    code="config.resource_control.dependency_timeout_short",
                )
            )

    def _validate_deprecated_fields(
        self, data: dict[str, Any], diagnostics: list[ConfigDiagnostic]
    ) -> None:
        for path, (replacement, note) in self.DEPRECATED_FIELDS.items():
            section, _, field_name = path.partition(".")
            section_data = data.get(section)
            if isinstance(section_data, dict) and field_name in section_data:
                diagnostics.append(
                    self._warning(
                        path,
                        f"Config field '{path}' is deprecated",
                        f"{note} 请改用 {replacement}。",
                        code="config.field.deprecated",
                    )
                )

    def _validate_object(self, path: str, value: Any, diagnostics: list[ConfigDiagnostic]) -> None:
        if not isinstance(value, dict):
            diagnostics.append(
                self._error(
                    path,
                    f"Config section '{path}' must be an object",
                    "请确认该配置段使用 YAML 对象写法。",
                    code="config.section.type",
                )
            )

    def _validate_field_type(
        self,
        path: str,
        value: Any,
        expected: type,
        diagnostics: list[ConfigDiagnostic],
    ) -> None:
        """标量字段类型校验；None 视为未设置（由 Struct 默认值兜底），跳过。"""
        if value is None:
            return
        # bool 是 int 子类：期望 bool 时只认 bool，期望其他类型时排除 bool
        if expected is bool:
            ok = isinstance(value, bool)
        else:
            ok = isinstance(value, expected) and not isinstance(value, bool)
        if not ok:
            diagnostics.append(
                self._error(
                    path,
                    f"{path} 必须是 {expected.__name__} 类型，当前为 {type(value).__name__}。",
                    code="config.field.type",
                )
            )

    def _validate_unknown_fields(
        self,
        section: str,
        data: dict[str, Any],
        allowed: set[str],
        diagnostics: list[ConfigDiagnostic],
    ) -> None:
        unknown = sorted(set(data) - allowed)
        for field_name in unknown:
            path = field_name if section == "$" else f"{section}.{field_name}"
            diagnostics.append(
                self._error(
                    path,
                    f"Unknown config field '{field_name}'",
                    "请检查字段名拼写，或确认当前版本是否支持该配置项。",
                    code="config.field.unknown",
                )
            )

    def _validate_numeric_range(
        self,
        path: str,
        value: Any,
        diagnostics: list[ConfigDiagnostic],
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> None:
        if value is None:
            return
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            diagnostics.append(
                self._error(
                    path,
                    f"Config field '{path}' must be numeric",
                    "请填写数字。",
                    code="config.field.type",
                )
            )
            return
        if minimum is not None and value < minimum:
            diagnostics.append(
                self._error(
                    path,
                    f"Config field '{path}' must be >= {minimum}",
                    f"请填写不小于 {minimum} 的数字。",
                    code="config.field.range",
                )
            )
        if maximum is not None and value > maximum:
            diagnostics.append(
                self._error(
                    path,
                    f"Config field '{path}' must be <= {maximum}",
                    f"请填写不大于 {maximum} 的数字。",
                    code="config.field.range",
                )
            )

    def _validate_enum(
        self,
        path: str,
        value: Any,
        allowed: set[str],
        diagnostics: list[ConfigDiagnostic],
    ) -> None:
        if value is None:
            return
        if value not in allowed:
            diagnostics.append(
                self._error(
                    path,
                    f"Config field '{path}' must be one of: {', '.join(sorted(allowed))}",
                    f"请从这些值中选择一个：{', '.join(sorted(allowed))}。",
                    code="config.field.enum",
                )
            )

    def _validate_string_list(
        self, path: str, value: Any, diagnostics: list[ConfigDiagnostic]
    ) -> None:
        if value is None:
            return
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            diagnostics.append(
                self._error(
                    path,
                    f"Config field '{path}' must be a list of strings",
                    '请使用字符串列表，例如 ["time", "memory"]。',
                    code="config.field.type",
                )
            )

    def _validate_int_list(
        self,
        path: str,
        value: Any,
        diagnostics: list[ConfigDiagnostic],
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> None:
        if value is None:
            return
        if not isinstance(value, list) or any(
            not isinstance(item, int) or isinstance(item, bool) for item in value
        ):
            diagnostics.append(
                self._error(
                    path,
                    f"Config field '{path}' must be a list of integers",
                    "请使用整数列表，例如 [500, 502, 503, 504]。",
                    code="config.field.type",
                )
            )
            return
        for item in value:
            if minimum is not None and item < minimum or maximum is not None and item > maximum:
                diagnostics.append(
                    self._error(
                        path,
                        f"Config field '{path}' contains invalid status code {item}",
                        "HTTP 状态码应在 100 到 599 之间。",
                        code="config.field.range",
                    )
                )
                return

    @staticmethod
    def _known_top_level_fields() -> set[str]:
        return {
            "config_schema_version",
            "log_level",
            "log_console",
            "log_file",
            "debug_silent_output",
            "event_trace_enabled",
            "begin_scene",
            "model",
            "embedding",
            "memory",
            "tool",
            "scene",
            "session",
            "think_engine",
            "initiative_timer",
            "repeat_guard",
            "health",
            "resource_control",
            "world",
            "character",
            "character_file",
        }

    @staticmethod
    def _struct_field_names(struct_type: type) -> set[str]:
        return set(getattr(struct_type, "__struct_fields__", ()))
