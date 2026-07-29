# GensokyoWorld 多角色智能导演实现计划

## Context

现有 GensokyoAI 已具备单角色扮演所需的核心能力：`Agent` 演员、独立会话与记忆、`SceneManager` 场景、工具调用、`ThinkEngine` / `InitiativeTimer` 主动对话、Runtime RPC 和流式前端边界。目标是完整实现 [docs/multi_character_design.md](d:/python_play/GensokyoAI/docs/multi_character_design.md) 中的 `GensokyoWorld`：不是按顺序轮流说话的 group chat，而是由 `Director` 根据剧情、在场角色与戏剧时机决定 `continue / switch / wait_user`，用一个模型扮演整台戏。

代码勘察发现草案需修正一处关键假设：多个 `Agent` **不能直接共享同一个 EventBus**。现有 `ActionPlanner` 都订阅 `MESSAGE_RECEIVED` 且没有 actor 过滤；memory/scene 工具也使用模块级 EventBus，直接共享会串台。最终架构因此采用：

- **共享一个 `ModelClient`**（单模型、统一限流与 Provider 连接）；
- **每个 Actor 独立 EventBus / SessionManager / WorkingMemory / SemanticMemory / ThinkEngine**（避免串台，保证私有状态）；
- **World 自有 EventBus + 串行调度锁**（统一导演、舞台、主动发言和前端事件）；
- **主动定时器归属于“对话主循环”而非固定归属于 Agent**：单角色模式由 Agent 充当主循环，多角色模式由 GensokyoWorld 充当主循环；
- 使用显式 actor-aware 工具执行上下文，移除多 Agent 对模块级全局 EventBus 的依赖。

旧的单角色 `Agent`、`agent.*` / `scene.*` RPC、角色卡和会话格式必须保持兼容；`world.enabled` 默认关闭。

### 核心设计原则（贯穿全程，勿违背）

> 洞察：概念上「单角色 = 只有一个角色的 World」。但**行为层不能直接合并**——否则单角色会为多角色机制白白付费。

1. **Actor 与模式无关**（✅ 已在 1a/1b 落地）：同一个 `Agent` 既能独立跑，也能被 World 注入依赖当演员。这是唯一真正共享的东西。
2. **单角色永不为多角色机制付费**：单 actor 场景**不启用 Director**（导演对 1 个角色只能 continue/wait_user，白白多一次 LLM 往返、翻倍 token），**不走 SharedTranscript/记忆投影**那套更绕的数据流，**不进 World 状态机**。
3. **编排层做成共享抽象，而非两份拷贝**：单角色 Agent 是主循环、多角色 World 是主循环，但主动定时器/回合机制通过 §7 的 `DialogueLoop` 协议共享，避免复制粘贴——这才是消除「又搞单角色又搞多角色」重复感的正解。
4. 已发布的 `agent.*` / `scene.*` / CLI / session 格式保持不动，World 是其**之上**的增量层。
5. **对话的真相源是 Agent，不是 World**（用户 2026-07-16 决策，落法一）：working memory / session / 语义记忆继续挂在 `Agent` 上；World 只在其上叠加 SharedTranscript、WorldStage、记忆投影等**多角色专属**编排状态。**明确排除「落法二」**——不把对话数据模型从 Agent 搬进 World（那是大手术、动已发布路径、单人零收益）。World 记忆隔离通过给 Actor 的 `Agent` 注入 world 作用域的记忆根实现（延续 1a 的 `AgentDependencies`），而非把记忆搬走。

### 实施前与交付约束（硬性）

1. 写代码前先执行 `git status`，确认工作树状态；不得覆盖用户已有改动。
2. 工作树干净时执行 `git pull --ff-only` 检查上游：
   - 有更新：先快进到最新上游，重新读取本计划涉及的关键文件并校正接线点，再开始实现；
   - 无更新：基于当前 HEAD 实现；
   - 无法 fast-forward、出现冲突、或工作树已有用户改动：停止并报告，不擅自 merge/reset/stash。
3. 实现完成后必须更新 changelog 与相关中英文文档，并运行完整验证。
4. **不执行 git commit、不执行 git push**；最终只报告改动文件、验证结果和建议提交信息，提交由用户亲自完成。

---

## 1. 基础依赖注入与工具上下文隔离

### 1.1 Agent 共享 ModelClient，但不共享 Actor EventBus

> ✅ **状态：已实现（阶段 1a）**。改动集中在 `AgentDependencies` 注入路径，单角色模式零行为变化。

已修改：
- `GensokyoAI/core/agent/composition.py`
- `GensokyoAI/core/agent/runtime_context.py`
- `GensokyoAI/core/agent/_impl.py`
- `GensokyoAI/core/agent/__init__.py`（导出 `AgentDependencies`）
- `tests/test_agent_composition.py`

已实现：
- 新增 `AgentDependencies`（`runtime_context.py`，msgspec Struct，字段 `model_client / resource_gates / actor_id / world_id` 全可空），`AgentComposition.__init__` 接受可选 `deps`；未传时保持当前自建行为。
- `EventBus`、`ToolExecutor`、Session/Memory/Scene manager 仍由每个 Actor 独立创建；`resource_gates` / `model_client` 可由 `deps` 注入共享。
- `AgentRuntimeContext` 增加 `actor_id`（默认 `SINGLE_ACTOR_ID`，`deps.actor_id` 优先，否则取 `character_name`）与 `world_id`；`Agent` 暴露 `self.actor_id` / `self.world_id`，构造支持 `dependencies=` 透传。
- 回归测试已验证：两个 Actor 的 `model_client is` 同一对象，而 `event_bus / session_manager / scene_manager / tool_executor` 均不同。

> ⚠️ 实施笔记：共享 `ModelClient` 在构造时绑定了一个 `event_bus`（`composition.py`），其 `MODEL_CALL_TIMING / MODEL_AUTH` 等模型层事件会全部汇到该 bus。多角色装配时应把共享 `ModelClient` 显式绑 **World bus**，Actor 私有 bus 不承载模型层事件。

### 1.2 用 ContextVar 替代工具模块全局状态

