"""配置 schema 定义。"""

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from msgspec import Struct, field

from ..utils.logger import setup_logging


class LogLevel(Enum):
    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class AuthConfig(Struct):
    """模型 Provider 认证配置。"""

    auth_type: str | None = None
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None
    refresh_token: str | None = None
    access_token: str | None = None
    expires_at: float | None = None
    refresh_before_seconds: int = 60
    auth_headers: dict[str, str] = field(default_factory=dict)
    auth_body: dict[str, str] = field(default_factory=dict)
    token_field: str = "access_token"
    expires_in_field: str = "expires_in"
    allow_401_refresh: bool = True


class ModelConfig(Struct):
    """模型配置"""

    provider: str = (
        "ollama"  # LLM Provider: ollama / openai / openrouter / deepseek / gemini / claude
    )
    name: str = "qwen3.5:9b"
    base_url: str | None = None
    api_path: str | None = None
    api_key: str | None = None  # API 密钥（OpenAI/Gemini/Claude 等需要）
    extra_headers: dict[str, str] = field(default_factory=dict)
    auth: AuthConfig | None = None
    model_capabilities_add: list[str] = field(default_factory=list)
    model_capabilities_remove: list[str] = field(default_factory=list)
    web_search_enabled: bool = False
    web_search_strategy: Literal["off", "explicit", "auto"] = "off"
    web_search_context_size: str | None = None
    web_search_user_location: dict[str, Any] = field(default_factory=dict)
    web_search_allow_fallback: bool = True
    web_search_metadata: dict[str, Any] = field(default_factory=dict)
    stream: bool = True
    think: bool = False
    thinking_enabled: bool | None = None
    reasoning_effort: str | None = None
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 2048
    timeout: int = 60
    use_proxy: bool = False  # 是否使用代理
    retry_max_attempts: int = 3
    retry_initial_delay: float = 1.0
    retry_backoff_factor: float = 2.0
    retry_status_codes: list[int] = field(default_factory=lambda: [500, 502, 503, 504])
    # 单次调用成本估算单价（元/百万 token）：配置后 ModelClient 按 usage 采样
    # 单次成本，供额度健康按消耗中位数动态计算阈值；None = 不估算（无法动态）
    price_input_per_million: float | None = None
    price_output_per_million: float | None = None
    # 缓存命中部分的输入单价（元/百万 token，如 kimi 缓存命中价）：
    # 配置后按真消耗分项计费（缓存读取 token 按此价）；None = 缓存也按全价（保守）
    price_input_cached_per_million: float | None = None
    # 缓存写入部分的输入单价（元/百万 token，如 Anthropic 写入溢价 1.25× 输入价）：
    # None = 按普通输入全价计（Moonshot 无写入溢价时的正确口径）
    price_input_cache_write_per_million: float | None = None


class EmbeddingConfig(Struct):
    """Embedding 模型配置"""

    provider: str | None = None  # 默认复用 model.provider
    name: str | None = None  # 必填；未配置时不再误用聊天模型
    base_url: str | None = None
    api_key: str | None = None
    dimensions: int | None = None
    encoding_format: str | None = None
    timeout: int | None = None
    use_proxy: bool | None = None


class TopicGenerationConfig(Struct):
    """话题生成配置"""

    name_max_length: int = 10
    summary_max_length: int = 100


class MemoryConfig(Struct):
    """记忆配置"""

    working_max_turns: int = 20
    semantic_enabled: bool = True
    semantic_top_k: int = 5
    semantic_similarity_threshold: float = 0.7
    # 话题数写入侧上限：达到上限新增话题时，淘汰回忆权重最低的非 pin 话题（§8.33）
    semantic_max_topics: int = 50
    auto_memory_enabled: bool = True
    # 自动记忆模型；None = 跟随主模型。当前无消费方（预留字段）。
    auto_memory_model: str | None = None
    # 定期记忆蒸馏（§8.29）：每 distill_turns 轮对话后，从近期工作记忆自动
    # 提炼「珍贵记忆」写入语义记忆（确定性触发，替代已删除的 AI 主动记忆工具）
    distill_enabled: bool = True
    distill_turns: int = 10
    # 话题热度淘汰（§8.32，参考 Lumi_Nox decay）：读取时按半衰期现算话题热度，
    # 低于阈值的话题对主动机制（静默思考游走）隐藏而非删除，被重新谈起时自然复活；
    # 重要性达到 topic_pin_importance 的话题免疫衰减
    topic_decay_enabled: bool = True
    topic_half_life_hours: float = 72.0
    topic_decay_threshold: float = 0.1
    topic_pin_importance: float = 8.0

    topic_generation: TopicGenerationConfig = field(default_factory=TopicGenerationConfig)


