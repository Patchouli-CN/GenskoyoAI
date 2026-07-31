"""配置合并逻辑。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config_schema import (
    AppConfig,
    EmbeddingConfig,
    InitiativeTimerConfig,
    LogLevel,
    MemoryConfig,
    ModelConfig,
    ResourceControlConfig,
    SceneConfig,
    SessionConfig,
    ThinkEngineConfig,
    ToolConfig,
    TopicGenerationConfig,
    WebSearchAPIConfig,
    WebSearchToolConfig,
)


class ConfigMerger:
    """配置合并能力。"""

    def __init__(self) -> None:
        self._provided_fields: dict[int, set[str]] = {}

    def _chooser(self, base: Any, override: Any) -> Callable[[str, Any], Any]:
        """生成 choose 闭包：override 显式提供字段优先，否则取 base 值。

        provided 为 None（无显式记录）时统一回落调用方给的 legacy 表达式。
        """
        provided = self._provided_fields.get(id(override))

        def choose(field_name: str, legacy_value: Any) -> Any:
            if provided is not None:
                return (
                    getattr(override, field_name)
                    if field_name in provided
                    else getattr(base, field_name)
                )
            return legacy_value

        return choose

    def _merge(self, base: AppConfig, override: AppConfig) -> AppConfig:
        """兼容旧测试和内部调用的合并入口。"""
        return self.merge(base, override)

    def merge(self, base: AppConfig, override: AppConfig) -> AppConfig:
        """合并配置 - override 优先"""
        result = AppConfig()
        choose = self._chooser(base, override)

        # 日志配置 - override 优先
        result.log_level = choose(
            "log_level",
            override.log_level if override.log_level != LogLevel.INFO else base.log_level,
        )
        result.log_console = choose("log_console", override.log_console)
        result.log_file = choose("log_file", override.log_file or base.log_file)
        result.debug_silent_output = choose(
            "debug_silent_output",
            override.debug_silent_output or base.debug_silent_output,
        )
        result.event_trace_enabled = choose(
            "event_trace_enabled",
            override.event_trace_enabled or base.event_trace_enabled,
        )

        # 其他配置 - override 优先
        result.model = self._merge_model(base.model, override.model)
        result.embedding = self._merge_embedding(base.embedding, override.embedding)
        result.memory = self._merge_memory(base.memory, override.memory)
        result.tool = self._merge_tool(base.tool, override.tool)
        result.scene = self._merge_scene(base.scene, override.scene)
        result.session = self._merge_session(base.session, override.session)
        result.think_engine = self._merge_think_engine(base.think_engine, override.think_engine)
        result.initiative_timer = self._merge_initiative_timer(
            base.initiative_timer,
            override.initiative_timer,
        )
        result.resource_control = self._merge_resource_control(
            base.resource_control,
            override.resource_control,
        )
        # world 为整节覆盖：actors 列表无法逐字段合并，用户提供则整体优先。
        result.world = choose("world", override.world if override.world.enabled else base.world)
        result.character = override.character or base.character
        result.character_file = override.character_file or base.character_file

        return result

    def _merge_model(self, base: ModelConfig, override: ModelConfig) -> ModelConfig:
        """合并模型配置 - override 优先。

        从 YAML 加载的配置会保留字段出现信息，避免用默认值猜测用户是否有意覆盖；
        对直接构造的 ModelConfig 仍保留旧的默认值回退策略以兼容现有调用。
        """
        choose = self._chooser(base, override)

        return ModelConfig(
            provider=choose(
                "provider", override.provider if override.provider != "ollama" else base.provider
            ),
            name=choose("name", override.name if override.name != "qwen3.5:9b" else base.name),
            base_url=choose("base_url", override.base_url or base.base_url),
            api_path=choose("api_path", override.api_path or base.api_path),
            api_key=choose("api_key", override.api_key or base.api_key),
            extra_headers=choose("extra_headers", override.extra_headers or base.extra_headers),
            auth=choose("auth", override.auth or base.auth),
            model_capabilities_add=choose(
                "model_capabilities_add",
                override.model_capabilities_add or base.model_capabilities_add,
            ),
            model_capabilities_remove=choose(
                "model_capabilities_remove",
                override.model_capabilities_remove or base.model_capabilities_remove,
            ),
            web_search_enabled=choose("web_search_enabled", override.web_search_enabled),
            web_search_strategy=choose("web_search_strategy", override.web_search_strategy),
            web_search_context_size=choose(
                "web_search_context_size",
                override.web_search_context_size or base.web_search_context_size,
            ),
            web_search_user_location=choose(
                "web_search_user_location",
                override.web_search_user_location or base.web_search_user_location,
            ),
            web_search_allow_fallback=choose(
                "web_search_allow_fallback", override.web_search_allow_fallback
            ),
            web_search_metadata=choose(
                "web_search_metadata",
                override.web_search_metadata or base.web_search_metadata,
            ),
            stream=choose("stream", override.stream),
            think=choose("think", override.think),
            thinking_enabled=choose(
                "thinking_enabled",
                override.thinking_enabled
                if override.thinking_enabled is not None
                else base.thinking_enabled,
            ),
            reasoning_effort=choose(
                "reasoning_effort", override.reasoning_effort or base.reasoning_effort
            ),
            temperature=choose(
                "temperature",
                override.temperature if override.temperature != 0.7 else base.temperature,
            ),
            top_p=choose("top_p", override.top_p if override.top_p != 0.9 else base.top_p),
            max_tokens=choose(
                "max_tokens",
                override.max_tokens if override.max_tokens != 2048 else base.max_tokens,
            ),
            timeout=choose("timeout", override.timeout if override.timeout != 60 else base.timeout),
            use_proxy=choose(
                "use_proxy",
                override.use_proxy if override.use_proxy != base.use_proxy else base.use_proxy,
            ),
            retry_max_attempts=choose(
                "retry_max_attempts",
                override.retry_max_attempts
                if override.retry_max_attempts != 3
                else base.retry_max_attempts,
            ),
            retry_initial_delay=choose(
                "retry_initial_delay",
                override.retry_initial_delay
                if override.retry_initial_delay != 1.0
                else base.retry_initial_delay,
            ),
            retry_backoff_factor=choose(
                "retry_backoff_factor",
                override.retry_backoff_factor
                if override.retry_backoff_factor != 2.0
                else base.retry_backoff_factor,
            ),
            retry_status_codes=choose(
                "retry_status_codes",
                override.retry_status_codes or base.retry_status_codes,
            ),
        )

    def _merge_embedding(self, base: EmbeddingConfig, override: EmbeddingConfig) -> EmbeddingConfig:
        """合并 Embedding 配置 - override 优先。"""
        choose = self._chooser(base, override)

        return EmbeddingConfig(
            provider=choose("provider", override.provider or base.provider),
            name=choose("name", override.name or base.name),
            base_url=choose("base_url", override.base_url or base.base_url),
            api_key=choose("api_key", override.api_key or base.api_key),
            dimensions=choose("dimensions", override.dimensions or base.dimensions),
            encoding_format=choose(
                "encoding_format", override.encoding_format or base.encoding_format
            ),
            timeout=choose("timeout", override.timeout or base.timeout),
            use_proxy=choose(
                "use_proxy",
                override.use_proxy if override.use_proxy is not None else base.use_proxy,
            ),
        )

    def _merge_memory(self, base: MemoryConfig, override: MemoryConfig) -> MemoryConfig:
        """合并记忆配置 - override 优先。"""
        choose = self._chooser(base, override)

        return MemoryConfig(
            working_max_turns=choose(
                "working_max_turns",
                override.working_max_turns
                if override.working_max_turns != 20
                else base.working_max_turns,
            ),
            episodic_threshold=choose(
                "episodic_threshold",
                override.episodic_threshold
                if override.episodic_threshold != 50
                else base.episodic_threshold,
            ),
            episodic_summary_model=choose(
                "episodic_summary_model",
                override.episodic_summary_model
                if override.episodic_summary_model is not None
                else base.episodic_summary_model,
            ),
            episodic_keep_recent=choose(
                "episodic_keep_recent",
                override.episodic_keep_recent
                if override.episodic_keep_recent != 10
                else base.episodic_keep_recent,
            ),
            semantic_enabled=choose("semantic_enabled", override.semantic_enabled),
            semantic_top_k=choose(
                "semantic_top_k",
                override.semantic_top_k if override.semantic_top_k != 5 else base.semantic_top_k,
            ),
            semantic_similarity_threshold=choose(
                "semantic_similarity_threshold",
                override.semantic_similarity_threshold
                if override.semantic_similarity_threshold != 0.7
                else base.semantic_similarity_threshold,
            ),
            auto_memory_enabled=choose("auto_memory_enabled", override.auto_memory_enabled),
            auto_memory_model=choose(
                "auto_memory_model",
                override.auto_memory_model
                if override.auto_memory_model is not None
                else base.auto_memory_model,
            ),
            distill_enabled=choose("distill_enabled", override.distill_enabled),
            distill_turns=choose(
                "distill_turns",
                override.distill_turns if override.distill_turns != 10 else base.distill_turns,
            ),
            topic_generation=self._merge_topic_generation(
                base.topic_generation,
                override.topic_generation,
            ),
        )

    def _merge_topic_generation(
        self,
        base: TopicGenerationConfig,
        override: TopicGenerationConfig,
    ) -> TopicGenerationConfig:
        """合并话题生成配置。"""
        choose = self._chooser(base, override)

        return TopicGenerationConfig(
            name_max_length=choose(
                "name_max_length",
                override.name_max_length
                if override.name_max_length != 10
                else base.name_max_length,
            ),
            summary_max_length=choose(
                "summary_max_length",
                override.summary_max_length
                if override.summary_max_length != 100
                else base.summary_max_length,
            ),
        )

    def _dict_to_tool_config(self, data: dict[str, Any]) -> ToolConfig:
        """字典转工具配置，处理嵌套 Web search 配置。"""
        tool_data = dict(data)
        web_search_data = tool_data.pop("web_search", None)
        if isinstance(web_search_data, dict):
            web_search_data = dict(web_search_data)
            api_data = web_search_data.pop("api", None)
            web_search_config = WebSearchToolConfig(**web_search_data)
            if isinstance(api_data, dict):
                web_search_config.api = WebSearchAPIConfig(**api_data)
            tool_data["web_search"] = web_search_config
        return ToolConfig(**tool_data)

    def _merge_web_search_api(
        self,
        base: WebSearchAPIConfig,
        override: WebSearchAPIConfig,
    ) -> WebSearchAPIConfig:
        """合并 Web search API Provider 配置。"""
        choose = self._chooser(base, override)

        return WebSearchAPIConfig(
            endpoint=choose("endpoint", override.endpoint or base.endpoint),
            method=choose("method", override.method if override.method != "POST" else base.method),
            api_key=choose("api_key", override.api_key or base.api_key),
            api_key_header=choose(
                "api_key_header",
                override.api_key_header
                if override.api_key_header != "Authorization"
                else base.api_key_header,
            ),
            api_key_prefix=choose(
                "api_key_prefix",
                override.api_key_prefix
                if override.api_key_prefix != "Bearer "
                else base.api_key_prefix,
            ),
            headers=choose("headers", override.headers or base.headers),
            request_template=choose(
                "request_template", override.request_template or base.request_template
            ),
            query_params=choose("query_params", override.query_params or base.query_params),
            results_path=choose(
                "results_path",
                override.results_path if override.results_path != "results" else base.results_path,
            ),
            title_path=choose(
                "title_path",
                override.title_path if override.title_path != "title" else base.title_path,
            ),
            url_path=choose(
                "url_path",
                override.url_path if override.url_path != "url" else base.url_path,
            ),
            snippet_path=choose(
                "snippet_path",
                override.snippet_path if override.snippet_path != "content" else base.snippet_path,
            ),
            published_at_path=choose(
                "published_at_path",
                override.published_at_path or base.published_at_path,
            ),
        )

    def _merge_web_search_tool(
        self,
        base: WebSearchToolConfig,
        override: WebSearchToolConfig,
    ) -> WebSearchToolConfig:
        """合并自有 Web search 工具配置。"""
        choose = self._chooser(base, override)

        default_user_agent = WebSearchToolConfig().user_agent
        return WebSearchToolConfig(
            enabled=choose(
                "enabled", override.enabled if override.enabled != base.enabled else base.enabled
            ),
            provider=choose(
                "provider", override.provider if override.provider != "bing" else base.provider
            ),
            max_results=choose(
                "max_results",
                override.max_results if override.max_results != 10 else base.max_results,
            ),
            timeout=choose("timeout", override.timeout if override.timeout != 10 else base.timeout),
            cache_ttl_seconds=choose(
                "cache_ttl_seconds",
                override.cache_ttl_seconds
                if override.cache_ttl_seconds != 300
                else base.cache_ttl_seconds,
            ),
            user_agent=choose(
                "user_agent",
                override.user_agent
                if override.user_agent != default_user_agent
                else base.user_agent,
            ),
            trigger_strategy=choose(
                "trigger_strategy",
                override.trigger_strategy
                if override.trigger_strategy != "explicit"
                else base.trigger_strategy,
            ),
            freshness_keywords=choose(
                "freshness_keywords", override.freshness_keywords or base.freshness_keywords
            ),
            prefer_for_characters=choose(
                "prefer_for_characters",
                override.prefer_for_characters or base.prefer_for_characters,
            ),
            prefer_for_scenarios=choose(
                "prefer_for_scenarios",
                override.prefer_for_scenarios or base.prefer_for_scenarios,
            ),
            region=choose("region", override.region or base.region),
            safe_search=choose(
                "safe_search",
                override.safe_search if override.safe_search != "moderate" else base.safe_search,
            ),
            snippet_max_length=choose(
                "snippet_max_length",
                override.snippet_max_length
                if override.snippet_max_length != 200
                else base.snippet_max_length,
            ),
            api=self._merge_web_search_api(base.api, override.api),
        )

    def _merge_tool(self, base: ToolConfig, override: ToolConfig) -> ToolConfig:
        """合并工具配置。"""
        choose = self._chooser(base, override)

        return ToolConfig(
            enabled=choose(
                "enabled", override.enabled if override.enabled != base.enabled else base.enabled
            ),
            builtin_tools=choose(
                "builtin_tools",
                override.builtin_tools
                if override.builtin_tools != base.builtin_tools
                else base.builtin_tools,
            ),
            custom_tools_path=choose(
                "custom_tools_path", override.custom_tools_path or base.custom_tools_path
            ),
            web_search=self._merge_web_search_tool(base.web_search, override.web_search),
        )

    def _merge_scene(self, base: SceneConfig, override: SceneConfig) -> SceneConfig:
        """合并场景配置。"""
        provided = self._provided_fields.get(id(override))
        default_path = Path("./scenes")

        def choose(field_name: str, legacy_value: Any) -> Any:
            if provided is not None:
                return (
                    getattr(override, field_name)
                    if field_name in provided
                    else getattr(base, field_name)
                )
            return legacy_value

        return SceneConfig(
            enabled=choose(
                "enabled",
                override.enabled if override.enabled != base.enabled else base.enabled,
            ),
            library_path=choose(
                "library_path",
                override.library_path
                if override.library_path != default_path
                else base.library_path,
            ),
            default_scene=choose(
                "default_scene",
                override.default_scene or base.default_scene,
            ),
            enforce_connectivity=choose(
                "enforce_connectivity",
                override.enforce_connectivity
                if override.enforce_connectivity != base.enforce_connectivity
                else base.enforce_connectivity,
            ),
        )

    def _merge_session(self, base: SessionConfig, override: SessionConfig) -> SessionConfig:
        """合并会话配置。"""
        provided = self._provided_fields.get(id(override))
        default_path = Path("./sessions")

        def choose(field_name: str, legacy_value: Any) -> Any:
            if provided is not None:
                return (
                    getattr(override, field_name)
                    if field_name in provided
                    else getattr(base, field_name)
                )
            return legacy_value

        return SessionConfig(
            auto_save=choose(
                "auto_save",
                override.auto_save if override.auto_save != base.auto_save else base.auto_save,
            ),
            save_path=choose(
                "save_path",
                override.save_path if override.save_path != default_path else base.save_path,
            ),
            max_sessions=choose(
                "max_sessions",
                override.max_sessions if override.max_sessions != 100 else base.max_sessions,
            ),
        )

    def _merge_resource_control(
        self,
        base: ResourceControlConfig,
        override: ResourceControlConfig,
    ) -> ResourceControlConfig:
        """合并 Runtime 资源控制配置。"""
        choose = self._chooser(base, override)

        defaults = ResourceControlConfig()
        return ResourceControlConfig(
            enabled=choose(
                "enabled", override.enabled if override.enabled != base.enabled else base.enabled
            ),
            runtime_max_concurrent=choose(
                "runtime_max_concurrent",
                override.runtime_max_concurrent
                if override.runtime_max_concurrent != defaults.runtime_max_concurrent
                else base.runtime_max_concurrent,
            ),
            runtime_queue_size=choose(
                "runtime_queue_size",
                override.runtime_queue_size
                if override.runtime_queue_size != defaults.runtime_queue_size
                else base.runtime_queue_size,
            ),
            session_max_concurrent=choose(
                "session_max_concurrent",
                override.session_max_concurrent
                if override.session_max_concurrent != defaults.session_max_concurrent
                else base.session_max_concurrent,
            ),
            provider_max_concurrent=choose(
                "provider_max_concurrent",
                override.provider_max_concurrent
                if override.provider_max_concurrent != defaults.provider_max_concurrent
                else base.provider_max_concurrent,
            ),
            stream_max_concurrent=choose(
                "stream_max_concurrent",
                override.stream_max_concurrent
                if override.stream_max_concurrent != defaults.stream_max_concurrent
                else base.stream_max_concurrent,
            ),
            model_max_concurrent=choose(
                "model_max_concurrent",
                override.model_max_concurrent
                if override.model_max_concurrent != defaults.model_max_concurrent
                else base.model_max_concurrent,
            ),
            tool_max_concurrent=choose(
                "tool_max_concurrent",
                override.tool_max_concurrent
                if override.tool_max_concurrent != defaults.tool_max_concurrent
                else base.tool_max_concurrent,
            ),
            web_search_max_concurrent=choose(
                "web_search_max_concurrent",
                override.web_search_max_concurrent
                if override.web_search_max_concurrent != defaults.web_search_max_concurrent
                else base.web_search_max_concurrent,
            ),
            image_generation_max_concurrent=choose(
                "image_generation_max_concurrent",
                override.image_generation_max_concurrent
                if override.image_generation_max_concurrent
                != defaults.image_generation_max_concurrent
                else base.image_generation_max_concurrent,
            ),
            dependency_install_max_concurrent=choose(
                "dependency_install_max_concurrent",
                override.dependency_install_max_concurrent
                if override.dependency_install_max_concurrent
                != defaults.dependency_install_max_concurrent
                else base.dependency_install_max_concurrent,
            ),
            acquire_timeout_seconds=choose(
                "acquire_timeout_seconds",
                override.acquire_timeout_seconds
                if override.acquire_timeout_seconds != defaults.acquire_timeout_seconds
                else base.acquire_timeout_seconds,
            ),
            default_timeout_seconds=choose(
                "default_timeout_seconds",
                override.default_timeout_seconds
                if override.default_timeout_seconds != defaults.default_timeout_seconds
                else base.default_timeout_seconds,
            ),
            dependency_install_timeout_seconds=choose(
                "dependency_install_timeout_seconds",
                override.dependency_install_timeout_seconds
                if override.dependency_install_timeout_seconds
                != defaults.dependency_install_timeout_seconds
                else base.dependency_install_timeout_seconds,
            ),
            overflow_policy=choose(
                "overflow_policy",
                override.overflow_policy
                if override.overflow_policy != defaults.overflow_policy
                else base.overflow_policy,
            ),
        )

    def _merge_initiative_timer(
        self,
        base: InitiativeTimerConfig,
        override: InitiativeTimerConfig,
    ) -> InitiativeTimerConfig:
        """合并主动定时器配置。"""
        choose = self._chooser(base, override)

        defaults = InitiativeTimerConfig()
        return InitiativeTimerConfig(
            enabled=choose(
                "enabled", override.enabled if override.enabled != base.enabled else base.enabled
            ),
            min_delay_seconds=choose(
                "min_delay_seconds",
                override.min_delay_seconds
                if override.min_delay_seconds != defaults.min_delay_seconds
                else base.min_delay_seconds,
            ),
            max_delay_seconds=choose(
                "max_delay_seconds",
                override.max_delay_seconds
                if override.max_delay_seconds != defaults.max_delay_seconds
                else base.max_delay_seconds,
            ),
            decision_temperature=choose(
                "decision_temperature",
                override.decision_temperature
                if override.decision_temperature != defaults.decision_temperature
                else base.decision_temperature,
            ),
            decision_max_tokens=choose(
                "decision_max_tokens",
                override.decision_max_tokens
                if override.decision_max_tokens != defaults.decision_max_tokens
                else base.decision_max_tokens,
            ),
            max_pending_summary_chars=choose(
                "max_pending_summary_chars",
                override.max_pending_summary_chars
                if override.max_pending_summary_chars != defaults.max_pending_summary_chars
                else base.max_pending_summary_chars,
            ),
            allow_frontend_edit_summary=choose(
                "allow_frontend_edit_summary",
                override.allow_frontend_edit_summary,
            ),
            replace_user_modified_timer=choose(
                "replace_user_modified_timer",
                override.replace_user_modified_timer,
            ),
            expose_pending_summary=choose(
                "expose_pending_summary",
                override.expose_pending_summary,
            ),
            max_initiative_times=choose(
                "max_initiative_times",
                override.max_initiative_times
                if override.max_initiative_times != defaults.max_initiative_times
                else base.max_initiative_times,
            ),
            drive_threshold=choose(
                "drive_threshold",
                override.drive_threshold
                if override.drive_threshold != defaults.drive_threshold
                else base.drive_threshold,
            ),
        )

    def _merge_think_engine(
        self, base: ThinkEngineConfig, override: ThinkEngineConfig
    ) -> ThinkEngineConfig:
        """合并思考引擎配置。"""
        choose = self._chooser(base, override)

        return ThinkEngineConfig(
            enabled=choose(
                "enabled", override.enabled if override.enabled != base.enabled else base.enabled
            ),
            think_interval_minutes=choose(
                "think_interval_minutes",
                override.think_interval_minutes
                if override.think_interval_minutes != 5
                else base.think_interval_minutes,
            ),
            random_walk_steps_min=choose(
                "random_walk_steps_min",
                override.random_walk_steps_min
                if override.random_walk_steps_min != 2
                else base.random_walk_steps_min,
            ),
            random_walk_steps_max=choose(
                "random_walk_steps_max",
                override.random_walk_steps_max
                if override.random_walk_steps_max != 5
                else base.random_walk_steps_max,
            ),
            emotional_trigger_threshold=choose(
                "emotional_trigger_threshold",
                override.emotional_trigger_threshold
                if override.emotional_trigger_threshold != 0.5
                else base.emotional_trigger_threshold,
            ),
            emotional_priority_probability=choose(
                "emotional_priority_probability",
                override.emotional_priority_probability
                if override.emotional_priority_probability != 0.7
                else base.emotional_priority_probability,
            ),
            think_temperature=choose(
                "think_temperature",
                override.think_temperature
                if override.think_temperature != 0.7
                else base.think_temperature,
            ),
            think_max_tokens=choose(
                "think_max_tokens",
                override.think_max_tokens
                if override.think_max_tokens != 200
                else base.think_max_tokens,
            ),
            initiative_temperature=choose(
                "initiative_temperature",
                override.initiative_temperature
                if override.initiative_temperature != 0.8
                else base.initiative_temperature,
            ),
            initiative_max_tokens=choose(
                "initiative_max_tokens",
                override.initiative_max_tokens
                if override.initiative_max_tokens != 100
                else base.initiative_max_tokens,
            ),
        )