> ✅ **ContextVar 隔离部分已实现**（事件总线解耦在先前 commit `4f2b0a2` 落地为 `tools/tool_context.py`，阶段 1a 又将其从「仅 event_bus」升级为完整 `ToolRuntimeContext`）。
> ⏳ **状态型工具串行化部分（`parallel_safe` + `execute_batch` 串行）未做，属阶段 1b。**

实际落地文件名为 **`GensokyoAI/tools/tool_context.py`**（非计划最初写的 `tools/context.py`）。

已修改：
- `GensokyoAI/tools/tool_context.py`（已含 `ToolRuntimeContext` / `bind_tool_context` / `current_tool_context` / `current_event_bus`）
- `GensokyoAI/tools/executor.py`
- `GensokyoAI/tools/tool_builtin/memory_tool.py`、`scene.py`（从 ContextVar 读，保留 `set_event_bus` 兼容薄壳）

核心类型（已实现，字段顺序以 event_bus 为首，带默认值）：
```python
class ToolRuntimeContext(Struct):
    event_bus: EventBus | None = None
    actor_id: str = SINGLE_ACTOR_ID   # 单角色默认 "__single__"
    world_id: str | None = None
```

已实现：
- `ContextVar[ToolRuntimeContext | None]`；`ToolExecutor.execute()` 调用工具前 `bind_tool_context(...)`，finally 中 `reset(token)` 恢复。
- memory/scene 工具通过 `current_event_bus`（`get_event_bus` 别名）取总线；`set_event_bus()` 保留为遗留兼容。
- 已验证 `asyncio.gather` 为每个 Task 复制上下文，并发工具调用与多 Actor 天然隔离（`tests/test_tool_context.py`）。

**阶段 1b（✅ 已完成）**：
- `GensokyoAI/tools/base.py`：`ToolDefinition` / `tool()` 装饰器增加 `parallel_safe: bool = True`。
- `remember` / `update_memory` / `scene_switch` 标记 `parallel_safe=False`。
- `ToolExecutor.execute_batch()`：只读工具并发（`asyncio.gather`），写状态工具按调用顺序串行；结果按入参顺序对齐。`ToolRegistry.register()` 也支持 `parallel_safe`。
- `tests/test_tool_context.py` 新增 `ToolBatchParallelSafetyTests`：验证并行工具重叠执行、串行工具并发度恒为 1 且保序、混合批结果按 tool_call_id 对齐。

---

## 2. World 配置、类型与持久化格式

### 2.1 配置链

> ✅ **状态：已实现（阶段 2.1）**。11 个测试覆盖解析/校验/合并/示例文件加载。

已修改：
- ✅ `config_schema.py`（`WorldActorConfig` / `WorldDirectorConfig` / `WorldTranscriptConfig` / `WorldPersistenceConfig` / `WorldConfig`；`AppConfig.world` 字段）
- ✅ `config_loader.py`（`_dict_to_world_config` 逐层展开 actors 列表与 director/transcript/persistence 子节；`_WORLD_NESTED_KEYS`）
- ✅ `config_merge.py`（world 整节覆盖，actors 列表不逐字段合并）
- ✅ `config_validator.py`（`_validate_world_data` + actor/director/transcript 子校验；`world` 加入已知顶层字段）
- ✅ `config.py`（再导出 World 配置类）
- ✅ `config/default.yaml`（文档化的 `world:` 节，默认 `enabled: false`）
- ✅ 新增 `config/world_example.yaml`（魔理沙 + 蕾米莉亚「红魔馆」双角色可运行示例）

校验已实现：actor id 唯一、`enabled` 时至少一个 enabled actor、protagonist 为 `__user__` 或 roster id、director 枚举/范围、未知字段、Path 字段类型。**不在校验阶段读取角色文件/场景库**（留待初始化返回结构化 diagnostics）。

新增 schema：
```python
WorldActorConfig:
  id: str
  character_file: Path
  initial_scene: str | None
  enabled: bool = True

WorldDirectorConfig:
  enabled: bool = True
  temperature: float = 0.2
  max_tokens: int = 384
  max_auto_turns: int = 4
  max_same_actor_turns: int = 2
  fallback_action: Literal["wait_user", "continue"] = "wait_user"

WorldTranscriptConfig:
  context_entries: int = 24
  max_entries_per_scene: int = 500

WorldPersistenceConfig:
  enabled: bool = True
  save_path: Path = Path("./sessions/worlds")

WorldConfig:
  enabled: bool = False
  id: str = "gensokyo"
  protagonist: str = "__user__"
  user_initial_scene: str | None
  actors: list[WorldActorConfig]
  director: WorldDirectorConfig
  transcript: WorldTranscriptConfig
  persistence: WorldPersistenceConfig
  project_perspective_memories: bool = True
  user_follows_current_actor: bool = True
```

校验：actor id 唯一、至少一个 enabled actor、protagonist 必须为 `__user__` 或 roster id、文件字段为字符串/Path、范围与枚举合法；不在 config validation 阶段读取文件/场景库，初始化时返回结构化 diagnostics。

### 2.2 World 核心类型

> ✅ **状态：已实现（阶段 2.2）**。纯数据层，无外部耦合，11 个单元测试覆盖。`persistence.py` 归入 2.3 待做。

新增包：`GensokyoAI/world/`
- ✅ `types.py`（`DirectorAction` / `SpeakerKind` StrEnum、`TranscriptEntry`、`DirectorDecision`、`WorldStateSnapshot`、常量 `USER_OCCUPANT_ID`）
- ✅ `stage.py`（`WorldStage`：`move` / `move_together` / `scene_of` / `characters_in` / `visible_actor_ids`，`asyncio.Lock` 原子移动）
- ✅ `transcript.py`（`SharedTranscript`：按 scene_id 分片，`add` / `history` / `render_for_scene` / `counts` / 按场景截断上限）
- ✅ `__init__.py`（导出以上类型）
- ⏳ `persistence.py`（阶段 2.3 待做）

测试：`tests/test_world_data_layer.py` —— 在场过滤、用户跟随原子移动、100 并发移动自洽、场景分片防穿帮、system 事件渲染、history limit、超限截断。