class ThinkEngineConfig(Struct):
    """思考引擎配置"""

    enabled: bool = True  # 是否启用静默思考
    think_interval_minutes: int = 5  # 思考间隔（分钟）
    random_walk_steps_min: int = 2  # 随机游走最少步数
    random_walk_steps_max: int = 5  # 随机游走最多步数
    emotional_trigger_threshold: float = 0.5  # 优先选择高情感话题的阈值
    emotional_priority_probability: float = 0.7  # 优先选择高情感话题的概率
    think_cooldown_minutes: int = 10  # 话题被思考后多少分钟内降低再次选中概率
    walk_visit_dedup: bool = True  # 单次随机游走是否避免重复访问同一话题
    think_temperature: float = 0.7  # 思考时的温度
    think_max_tokens: int = 200  # 思考最大 token 数
    initiative_temperature: float = 0.8  # 生成主动消息时的温度
    initiative_max_tokens: int = 0  # 生成主动消息最大 token 数；0 表示不限制


class InitiativeTimerConfig(Struct):
    """回答后主动定时器配置。"""

    enabled: bool = True
    min_delay_seconds: int = 30
    max_delay_seconds: int = 1800
    decision_temperature: float = 0.4
    decision_max_tokens: int = 300
    max_pending_summary_chars: int = 240
    allow_frontend_edit_summary: bool = True
    replace_user_modified_timer: bool = True
    expose_pending_summary: bool = True
    max_initiative_times: int = 1  # 用户回复后最多连续主动发言次数；达到上限后暂停主动定时器

    # 对话欲（§7.3，2026-07-30 用户定稿）：ThinkEngine 四维心情模型打分
    # （一次短 JSON LLM），total_drive 超阈值即「想说」，否则沉默——
    # 无累积器、无犹豫链、无强制 fallback，二元判断由阈值独立完成。
    drive_threshold: float = 0.6  # total_drive（0~1 加权）超过该值即主动发言


class RepeatGuardConfig(Struct):
    """复读烦躁模型：同一用户连续复读/刷屏时，角色逐渐厌烦直至暂时不理。

    判重与连击计数在接入层（如 nb2 QQ 适配器）按发送者进行——发送者身份只
    存在于接入层；Runtime/Agent 只消费注入的厌烦上下文。被「不理」期间的
    消息在适配器侧直接丢弃，不进 Runtime，零 token 消耗。
    """

    enabled: bool = True
    similarity: float = 0.75  # 与近期消息相似度 ≥ 该值判为复读（0~1）
    history_size: int = 5  # 每用户参与判重的近期消息条数
    warn_streak: int = 3  # 连续复读达到该次数：注入厌烦情绪，回复转冷淡
    mute_streak: int = 5  # 连续复读达到该次数：角色最后一句话表态，随后进入「不理」冷却
    mute_minutes: int = 10  # 「不理」冷却时长（分钟），期间复读消息直接忽略
    # 冷却期间遇到「有新意」的内容时，交给 LLM 以角色性格做破例判定
    # （诚恳道歉/真心请求可能消气，有趣内容可能破例回一句）；
    # False = 一律静默到冷却结束，零额外 token
    llm_break: bool = True


class HealthConfig(Struct):
    """框架健康中心（core.health.HealthCenter）的判定阈值。

    健康判定一律走这里的静态阈值——重启不漂移（2026-08-02 用户定稿：
    运行时估算的动态阈值重启即失效，砍判定、留计费计量）。
    """

    quota_warn_yuan: float = 20.0  # 额度余额低于该值 → 🟡 警告
    quota_crit_yuan: float = 5.0  # 低于该值 → 🔴 临界；≤ 0 → 🟣 耗尽


class WebSearchAPIConfig(Struct):
    """自有 Web search API Provider 配置。"""

    endpoint: str | None = None
    method: str = "POST"
    api_key: str | None = None
    api_key_header: str = "Authorization"
    api_key_prefix: str = "Bearer "
    headers: dict[str, str] = field(default_factory=dict)
    request_template: dict[str, Any] = field(
        default_factory=lambda: {"query": "{query}", "count": "{max_results}"}
    )
    query_params: dict[str, Any] = field(default_factory=dict)
    results_path: str = "results"
    title_path: str = "title"
    url_path: str = "url"
    snippet_path: str = "content"
    published_at_path: str | None = None