核心类型：
- `WorldStage`: `locations: dict[occupant_id, scene_id]`，用户使用常量 `__user__`；提供 `move()`、`scene_of()`、`characters_in()`、`visible_actor_ids()`，内部用 `asyncio.Lock` 保证场景移动原子性。
- `TranscriptEntry`: id、scene_id、speaker_kind (`user|character|system`)、speaker_id/name、content、timestamp、metadata。
- `SharedTranscript`: **按 scene_id 分片**，append/render/history/trim；角色仅看到当前场景最近 N 条，共享剧本不写进 Actor 私有 working memory。
- `DirectorAction`: `continue | switch | wait_user`。
- `DirectorDecision`: action、next_actor_id、reason、confidence、fallback_applied。
- `WorldTurn` / `WorldStreamEvent`: actor identity、scene、content/chunk、director decision、turn index。
- `WorldStateSnapshot`: world/session id、roster、stage、current_actor、waiting_for_user、transcript counts、initiative queue。

### 2.3 World session 与世界内角色记忆

> ✅ **状态：已实现（阶段 2.3）**。新增独立 World 存档格式、安全读写与恢复诊断，并完成 world 作用域长期语义记忆根注入；阶段 2.3 定向测试 29 例、全量 458 passed（另有 3 subtests）全绿。

新增 `WORLD_SESSION_SCHEMA_VERSION = 1`（`core/schema_versions.py`）和独立 `WorldPersistence`：
- World 会话路径 `sessions/worlds/<sanitized-world-id>/<world-session-id>.json`，复用 `sanitize_path_id`、原子 JSON/msgspec 写法。
- 保存：world session metadata、stage locations、current actor、protagonist、按场景 transcript、director counters、World 主循环主动定时器状态。
- 支持 create/list/resume/delete/export；新格式独立 version，不修改现有单角色 session schema，不迁移旧会话。
- 已实现 msgspec JSON、原子替换、`.bak`、备份恢复、损坏文件 quarantine、format/schema/world/session 身份校验，以及缺失/新增 actor 的结构化 diagnostics。完整 World/Actor 恢复编排仍按实施顺序留到阶段 8。

**World 模式的角色长期记忆必须按世界隔离**：
```text
memory/world_<world_id>/<character_name>/
  topics.json
  ...
```
- 同一个角色在不同 world 中拥有不同人生与关系，绝不串记忆。
- 同一个 world 的多个 world session 默认延续该角色在该世界里的长期语义记忆；短期 working memory/共享 transcript 仍按 world session 隔离。
- `AgentDependencies` / `AgentRuntimeContext` 已增加显式 `semantic_memory_root` 注入，`AgentComposition` 原样透传，`Agent.semantic_memory` 按是否注入选择路径；单角色模式保持现有 `sessions/<character>/memory/<session_id>` 行为，World 模式使用上述世界分区，不通过字符串拼接偷改 character_name。
- World bundle 保存角色私有 session/working-state 引用；恢复时校验 roster 与角色卡。缺失角色返回 diagnostics，可选择禁用缺失 actor，而不是静默串角色。

---

## 3. Agent 的 World-turn 桥接（不污染私有 working memory）

> ✅ **状态：已实现（阶段 3）**。定向 5 例（`tests/test_world_turn_bridge.py`）+ 全量 `463 passed, 3 subtests passed`，ruff / format / pyright 全绿；单角色路径零行为变化。
>
> 落地要点：
> - `Agent.send_world_turn(_stream)(trigger_text, system_contexts, *, record_trigger=False)`：trigger 默认不入私有 working memory（`record_in_working_memory=False` 经 MESSAGE_RECEIVED 透传，`CoreListeners` 跳过写入）；Actor 自己生成的回复照常写入；world 回合的 `discard_initiative_timer` 以 `source="world"` 调用，不重置连续主动计数。
> - **事件链修复**：`system_contexts` 与 `world_turn` 现经 ACTION_DECIDED → GENERATE_RESPONSE 全程透传（此前在链中被静默丢弃——单角色 `send` 的 system_contexts 同样受影响，已一并修复）。
> - 工具 continuation 保留本轮 contexts：`build_continuation(system_contexts=None)` + `process_stream(continuation_contexts=...)`；World 回合注入，单角色不注入（行为不变）。
> - **顺带修复流尾丢失**：`response_future` 完成时排空 `get_chunk_task` 结果与队列残余 chunk；`complete_response` 不再提前清空/置空流式队列（该队列随下次 `prepare_response` 整体替换）。

修改：
- `GensokyoAI/core/agent/_impl.py`
- `GensokyoAI/core/agent/action_planner.py`
- `GensokyoAI/core/event_listeners.py`
- `GensokyoAI/core/agent/message_builder.py`
- `GensokyoAI/core/agent/response_handler.py`

新增 World 专用调用入口（命名可按周边风格）：
```python
Agent.send_world_turn(trigger_text, system_contexts, *, record_trigger=False)
Agent.send_world_turn_stream(...)
```

实现：
- `_publish_message_received()` 增加 metadata：`world_turn=True`、`actor_id`、`record_in_working_memory=False`。
- `CoreListeners.on_message_received()` 在该标志为 false 时不把共享触发文本写入 Actor 私有 working memory；ActionPlanner 仍能用 trigger_text 触发 SPEAK 与语义记忆检索。
- Actor 自己生成的回复仍写入其私有 working memory，保持角色自身延续性；World 同时把可见回复追加到当前场景的 SharedTranscript。
- World 每轮通过 `system_contexts` 注入：当前场景、在场角色、当前场景共享剧本、明确的当前演员身份、禁止替其他角色代言的规则。
- 修正工具 continuation：`MessageBuilder.build_continuation()` 必须保留本轮 world/system contexts，否则 Actor 调工具后会丢失舞台与共享剧本。

---

## 4. Director：智能选角，不是轮流