class WebSearchToolConfig(Struct):
    """自有 Web search 工具配置。"""

    enabled: bool = False
    provider: str = "ddg"  # ddg / bing / api / mixed
    max_results: int = 10
    timeout: int = 10
    cache_ttl_seconds: int = 300
    trigger_strategy: Literal["off", "explicit", "auto"] = "explicit"
    freshness_keywords: list[str] = field(
        default_factory=lambda: [
            "今天",
            "今日",
            "现在",
            "当前",
            "最新",
            "新闻",
            "价格",
            "版本",
            "发布",
            "更新",
            "today",
            "latest",
            "news",
            "price",
            "version",
        ]
    )
    prefer_for_characters: list[str] = field(default_factory=list)
    prefer_for_scenarios: list[str] = field(default_factory=list)
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    region: str | None = None
    safe_search: str = "moderate"
    snippet_max_length: int = 200
    api: WebSearchAPIConfig = field(default_factory=WebSearchAPIConfig)


class ToolConfig(Struct):
    """工具配置"""

    enabled: bool = True
    builtin_tools: list[str] = field(default_factory=lambda: ["time", "moon", "memory", "system"])
    custom_tools_path: Path | None = None
    web_search: WebSearchToolConfig = field(default_factory=WebSearchToolConfig)


class SceneConfig(Struct):
    """场景配置。

    场景库全局共享，从 library_path 目录加载 *.yaml。当前场景是会话级状态，
    持久化在 SessionContext.metadata；场景上下文仅在对话开始时注入一次。
    """

    enabled: bool = False
    library_path: Path = field(default_factory=lambda: Path("./scenes"))
    default_scene: str | None = None  # 未从会话恢复到场景时的默认起始场景 id
    enforce_connectivity: bool = False  # True 时切换必须走 connected_scenes

    def __post_init__(self):
        if not isinstance(self.library_path, Path):
            object.__setattr__(self, "library_path", Path(self.library_path))


class SessionConfig(Struct):
    """会话配置"""

    auto_save: bool = True
    save_path: Path = field(default_factory=lambda: Path("./sessions"))
    max_sessions: int = 100

    def __post_init__(self):
        # 强制转换为 Path 对象
        if not isinstance(self.save_path, Path):
            object.__setattr__(self, "save_path", Path(self.save_path))


class ResourceControlConfig(Struct):
    """Runtime 资源控制配置。"""

    enabled: bool = True
    runtime_max_concurrent: int = 4
    runtime_queue_size: int = 8
    session_max_concurrent: int = 1
    provider_max_concurrent: int = 2
    stream_max_concurrent: int = 1
    model_max_concurrent: int = 2
    tool_max_concurrent: int = 2
    web_search_max_concurrent: int = 1
    image_generation_max_concurrent: int = 1
    dependency_install_max_concurrent: int = 1
    acquire_timeout_seconds: float = 0.25
    default_timeout_seconds: float = 120.0
    dependency_install_timeout_seconds: int = 600
    overflow_policy: Literal["reject", "wait"] = "reject"
    # 每用户同时在内存中装配的租户 Agent 上限；达到上限时休眠最久未活跃租户
    # （会话保存、磁盘数据保留，再次发言自动唤醒），全部繁忙才报 agent.limit_exceeded
    tenant_max_agents_per_user: int = 32


class WorldActorConfig(Struct):
    """World 中一个角色（Actor）的装配配置。"""

    id: str  # 稳定 ASCII/roster id，与角色显示名分离
    character_file: Path | None = None
    initial_scene: str | None = None
    enabled: bool = True

    def __post_init__(self):
        if self.character_file is not None and not isinstance(self.character_file, Path):
            object.__setattr__(self, "character_file", Path(self.character_file))


class WorldDirectorConfig(Struct):
    """导演决策配置。"""

    enabled: bool = True
    temperature: float = 0.2
    max_tokens: int = 384
    max_auto_turns: int = 4  # 一段自动表演最多连续多少轮后强制交还用户
    max_same_actor_turns: int = 2  # 同一角色最多连续发言轮数
    fallback_action: Literal["wait_user", "continue"] = "wait_user"


class WorldTranscriptConfig(Struct):
    """共享剧本配置。"""

    context_entries: int = 24  # 每轮注入模型的最近共享剧本条数
    max_entries_per_scene: int = 500  # 每个场景分片保留上限