> ✅ **状态：已实现（阶段 4）**。定向 25 例（`tests/test_world_director.py`）+ 全量 `518 passed, 3 subtests passed`，ruff / format / pyright 全绿；纯增量，单角色路径零变化。
>
> 落地要点：
> - `GensokyoAI/world/director.py`：`Director.decide(DirectorContext)` 复用共享 `ModelClient.chat()` 与 ThinkEngine 的 JSON schema/解析降级模式（`response_format` 按 `STRUCTURED_OUTPUT` 能力注入、正则提取 + 自我修正重试一次）。
> - `world/types.py` 新增 `DirectorPhase`（after_user/after_actor/initiative）、`ActorBrief`（仅公开摘要的候选角色）、`DirectorContext`（每次决策前由 World 现算的快照，含候选/当前角色/共享剧本/轮数计数/待表达意图）。
> - 硬熔断不调模型（省 token）：空候选、`auto_turn_count >= max_auto_turns` 直接 `wait_user`。
> - 校验降级：`switch` 目标必须在候选列表内、非当前角色、非用户；`continue` 要求当前角色在场且未达 `max_same_actor_turns`；非法决策按 `fallback_action` 降级（fallback=continue 也需自身合法，否则 wait_user）；JSON 失败/异常 → `wait_user`。绝不抛出、绝不死循环。
> - prompt 中显式告知当前被禁止的动作（如连发达上限不允许 continue），减少非法输出与重试。
> - 每次决策发布 `SystemEvent.WORLD_DIRECTOR_DECISION`（新增事件类型，data 含 phase/action/next/reason/fallback/计数），debug 可见 reason；事件发布失败不影响决策返回。

新增：
- `GensokyoAI/world/director.py`

Director 复用共享 `ModelClient.chat()` 和现有 ThinkEngine 的 JSON schema/解析降级模式。

输入：
- phase：`after_user | after_actor | initiative`
- 当前场景与环境
- 当前在场 actor（id、显示名、角色简介/metadata，不注入完整私有 prompt）
- 当前 actor
- 当前场景最近 shared transcript
- 连续自动发言计数、同角色连续发言计数
- 当前对话主循环的 initiative timer 状态与待表达世界意图

输出严格 schema：
```json
{
  "action": "continue|switch|wait_user",
  "next_character": "actor_id|null",
  "reason": "...",
  "confidence": 0.0
}
```

验证/降级：
- `switch` 目标必须 enabled、在用户当前场景、且不是用户；否则拒绝并按 config fallback。
- `continue` 必须有 current actor 且其仍在场。
- 达到 `max_auto_turns` 必须强制 `wait_user`；达到 `max_same_actor_turns` 不允许 continue。
- JSON 解析失败、模型超时、空 roster → `wait_user`，绝不死循环。
- 每次选择发布 World 事件，debug 模式可见 reason，正常用户只看到演出。

演员尾信号优化不作为正确性的依赖：先实现可靠的独立 Director 调用；再增加可选 `director.strategy="separate|actor_hint"`。`actor_hint` 只提供建议，World 仍校验，解析失败自动回退独立 Director；隐藏信号不得泄漏到流式正文。

---

## 5. GensokyoWorld 主类与状态机

> ✅ **状态：主类与状态机已实现（阶段 5）**。定向 10 例（`tests/test_world_main.py`）+ 桥接补充 1 例 + 全量 `552 passed, 3 subtests passed`，ruff / format / pyright 全绿。
>
> 落地要点：
> - `GensokyoAI/world/world.py`：`GensokyoWorld.create(config)` 装配（共享 ModelClient 显式绑 World 总线 + 共享 gates；每 Actor 独立 Agent——`setup_signal_handlers=False`、`manage_initiative_timer=False`、world 记忆根注入；装配期硬校验角色文件存在性与**净化后角色名唯一性**（记忆根碰撞）并抛 `WorldAssemblyError(diagnostics)`）；初始舞台按 begin_scene.scene > initial_scene > user_initial_scene > default_scene > 合成场景 `world_default` 布置。
> - 开场：protagonist 是角色→以 begin_scene.action（或通用开场提示）主动开场并进导演调度；是 `__user__`→只布置舞台等用户。
> - 用户回合：turn lock 串行；用户消息入当前场景剧本 → Director after_user → 演员回合（`send_world_turn_stream` 注入场景/在场/共享剧本/身份禁代言）→ 正文入剧本 → Director after_actor 循环至 wait_user/熔断；`current_actor_id` 回合开始即登记（用户跟随依赖）。
> - 场景联动：订阅各 Actor 总线 `SCENE_SWITCHED`（阶段 3 载荷已含 from_scene_id/actor_id）→ 更新 WorldStage；当前演员移动且 `user_follows_current_actor` 时用户原子跟随；目的地场景写入公开过渡事件；广播 `WORLD_SCENE_MOVED`。
> - `world/events.py`：`WorldActorTurnPayload` / `WorldSceneMovedPayload` 载荷类型；`SystemEvent` 增补 `WORLD_STARTED/SHUTDOWN/ACTOR_TURN_STARTED/CHUNK/COMPLETED/SCENE_MOVED/WAITING_USER`（事件名已对齐 §8.1 协议）。流式接口产出 `world.actor.*`/`world.waiting_user` 事件序列。
> - **连带修复（集成才发现）**：world-turn 触发文本此前不到达模型——`build()` 依赖工作记忆提供当前输入，而触发默认不入私历。现 `MessageBuilder.build(ephemeral_input=...)` 与 `build_continuation(ephemeral_input=...)` 以临时 user 消息注入本轮触发（单角色路径调用形态与行为零变化）。
> - ⏳ 未做：`memory_projector.py`（阶段 6）、`initiative.py` 与 Actor 定时器归属切换（阶段 7，本阶段已用 `manage_initiative_timer=False` 先行关闭 Actor 各自定时器）、完整恢复编排（阶段 8，当前每回合/移动后保存舞台状态到 World 存档）。

新增：
- `GensokyoAI/world/world.py`
- `GensokyoAI/world/events.py`（或扩展 `SystemEvent`，推荐 World 自有枚举/载荷再桥接 Runtime）
- `GensokyoAI/world/memory_projector.py`
- `GensokyoAI/world/initiative.py`

### 5.1 初始化

`GensokyoWorld.create(config)`：
1. 创建 World resource gates、共享 ModelClient、World EventBus、WorldPersistence。
2. 加载共享 Scene library（复用 `SceneManager.load_library/get_scene/render_scene_with_options`，但不使用其单一 current_scene 作为多角色真相源）。
3. 为每个 actor 加载角色卡，创建独立 Agent（共享 ModelClient，独立 EventBus/Session/Memory）。
4. 创建或恢复各 Actor 私有 session；把 actor_id → Agent 注册到 roster。
5. 初始化 WorldStage（actor 与 `__user__` 位置）、SharedTranscript、Director，以及归属于 World 对话主循环的 InitiativeTimer。
6. 订阅每个 Actor EventBus 的 `SCENE_SWITCHED`；订阅回调通过闭包绑定 actor_id，更新 WorldStage 并桥接到 World EventBus。Actor 自身不启动独立主动定时器，避免多个角色各自抢占世界主循环。

### 5.2 开场

- protagonist 是 actor id：将用户放到该 actor 的 begin_scene/initial_scene，调用该 Actor 的 world-turn 入口，以 begin_scene.action 主动开场；追加 shared transcript；Director 决定继续、切人或等用户。
- protagonist 是 `__user__`：只布置 stage，进入 `waiting_for_user=True`，不生成虚假欢迎词。
- begin_scene.scene > actor initial_scene > world user_initial_scene > scene.default_scene；冲突时产生日志 diagnostics。

### 5.3 用户回合

`world.send_message(user_input)` / stream：
1. 取得 world turn lock；用户消息优先于 World 主循环的主动定时器，取消或重新规划尚未触发的世界主动意图。
2. 把用户消息追加到用户当前场景 transcript。
3. Director `after_user` 从**同场 enabled actors**中选择首个响应者（无合适角色可 wait_user）。
4. 选中 Actor 用 private memory + scene + shared transcript 生成回复；正文标注 actor id/name 流给前端。
5. 回复追加 shared transcript；调用 Director `after_actor`。
6. 按 decision 循环 continue/switch；达到边界或 wait_user 结束，释放锁。

### 5.4 场景切换

- Actor 的 `scene_switch` 仍走其独立 SceneServiceListener，World 监听其 `SCENE_SWITCHED`：更新 `WorldStage[actor_id]`；若 actor 是当前演员且 `user_follows_current_actor=True`，原子移动 `__user__`。
- World 广播带 `actor_id/from_scene/to_scene/user_moved` 的事件；Runtime/console 使用 World 事件，而非猜单 Agent current scene。
- Director 每次决策前重新计算同场 roster，移动后绝不选择已离场角色。
- 前端可通过 `world.move` 明确移动用户或角色；权限/参数校验在 World 层完成。

---

## 6. 共享剧本与私有记忆数据流

> ✅ **状态：私有记忆投影已实现（阶段 6）**。定向 6 例（`tests/test_world_projector.py`）+ 全量 `558 passed, 3 subtests passed`，ruff / format / pyright 全绿。
>
> 落地要点：
> - `GensokyoAI/world/memory_projector.py`：`WorldMemoryProjector.project()` 一次批量结构化模型调用（复用 ThinkEngine 的 JSON schema/降级模式，单次调用不重试），为在场角色各生成 `PerspectiveMemory`（summary/importance/emotional_valence/topic_name）；只保留在场角色有效条目（模型幻觉出的不在场者会被校验丢弃）；失败回退**确定性公开事实摘要**（importance 0.3），绝不阻塞用户回复。
> - World 集成：段落结束（wait_user）即后台 `create_task` 投影；按场景游标只投影新增剧本；参与者 = 本段发言者 ∪ 当前在场角色；逐 Actor 调 `semantic_memory.add_async()`（`topic_name` 走「AI 指定话题」路径，避免话题打分的额外 LLM 调用；单 Actor 失败仅记日志）；`project_perspective_memories=False` 时完全停用；`flush_projections()` 供关机/测试等待落笔，shutdown 先 flush 再保存。
> - 投影只在用户当前场景进行；场景切换产生的公开过渡事件随下一段一并投影。

### SharedTranscript
- 只记录舞台上可被看到/听到的用户与角色正文、公开动作、公开场景事件。
- 不记录 Director reason、模型 reasoning、ThinkEngine 内心思考、私有记忆工具结果。
- 按场景分片，从第一版即防穿帮；角色移动后只注入新场景 transcript，必要时附一条“刚从 X 来到 Y”的公开过渡事件。

### Actor 私有记忆
- Actor 自己的回复继续进入其 working memory；shared transcript 不复制到所有 working memory。
- 新增 `WorldMemoryProjector`：在一次自动表演段落结束（wait_user）或重要场景事件后，用一次批量结构化模型调用，为当前场景参与者生成各自视角的摘要、importance、emotional_valence。
- 投影结果调用各 Actor 现有 `semantic_memory.add_async()`；失败时使用确定性的公开事实摘要，不能阻塞用户回复。
- 只给亲历/在场角色写入；不在场角色不会知道该场景发生的事。

---

## 7. 主动定时器属于“对话主循环”

> ✅ **状态：已实现（阶段 7）**。定向 22 例（`tests/test_world_initiative.py` 10 + `tests/test_drive_accumulator.py` 12）+ 全量 `579 passed, 3 subtests passed`，ruff / format / pyright 全绿。
>
> 落地要点：
> - `core/dialogue_loop.py`：`DialogueLoop` Protocol + `InitiativePlan`（只存意图摘要，不存话术）。
> - `core/initiative_scheduler.py`：纯调度器（替换式计划、代际守卫、fire 时效校验、事件发布），编排层共享抽象，不含任何 LLM/角色逻辑。
> - `world/initiative.py`：World 主循环——段落结束统一做一次世界级规划（一次 LLM），全世界只有一个定时器；到点持回合锁后由 Director phase=initiative 基于**触发当下**的场景与在场角色选角，无人适合则放弃；用户发言取消旧计划并在该轮结束后重规划。
> - **§7.3 对话欲（按用户 2026-07-29 决策实现）**：短期思考接入四维动机评估——`ThinkEngine.decide_drive_initiative()` 一次 LLM 同时输出四维动机画像与调度决策（注入当前对话欲/心情状态，动机四维回灌累积器）；`core/agent/drive_accumulator.py` 纯算术累积（每轮基础增量 + 动机增益 + 情感尖峰 + 场景匹配，沉默低权重），心情非对称半衰期衰减（正快负慢），发言后泄压，`session.metadata["initiative_drive"]` 持久化。`initiative_timer.drive_enabled` 开启，默认关。
> - **强制 fallback 链已删除**（用户 2026-07-29 决策）：`fallback_on_no_schedule` 等 4 个配置键从 schema 移除；旧配置键由 loader 静默丢弃 + validator `DEPRECATED_FIELDS` 迁移警告（不报错）；AI 决定不发言即不发言。事件 payload 移除 `fallback_on_no_schedule`/`is_fallback`，`source` 新增 `"drive"`。
> - **审查修复**（§5.6 定时器两条）：`trigger()` 不再持锁跨整个 LLM 生成；触发与回合生成经 `_request_semaphore` 互斥 + fire 时效校验，中途到点不再并发生成写乱私历。
> - World 对话欲化（世界级 plan 换 drive）留作后续评估项；当前 World 规划已是每段一次而非每轮一次，成本可接受。