class WorldPersistenceConfig(Struct):
    """World 会话持久化配置。"""

    enabled: bool = True
    save_path: Path = field(default_factory=lambda: Path("./sessions/world"))

    def __post_init__(self):
        if not isinstance(self.save_path, Path):
            object.__setattr__(self, "save_path", Path(self.save_path))


class WorldConfig(Struct):
    """多角色 World 编排配置。默认关闭，不影响单角色模式。"""

    enabled: bool = False
    id: str = "gensokyo"
    protagonist: str = "__user__"  # "__user__" 或 roster 中某个 actor id
    user_initial_scene: str | None = None
    actors: list[WorldActorConfig] = field(default_factory=list)
    director: WorldDirectorConfig = field(default_factory=WorldDirectorConfig)
    transcript: WorldTranscriptConfig = field(default_factory=WorldTranscriptConfig)
    persistence: WorldPersistenceConfig = field(default_factory=WorldPersistenceConfig)
    project_perspective_memories: bool = True  # 是否为在场角色各写各视角记忆
    user_follows_current_actor: bool = True  # 当前演员切场景时用户是否跟随


class BeginScene(Struct):
    """角色开场设置。

    - scene: 初始场景 id，交给 SceneManager 设为会话起始场景（含完整环境描述与持久化）。
    - action: 开场时角色正在做的事，驱动模型以角色视角主动开口。

    兼容旧的纯字符串写法：`begin_scene: "..."` 等价于只填 action、不指定 scene。
    """

    scene: str | None = None
    action: str = ""

    @property
    def has_content(self) -> bool:
        """是否包含可用于开场的信息。"""
        return bool((self.scene and self.scene.strip()) or (self.action and self.action.strip()))


class MotivationWeightsConfig(Struct):
    """四维心情权重（角色卡 motivation_weights 节）：性格决定哪维动机更主导对话欲。

    默认值即通用人格基线；总和为 1 时 total_drive 保持 0~1 量纲，
    刻意调大某维（总和 >1）等于让这个角色整体更「话痨」，反之更「闷」。
    """

    expression_drive: float = 0.3  # 表达欲：有话想说的冲动
    emotional_charge: float = 0.35  # 情感驱动力：情绪需要出口
    relational_need: float = 0.2  # 关系需求：想拉近/回应对方
    situational_relevance: float = 0.15  # 情景相关性：此刻开口是否合时宜


class CharacterConfig(Struct):
    """角色配置"""

    name: str
    system_prompt: str
    greeting: str = ""
    begin_scene: BeginScene | None = None
    example_dialogue: list[dict[str, str]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    motivation_weights: MotivationWeightsConfig = field(default_factory=MotivationWeightsConfig)
    # 八维情绪基线（emotion.py）：角色的「平常心情」与衰减目标，如 {"happy": 0.4}
    emotion_baseline: dict[str, float] = field(default_factory=dict)


class AppConfig(Struct):
    """应用配置"""

    # 日志配置
    log_level: LogLevel = LogLevel.INFO
    log_console: bool = True
    log_file: Path | None = None

    # 调试配置：开启后才输出静默思考、内心决策、推理内容等默认隐藏信息
    debug_silent_output: bool = False

    # 事件追踪日志：开启后 EventBus 会输出每个事件的详细投递日志
    event_trace_enabled: bool = False

    # 子配置
    model: ModelConfig = field(default_factory=ModelConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    tool: ToolConfig = field(default_factory=ToolConfig)
    scene: SceneConfig = field(default_factory=SceneConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    think_engine: ThinkEngineConfig = field(default_factory=ThinkEngineConfig)
    initiative_timer: InitiativeTimerConfig = field(default_factory=InitiativeTimerConfig)
    repeat_guard: RepeatGuardConfig = field(default_factory=RepeatGuardConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    resource_control: ResourceControlConfig = field(default_factory=ResourceControlConfig)
    world: WorldConfig = field(default_factory=WorldConfig)

    # 角色开场模式：True=模型主动（场景开场），False=用户主动（静态greeting）
    begin_scene: bool = True

    # 角色
    character: CharacterConfig | None = None
    character_file: Path | None = None

    def __post_init__(self):
        # 确保保存路径存在
        if self.session.save_path:
            self.session.save_path.mkdir(parents=True, exist_ok=True)

        # 应用日志配置
        self._apply_logging_config()

    def _apply_logging_config(self) -> None:
        """应用日志配置"""
        setup_logging(
            log_level=self.log_level.value,
            log_console=self.log_console,
            log_file=self.log_file,
        )