### 7.1 抽象边界

新增对话主循环协议（建议 `GensokyoAI/core/dialogue_loop.py`）：
```python
class DialogueLoop(Protocol):
    async def plan_initiative_after_turn(...) -> InitiativePlan | None: ...
    async def trigger_initiative(plan: InitiativePlan) -> Any: ...
    async def cancel_initiative(reason: str) -> bool: ...
```

主动定时器从“Agent 的固有部件”提升为“当前对话主循环的调度器”：
- **单角色模式**：`Agent` 是对话主循环；行为与当前版本一致，由该角色决定并生成主动发言。
- **多角色模式**：`GensokyoWorld` 是对话主循环；整个世界只有一个主动定时器。Actor 是演员，不各自占有主循环，也不各自创建 timer。

修改：
- `GensokyoAI/core/agent/initiative_timer.py`：提取可复用的纯调度器/状态机，使其依赖 `plan_callback` 与 `trigger_callback`，而不是绑定单个 Agent/角色。
- `GensokyoAI/core/agent/_impl.py`：实现单角色 DialogueLoop 适配器；增加 `manage_initiative_timer: bool=True`，World Actor 设为 false。
- 新增 `GensokyoAI/world/initiative.py`：实现 World DialogueLoop 的计划与触发逻辑。

### 7.3 对话欲累积替代强制调度（用户 2026-07-28 决策，随阶段 7 一并实施）

> ⚠️ **2026-07-30 用户三次澄清后定稿重构（已实施）**：累积器方案整体废弃，改为——**ThinkEngine 四维心情模型打分（一次短 JSON LLM）→ `total_drive` 超 `drive_threshold`（默认 0.6）即主动发言，否则沉默**。LLM 只负责打分与候选内容（四维 + 候选发言 + 建议延迟 + 热情度），「说不说」由代码按阈值独立判定；无累积器（`DriveAccumulator` 已删）、无心情半衰期、无犹豫链（`hesitation_*` 与 RPC 已退役）、无强制 fallback；ThinkEngine 是决策区（`evaluate_speaking_drive()` 唯一入口），ActionPlanner 只执行 SPEAK/WAIT。以下为阶段 7 时的原始设计记录，其中「累积器」部分已被上述定稿取代。

把「每轮结束问一次模型要不要安排」改为「驱动力累积到阈值才主动」，`drive` 累积器作为 `plan_callback` 实现，一次重构完成：

- **废除强制**：`InitiativeTimerConfig.fallback_on_no_schedule` 的语义（模型不想说也强行安排 300s 后发言）与本模型冲突，上线后应默认关闭/移除——违背角色意愿是当前设计的核心问题。
- **复用已有动机量化**：`motivation_evaluator.py` 的 `MotivationProfile` 四维（表达欲 / 情感驱动力 / 关系需求 / 情景相关性 → `total_drive`）即对话欲；现仅接在 ActionPlanner ← ThinkEngine 随机游走路径，需接入定时器路径，并删除定时器自有的那次 LLM `_decide` 往返。
- **省 token**：累积为纯算术，仅在跨阈值时调用一次 LLM 生成意图摘要；多轮不想说话即零成本。
- **增量以事件为主**：话题未聊完/被打断、情感强度尖峰、场景与挂心话题匹配为主要来源；沉默时长权重压低，否则退化为「伪装的固定间隔定时器」。说完话后表达欲部分泄压。
- **心情非对称衰减**：正面情绪半衰期短、负面情绪半衰期长但仍衰减（享乐适应），按 valence 分别配置。
- **必守**：`drive` / mood 需随会话持久化，否则重启即人格重置；保留「存意图摘要、不存话术」——阈值只决定「此刻有话想说」，说什么仍到点再生成。

### 7.2 World 主循环主动计划

每次一个完整 World 自动表演段落结束并进入 `wait_user` 后，World 统一做一次 initiative planning：
- 输入当前场景 shared transcript、在场演员状态摘要、最近 Director 决策、沉默时长策略。
- 输出 `InitiativePlan`：`should_schedule`、delay、世界级意图摘要、reason、enthusiasm；此时**不提前锁死发言角色**，因为到点时场景/在场角色可能已变化。
- 用户在定时器到期前发言时，World 主循环取消旧 plan，并在该轮表演结束后重新规划。

定时器到期：
1. 获取 world turn lock；若用户请求/表演正在进行则按配置延后，不并发抢话。
2. Director phase=`initiative` 基于**触发当下**的场景、在场角色和意图摘要，决定 `switch/continue/wait_user` 以及谁开口。
3. 选中的 Actor 通过同一个 World actor-turn 状态机生成主动消息；追加 transcript，继续 Director 调度；仍受 `max_auto_turns` / `max_same_actor_turns` 熔断。
4. Director 判断此刻无人适合说话时可 discard 或重新安排，不为“定时器到点”强行台词。

这样主动定时器承担的是“世界何时再次推动剧情”，Director 承担“到那个时机谁最适合开口”，符合多角色世界是主循环的设计理念。

---

## 8. Runtime RPC、流式协议与 Console

> ✅ **状态：已实现（阶段 9）**。定向 22 例（`tests/test_runtime_robustness.py` 4 + `tests/test_world_runtime_rpc.py` 11 + `tests/test_world_console.py` 6 + WS world 分流 1）+ 全量 `609 passed, 3 subtests passed`，ruff / format / pyright 全绿。
>
> 落地要点：
> - `RuntimeState.world` 与 agent 互斥（init 双向硬校验），绝不影响 root 租户路由判定；world.init/start/send_message[_stream]/state/roster/transcript/move/session.*/shutdown 14 个 RPC 全部经 `RPC_METHOD_SPECS` 注册并可经 `runtime.info` 发现（capability `world.orchestration`）。
> - 网络模型：`world.` 进 `_NETWORK_RESOURCE_PREFIXES` 与 `_is_tenant_method`（租户状态隔离）；`world.send_*` 进 `NETWORK_IDEMPOTENCY_METHODS`（幂等账本复用，World 存档槽位）；auth `required_role` world 分支 read/chat；WS `world.send_message_stream` 与 agent 流同等的 ack/task/cancel 支持，终帧 `world.finish` 聚合 turns。
> - World 事件入契约（8 个 `world.*` RuntimeEventSpec）与 `world` 事件分类（并入 `runtime_observable`）；事件订阅/录制经 `_runtime_event_bus()` 在 World/Agent 总线间选择；两套脱敏统一为 `sanitize_event_payload`。
> - §5.6 归档 runtime 4 条修复一并落地（WS 取消账本确定性关闭链、清理链容错、租户目录启动隔离、依赖安装线程卸载+超时钳制）。
> - console：`backends/console/world_backend.py`（**显示单一通道**：send 用非流式驱动回合，显示全部由 World 总线订阅承担——用户回合与主动剧情天然不重复）；CLI `--world`（或 `world.enabled` 自动选择）；`/world` `/roster` `/stage` `/transcript`。
> - 设计偏差记录：world 网络写不要求 `expected_revision`（World 无 session revision 概念，turn lock 已串行化）；console 放弃「send 迭代 stream」改为单通道 bus 显示（消除 stream/bus 双通道重复）。

### 8.1 Runtime

修改：
- `GensokyoAI/runtime/service.py`
- `GensokyoAI/runtime/rpc.py`
- `GensokyoAI/runtime/event_contract.py`
- `GensokyoAI/backends/web_server/http_adapter.py`
- `bridge_main.py`（若只走通用 dispatch 无需专改）

`RuntimeState` 新增 `world: GensokyoWorld | None`，保留 `agent`；单角色与 world 模式互斥启动但 API 同时可发现。

新增 RPC：
- `world.init`
- `world.start`
- `world.send_message`
- `world.send_message_stream`
- `world.state`
- `world.roster`
- `world.transcript`
- `world.move`
- `world.session.create/list/resume/delete/export`
- `world.shutdown`

`runtime.info` 新增 capability `world.orchestration`、methods/specs；协议仅增量，不改 major。

流式事件必须包含：
```json
{"type":"world.actor.started","actor_id":"marisa","actor_name":"雾雨魔理沙","scene_id":"..."}
{"type":"world.actor.chunk","actor_id":"marisa","content":"..."}
{"type":"world.actor.completed",...}
{"type":"world.director.decision","action":"switch","next_actor_id":"patchouli"}
{"type":"world.waiting_user"}
```
WebSocket 为 `world.send_message_stream` 增加与 agent stream 同等的 task/cancel/backpressure 支持。

### 8.2 Console

新增：
- `GensokyoAI/backends/console/world_backend.py`
- CLI 增加 `--world`（或 config world.enabled 自动选择）

行为：
- 动态显示当前发言者 `魔理沙:` / `帕秋莉:`，不再用固定 `_character_name`。
- 显示 World 场景移动、Director 切人（正常模式不显示内部 reason）、主动发言和等待用户状态。
- 复用 Rich 样式、命令系统；新增 `/world`、`/roster`、`/stage`、`/transcript`，单角色 console 不变。

---

## 9. 实施顺序（每阶段可独立验证，最终一次性交付完整功能）

0. **同步上游**：`git status` → 工作树干净后 `git pull --ff-only`；如有更新，基于最新代码重新核对全部接线点；不 commit/push。
1. **隔离基础**：
   - ✅ **1a（已完成）**：`AgentDependencies` 共享 ModelClient/gates 注入 + `ToolRuntimeContext` ContextVar（actor_id/world_id）+ Actor 身份暴露；单角色全回归绿。
   - ✅ **1b（已完成）**：状态型工具 `parallel_safe` 元数据 + `execute_batch` 对同一 Actor 状态型工具串行、只读工具并发。
2. **World 数据层**：配置、types、WorldStage、scene-partitioned SharedTranscript、WorldPersistence。
3. **Actor bridge**：world-turn 调用、trigger 不入私有 memory、tool continuation 保留 world contexts。
4. **Director 与主状态机**：✅ Director（阶段 4）+ ✅ 主状态机/开场/用户回合（阶段 5，见 §5 状态标注）。
5. **场景联动**：✅ 已完成（阶段 5）：Actor scene_switch → WorldStage + 用户跟随 + 在场过滤 + 公开过渡事件。
6. **私有记忆投影**：✅ 已完成（阶段 6）：各视角摘要批量生成与后台写入，见 §6 状态标注。
7. **主循环主动定时器**：✅ 已完成（阶段 7）：DialogueLoop 协议、纯调度器、World 主循环计划/触发、§7.3 对话欲（四维动机接入短期思考）、强制 fallback 链删除，见 §7 状态标注。
8. **持久化恢复**：✅ 已完成（阶段 8）：`GensokyoWorld.resume(config, session_id)` 恢复编排——存档舞台/共享剧本分片/各 actor 私有会话还原（缺失降级新建 + warning 诊断，绝不静默串角色），roster 差异经 `world.resume_diagnostics` 呈现，原存档作为活动存档续写，投影游标跳过上一次已投影剧本。审查修复（§5.6 归档 4 条）一并落地：`create_async` per-key 锁消除 create TOCTOU；`list()` docstring 补自愈副作用；roster diagnostics 并入 stage 键（除 `__user__`）与 `current_actor_id`（幽灵占位不再漏诊）；`core/migrations` 对更高未知 schema_version 不再静默降级改写版本（session/memory 两路），与 world/persistence 硬拒绝契约对齐为「不静默降级」。export/delete/list 仍由 `WorldPersistence` 提供（阶段 2.3 已测），阶段 9 接 RPC。
9. **Runtime / WebSocket / Console**：✅ 已完成（阶段 9）：world.* RPC（14 个）、WS 流式 actor 事件（`world.finish` 终帧聚合）、console `world_backend.py` + `--world` + `/world` `/roster` `/stage` `/transcript`，见 §8 状态标注。§5.6 归档 runtime 4 条（WS 取消账本、清理链、租户启动隔离、依赖安装阻塞）与 world.\* 接线清单全部落地。
10. **文档与完整验收**：✅ 已完成（阶段 10）：README 中英多角色章节、QUICKSTART World 快速上手、runtime_api「World 多角色编排 API」章节（协议 `2.1.0`）、`multi_character_design.md` 草案状态转已实现、changelog `v2026.7.30.0`（package 版本同步 bump、`world session schema v1` 入表）、default/world example 配置核对（阶段 2.1 已就绪）。

---

## 10. 测试矩阵与验收

### 单元测试
- WorldStage：移动、同场过滤、用户跟随、并发原子性。
- SharedTranscript：按场景隔离、限制条数、渲染 speaker、私有字段不泄漏。
- Director：合法 continue/switch/wait、非法/离场 actor 降级、JSON 失败、超时、自动轮数熔断。
- ToolRuntimeContext：两个 Actor 并发工具调用命中各自 EventBus；ContextVar 恢复；状态工具串行。
- WorldPersistence：round-trip、损坏文件、路径净化、版本/缺失 actor diagnostics。
- World memory namespace：同 world 跨 session 延续、不同 world 同角色完全隔离；路径严格为 `memory/world_<world_id>/<character_name>`（均净化）。
- DialogueLoop/InitiativeTimer：单角色兼容原行为；World Actor 不创建 timer；整个 World 只有一个 timer；用户输入取消并重规划；到点后才由 Director 选角。

### 集成测试（fake Provider，可脚本化决策）
1. **红魔馆偷书完整戏**：魔理沙开场 → 移动红魔馆 → 用户问主人 → Director 在合适时机 switch 帕秋莉 → 帕秋莉看到共享剧本并以自己人设接话。
2. 证明不是 round-robin：Director 连续选择当前角色、跳过某角色、wait_user 均可；顺序由剧情决策而非 roster 顺序。
3. 不在场角色绝不被选中；移动后可被选中。
4. 魔理沙/帕秋莉共享同一个 ModelClient，但私有 memory/session 不互相可见。
5. 工具调用后仍保留共享剧本与当前场景。
6. `scene_switch` 更新 Actor + 用户位置并广播正确 world event。
7. protagonist actor 主动开场；protagonist=`__user__` 不自动说话。
8. World 主循环 timer 到期后才由 Director 从当下在场角色中选角；用户先发言会取消旧计划；Actor 无独立 timer，因此不存在多个角色 timer 抢话。
9. 保存并恢复后 roster、stage、transcript、current actor、World timer、actor sessions 一致；同 world 角色长期记忆延续，不同 world 不串。

### Runtime / E2E
- `runtime.info` 声明全部 world methods/capability；结构化错误稳定。
- HTTP JSON RPC、WebSocket world stream、cancel stream、事件订阅均覆盖。
- Console fake provider smoke test验证动态角色名前缀与切换。
- 旧 `agent.*`、`scene.*`、单角色 CLI 全回归。

### 最终命令
- 先跑 world 定向 tests。
- 执行项目标准 `./normalize_code.cmd`（ruff format、ruff check、pyright、pytest）。
- 实际用两个角色卡 + 测试 Provider 驱动一段红魔馆对话，观察流式 actor 事件、Director decision、场景切换、持久化文件。
- 如用户配置了真实模型，再运行一次可选真实 E2E；真实 API 失败不影响离线自动测试结论。

---

## 10.5 记忆层简化（用户 2026-07-28 决策，World 落地后实施）

用户调研：主流用法为「开会话 → 扮演 → 删掉重开」的快节奏短会话。据此对外简化为**两层记忆**：

- **保留**：工作记忆（短期）+ 话题记忆（长期，AI 总结话题 + 情感标注）。
- **降级/砍除**：episodic 压缩层（短会话下压缩阈值几乎触发不到，属空转抽象）；embedding 向量检索明确为**可选增强、默认关闭**（`semantic.py` 本就是「话题感知 + 可选 embedding」，关闭即省 embedding API 成本）。
- **话题图（topic store）不可砍**：它是 `ThinkEngine` 随机游走的图、`MotivationEvaluator` 的 `emotional_valence` 来源、§7.3 心情衰减的作用对象，也是 §2.3 world 记忆隔离与 §6 `WorldMemoryProjector` 的写入目标。砍掉则对话欲模型失去输入信号，退化为纯时间驱动。
- **排期**：World 落地（至少完成阶段 6）后再动，避免与阶段 4/6 撞车；届时对多角色实际需要的长期记忆量也更有把握。
- 备注：短会话用法可能部分源于 Alpha 阶段长会话体验未打磨，非必然的内在偏好；保留话题结构可避免日后回补。

---

## 11. 防护与明确取舍

- World 全回合持有单一 `asyncio.Lock`，不允许两个用户请求/主动消息同时推进戏；状态型工具另有 stage lock。
- `max_auto_turns`、`max_same_actor_turns`、Director timeout/fallback 是硬熔断，避免演员无限互聊烧 token。
- Director 永远只看公开角色摘要与共享剧本，不读取其他 Actor 私有记忆。
- 每个 Actor 独立 EventBus 是必须调整；不照草案原文共享 EventBus。
- `SceneManager` 继续保持单 Agent 语义；多角色位置由 WorldStage 管，不破坏现有 scene.*。
- 独立 Director 调用是默认可靠路径；演员尾信号作为可选优化，不牺牲流式正文正确性。
- 不支持第一版多模型/实时并发抢话；这是草案明确的非目标，不影响“智能时机选角”的核心价值。
