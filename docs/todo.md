# GensokyoWorld 多角色功能 — 交接文档

> 给接手的 AI：本文件是**唯一入口**。先通读本文，再看 `docs/gsk-ai-multi-character.md`（完整实施计划，含每阶段文件级细节与状态标注）。
> 两份文件都在 `docs/`，随仓库一起提交，clone 即可见。

## 0. 一句话现状

多角色 `GensokyoWorld` **全部 10 个阶段已完成**，`v2026.7.30.0` 已正式发布并 push（`cd6d9a8`）。发布后完成两轮实机修正：①thinking 模型链路修复（`think: false` 显式禁用 kimi 思考、决策调用 max_tokens 下限 1024，`b2be0cd` 已 push）；②**对话欲按用户 2026-07-30 定稿重构**（ThinkEngine 四维打分 + `drive_threshold` 阈值二元判断，删除累积器/犹豫链/旧决策路径，本次未提交）。全量 `591 passed, 3 subtests passed` 全绿。

- 基线：上一轮事件总线解耦 `4f2b0a2`；用 `git log --oneline` 查看最新 HEAD。
- 当前测试：`591 passed, 3 subtests passed`，ruff check / pyright 全过。注意：`ruff format --check .` 有 2 个历史遗留未格式化文件（`GensokyoAI/runtime/media_store.py`、`tests/test_session_message_restore.py`），其余全部 format 干净。
- **对话欲最终形态见 §5.5「对话欲最终定稿」**（用户三次澄清的记录，改动前先读，别再理解错）。

---

## 1. 硬性约束（务必遵守）

1. **不要 `git commit` / `git push`**。完成后只报告改动文件、验证结果、建议 commit message，由用户亲自提交。
2. 动手前先 `git status` 确认工作树；工作树干净时 `git pull --ff-only` 检查上游，有更新则先快进并重新核对接线点。无法 ff / 有冲突 / 有用户改动 → 停止并报告，不擅自 merge/reset/stash。
3. **逐阶段推进**：一次做一个可独立验证的阶段，跑完测试再进下一个。用户在意 token 成本，别一口气堆完多个阶段。
4. 每阶段完成后更新 `docs/gsk-ai-multi-character.md` 里对应小节的状态标注。

---

## 2. 核心设计原则（贯穿全程，勿违背）

> 来自用户的关键决策，写进计划文档开头的「核心设计原则」小节，这里再强调一遍。

1. **Actor 与模式无关**（✅ 1a/1b 已落地）：同一个 `Agent` 既能独立跑，也能被 World 注入依赖当演员。这是唯一真正共享的东西。
2. **单角色永不为多角色机制付费**：单 actor 场景**不启用 Director**（导演对 1 个角色只能 continue/wait_user，白白多一次 LLM 往返、翻倍 token）、**不走 SharedTranscript/记忆投影**、**不进 World 状态机**。
3. **编排层做成共享抽象，而非两份拷贝**：单角色 Agent 是主循环、多角色 World 是主循环，但主动定时器/回合机制通过计划 §7 的 `DialogueLoop` 协议共享，不复制粘贴。
4. 已发布的 `agent.*` / `scene.*` / CLI / session 格式**保持不动**，World 是其**之上**的增量层。
5. **对话真相源是 Agent，不是 World（落法一，用户已决策）**：working memory / session / 语义记忆继续挂在 `Agent`；World 只叠加 SharedTranscript、WorldStage、记忆投影等多角色专属状态。**明确排除「落法二」**——不把对话数据模型搬进 World。World 记忆隔离靠给 Actor 的 `Agent` 注入 world 作用域记忆根实现（延续 1a 的 `AgentDependencies`），不搬记忆。

---

## 3. 已完成阶段（未提交，全绿）

### ✅ 阶段 1a — Actor 身份 + 共享 ModelClient 注入
- `GensokyoAI/core/agent/runtime_context.py`：新增 `AgentDependencies`（msgspec Struct，字段 `model_client / resource_gates / actor_id / world_id` 全可空）；`AgentRuntimeContext` 加 `actor_id`（默认 `SINGLE_ACTOR_ID`）/ `world_id`。
- `GensokyoAI/core/agent/composition.py`：`AgentComposition.__init__` 接受可选 `deps`；`build()` 中 `model_client` / `resource_gates` 优先复用注入实例，**EventBus 永远每 Actor 独立创建**；`actor_id` 默认取 `character_name`。
- `GensokyoAI/core/agent/_impl.py`：`Agent.__init__` 加 `dependencies` 参数并透传；暴露 `self.actor_id` / `self.world_id`。
- `GensokyoAI/core/agent/__init__.py`：导出 `AgentDependencies`。
- 测试：`tests/test_agent_composition.py`（两 Actor 共享 ModelClient 但 bus/session/scene 隔离等）。

### ✅ 阶段 1b — 工具并行安全 + 批量执行串/并行
- `GensokyoAI/tools/base.py`：`ToolDefinition` / `tool()` 装饰器加 `parallel_safe: bool = True`。
- `remember` / `update_memory` / `scene_switch` 标记 `parallel_safe=False`。
- `GensokyoAI/tools/executor.py`：`execute_batch()` 只读工具并发（gather）、写状态工具按调用顺序串行，结果按入参顺序对齐；`_is_parallel_safe()` 查注册表，外部/未知工具保守视为可并行。
- `GensokyoAI/tools/registry.py`：`register()` 支持 `parallel_safe`（并修复了原 `decorated.name` 潜伏 bug）。
- 测试：`tests/test_tool_context.py::ToolBatchParallelSafetyTests`。

### ✅ 阶段 2.2 — World 数据层（`GensokyoAI/world/`，纯数据无耦合）
- `types.py`：`DirectorAction` / `SpeakerKind`（StrEnum）、`TranscriptEntry`、`DirectorDecision`、`WorldStateSnapshot`、常量 `USER_OCCUPANT_ID = "__user__"`。
- `stage.py`：`WorldStage`（`move` / `move_together` / `scene_of` / `characters_in` / `visible_actor_ids`，`asyncio.Lock` 原子移动）。
- `transcript.py`：`SharedTranscript`（按 scene_id 分片，`add` / `history` / `render_for_scene` / `counts` / 按场景截断上限）。
- `__init__.py`：导出以上。
- 测试：`tests/test_world_data_layer.py`（11 例）。

### ✅ 阶段 2.1 — WorldConfig 配置链
- `config_schema.py`：`WorldActorConfig` / `WorldDirectorConfig` / `WorldTranscriptConfig` / `WorldPersistenceConfig` / `WorldConfig`；`AppConfig.world` 字段。
- `config_loader.py`：`_dict_to_world_config()` 展开 actors 列表与子节；`_WORLD_NESTED_KEYS`。
- `config_merge.py`：world 整节覆盖。
- `config_validator.py`：`_validate_world_data` + actor/director/transcript 子校验；`world` 加入 `_known_top_level_fields()`；文件顶部加了 `from pathlib import Path`。
- `config.py`：再导出 World 配置类。
- `config/default.yaml`：文档化 `world:` 节（`enabled: false`）。
- `config/world_example.yaml`：魔理沙 + 蕾米莉亚「红魔馆」双角色可运行示例（引用的角色卡真实存在）。
- 测试：`tests/test_world_config.py`（11 例）。

---

## 4. 待做阶段（从这里继续）

按计划 §9 顺序，逐阶段做、逐阶段验证。每阶段的**文件级细节在 `docs/gsk-ai-multi-character.md` 对应小节**。

- **✅ 2.3 — WorldPersistence + 按世界隔离的记忆命名空间**
  - 独立 World session schema/format、版本化 `WorldSessionRecord`、create/list/resume/delete/export 与异步包装已实现。
  - 路径净化、msgspec JSON、原子替换、`.bak`、备份恢复、quarantine、身份/版本校验与 roster diagnostics 已实现。
  - `AgentDependencies.semantic_memory_root` 注入链已实现；World 使用 `memory/world_<world_id>/<character_name>/`，单角色仍使用原有按 session 路径。
  - 定向 29 例及全量 `458 passed, 3 subtests passed`，ruff / format / pyright 全绿。
- **✅ 3 — Actor 的 world-turn 桥接**：`Agent.send_world_turn(_stream)`；trigger 文本默认不入私有 working memory（`record_in_working_memory=False`）；事件链全程透传 `system_contexts`/`world_turn`（修复旧的静默丢弃）；`MessageBuilder.build_continuation()` 保留本轮 world/system contexts；顺带修复流式尾部 chunk 丢失。定向 5 例 + 全量 463 passed 全绿，未提交。
- **✅ 4 — Director**：`world/director.py`，复用共享 `ModelClient.chat()` + ThinkEngine 的 JSON schema/降级模式。`DirectorContext` 快照输入；硬熔断（空候选/`max_auto_turns`）不调模型；`switch` 目标严格校验在场/非当前/非用户、`continue` 校验在场与 `max_same_actor_turns`，非法按 `fallback_action` 降级；解析失败重试一次后 → wait_user；prompt 显式告知被禁动作；每次决策发布 `WORLD_DIRECTOR_DECISION` 事件。定向 25 例 + 全量 518 passed 全绿，未提交。
- **✅ 5 — GensokyoWorld 主类与状态机**：`world/world.py`（create 装配：共享 ModelClient 绑 World bus、Actor 独立 Agent + 世界记忆根、净化名碰撞硬报错；初始舞台优先级链 + 合成场景兜底）、`world/events.py`（载荷类型，事件名对齐 §8.1）。开场（protagonist 角色主动开场 / `__user__` 等用户）、用户回合导演调度循环（turn lock + 熔断）、场景切换联动 WorldStage + 用户原子跟随 + 公开过渡事件。Actor 侧新增 `setup_signal_handlers=False` / `manage_initiative_timer=False`。**连带修复**：world-turn 触发文本以临时 user 消息注入生成与 continuation（此前根本不到达模型）。定向 10+1 例 + 全量 552 passed 全绿，未提交。
- **✅ 6 — 私有记忆投影**：`world/memory_projector.py`（`WorldMemoryProjector` 一次批量结构化调用生成各在场角色 `PerspectiveMemory`，校验丢弃不在场者，失败回退确定性公开摘要）；World 段落结束后台投影（按场景游标只投新增、参与者=发言者∪在场、逐 Actor `add_async` 自捕获、config 可停用、shutdown 先 flush）。定向 6 例 + 全量 558 passed 全绿，未提交。
- **✅ 7 — DialogueLoop 抽象 + 对话欲 + fallback 链删除**：`core/dialogue_loop.py`（Protocol + `InitiativePlan`）、`core/initiative_scheduler.py`（纯调度器，替换式计划 + fire 时效校验）、`world/initiative.py`（World 段落结束一次世界规划，全世界一个定时器；到点 Director initiative 按当下在场选角；用户发言取消重规划）。**§7.3 按用户 07-29 决策实现**：`ThinkEngine.decide_drive_initiative()` 一次 LLM 出四维动机 + 调度决策（注入对话欲/心情状态，动机回灌累积器）；`core/agent/drive_accumulator.py` 纯算术累积 + 心情非对称半衰期 + 泄压 + session.metadata 持久化；`drive_enabled` 默认关。**强制 fallback 链整体删除**：4 个配置键移除，旧键 loader 丢弃 + validator 迁移警告（不报错），事件 payload 移除 fallback 字段，`source` 新增 `"drive"`。**审查修复**：trigger() 不再持锁跨 LLM 生成；触发与回合经信号量互斥 + 时效校验。定向 22 例 + 全量 579 passed 全绿，未提交。
- **✅ 8 — 持久化恢复**：`GensokyoWorld.resume(config, session_id)` 恢复编排：存档舞台/共享剧本分片/各 actor 私有会话还原（缺失降级新建 + warning 诊断，绝不静默串角色），roster 差异经 `world.resume_diagnostics` 呈现，原存档作为活动存档续写，投影游标跳过旧剧本。§5.6 归档 4 条一并落地：`create_async` per-key 锁消除 TOCTOU、`list()` docstring 补自愈副作用、roster diagnostics 并入 stage 键与 current_actor（幽灵占位不漏诊）、`core/migrations` 高版本文件不再静默降级（session/memory 两路）。定向 8 例 + 全量 587 passed 全绿，未提交。
- **✅ 9 — Runtime / WebSocket / Console**：`RuntimeState.world`（与 agent 互斥、绝不影响 root 租户路由）；`world.init/start/send_message[_stream]/state/roster/transcript/move/session.create/list/resume/delete/export/shutdown` 14 个 RPC；WS 流式分流 `world.send_message_stream`（ack 先于参数校验，world.finish 终帧聚合 turns）；`runtime.info` capability `world.orchestration`。接线清单全落地：auth `required_role` world 分支（read/chat，不再 fallthrough admin）、`_NETWORK_RESOURCE_PREFIXES`+`_is_tenant_method` 加 `world.`（租户隔离）、`NETWORK_IDEMPOTENCY_METHODS` 加 world.send_*（复用幂等账本，World 存档槽位）、event_contract 加 8 个 world 事件契约 + `_runtime_event_category_map` 加 world 分类并入 runtime_observable、事件订阅/录制改从 `_runtime_event_bus()`（World 模式取 World 总线）、**两套脱敏统一为 `sanitize_event_payload`**（字段并集 + password 子串 + 统一 `[REDACTED]`）。**§5.6 归档 4 条一并落地**：WS 断连/取消落在发送窗口的幂等账本确定性收敛（`_send_streaming_rpc_frames` finally 显式 aclose + service 层 GeneratorExit 收敛 cancelled + 网络/本地两层关闭链）；`_await_cancelled_task` 抑制一切任务异常（清理链不再被心跳异常截断）；`_load_tenant_catalog` 按租户隔离损坏（单租户 operations.json 损坏只跳过该租户并 warning，不静默重建空账本）；`dependency.install` 改 `asyncio.to_thread` + timeout 钳配置上限。console：`backends/console/world_backend.py`（动态发言者前缀、**显示单一通道**——用户回合与主动剧情都只经 World 总线事件显示，turn lock 保证不交错，无 stream/bus 双份问题）、CLI `--world`（或 world.enabled 自动选择，含 --resume/--new-session/--list-sessions 的 World 存档语义）、`/world` `/roster` `/stage` `/transcript` 命令、单角色会话命令在 World 模式友好拦截。定向 22 例（robustness 4 + world_rpc 11 + world_console 6 + WS world 分流 1）+ 全量 `609 passed, 3 subtests passed` 全绿，未提交。
- **✅ 10 — 文档与完整验收**：README 中英新增「一台戏：多角色世界」亮点与 `world.*` RPC 清单；QUICKSTART 新增 World 快速上手（world_example 一键开演 + `--world` 参数表）；`docs/runtime_api.md` 新增「World 多角色编排 API」章节（14 方法/权限/WS 帧协议/事件分类），协议版本 `2.0.0`→`2.1.0`（major 2 不变，纯增量）；`docs/multi_character_design.md` 草案状态转「已实现」；changelog `v2026.7.30.0`（正式发布，中英双版）+ `pyproject.toml` bump `2026.7.30.0` + `world session schema v1` 入 schema 表；`rpc.py` `RUNTIME_PROTOCOL_VERSION` 与版本一致性测试同步。default.yaml world 节与 world_example.yaml 阶段 2.1 已就绪无需改。全量 `609 passed, 3 subtests passed` 全绿，未提交。

## 5. 关键陷阱（血泪，别踩）

- **多个 Agent 绝不能共享同一个 EventBus**：`ActionPlanner` / `CoreListeners` / `MetricsListeners` 都订阅 `MESSAGE_RECEIVED` 且无 actor 过滤，共享必串台。这是整个隔离架构的根基。共享的只有 ModelClient / resource_gates。
- **共享 ModelClient 的事件归属**：`ModelClient` 构造时绑定一个 `event_bus`，其 `MODEL_CALL_TIMING` / `MODEL_AUTH` 等会全汇到该 bus。多角色装配时应把共享 ModelClient 显式绑 **World bus**，Actor 私有 bus 不承载模型层事件。
- **工具事件总线按调用注入**：内置工具（memory/scene）经 `GensokyoAI/tools/tool_context.py` 的 `ContextVar` 读事件总线，`ToolExecutor.execute()` 里 `bind_tool_context(...)` 注入。若在 ToolExecutor 之外裸调用工具（如测试），必须 `with bind_event_bus(bus):` 或 `bind_tool_context(...)` 包裹，否则取不到总线。详见 `tools/tool_context.py` 顶部 docstring。
- **配置校验阶段不读文件/场景库**：actor character_file、scene id 的存在性留到初始化阶段返回结构化 diagnostics，别在 `config_validator` 里读盘。
- **msgspec Struct 字段顺序**：无默认值字段必须在有默认值字段之前。

## 5.5 老手笔记（前任 AI 的经验，非计划内容但能少踩坑）

**协作方式（用户明确要求）**
- **不要用多个 subagent 并行改代码**：会互相抢任务、产生竞态。子代理只用于只读调研/审查；修改一律主会话单线程顺序做。
- 用户在意 token 成本：一次做一个阶段，跑完验证再进下一个；探索优先直接读文件，别撒一堆代理。
- 全中文交流与文档；commit **不加 Co-Authored-By**；提交前先报告改动，由用户决定何时提交。

**已修过的隐蔽 bug（别改回去）**
- `ActionExecutor.complete_response()` 曾顺手清空流式队列，导致流尾最后一个 chunk 必丢（真模型下被网络时序掩盖，假 Provider 下才现形）。现在它只解析 future，队列由下次 `prepare_response()` 整体替换；`_send_stream_impl` 退出前会排空 `get_chunk_task` 结果与队列残余。
- 事件链 `MESSAGE_RECEIVED → ACTION_DECIDED → GENERATE_RESPONSE` 曾两跳都不转发 `system_contexts`，console 的 `<attention>` 注入与 RPC 的 `system_contexts` 参数从来没到过模型。阶段 3 已修，新增字段务必确认全链透传。
- **World console 显示曾走 stream/bus 双通道**：send 迭代 `send_message_stream` 显示一份、World 总线订阅又显示一份，`_in_user_turn` 时间标志挡不住异步调度的事件（回调落地时标志已清）。阶段 9 定为**单一通道**：send 用非流式 `world.send_message` 驱动回合，显示全部由 World 总线订阅承担（turn lock 保证用户回合与主动剧情不交错）。别再给 console 加回 stream 迭代显示。

**代码规范（用户 2026-07-30 明确要求）**
- **`build_*` 工具函数统一放 `utils/`**，且注意功能相关（不是安全工具别进 `path_security.py`，通用辅助进 `helpers.py`）；**不要每个小功能拆一个模块/文件**（`world/memory_paths.py` 这类单函数小模块已被点名并清理）。
- World 存储布局（用户定稿）：`sessions/world/<world_id>/` 是唯一命名空间——World 存档（`<session>.json`）、actor 私有会话（`<角色名>/`）、语义记忆（`memory/<角色名>/`）同树管理；单角色 `sessions/<角色名>/` 不受影响。

**代码风格既成事实**
- 已使用 Python 3.14 的 PEP 758 语法（无括号 `except A, B:`）。项目硬要求 3.14+，用旧版本跑会直接语法错误——不是代码写错了。
- `_impl.py` 已做过一轮瘦身：定时器编排在 `initiative_coordinator.py`、角色扮演框架 prompt 在 `prompts.py`、send 四兄弟收敛为 `_send_impl` / `_send_stream_impl`。往 Agent 加东西前先看这几处有没有合适的落点。
- `model_client.py`（约 1200 行）是已知待拆项，下一刀建议先抽遥测/事件发布块（约 180 行，低风险）。**embeddings 那段与 retry/telemetry 耦合较深，不要在 World 阶段中途动。**

**动机权重 = 性格（用户 2026-07-28 提出，尚未实施，实施前先读这几条）**
- 想法：`MotivationProfile.total_drive` 现在是硬编码加权（`expression_drive * 0.3` 那串字面量），改为从角色卡读 `motivation.base_weights`，让四维配比定义性格（例：妖梦性子直 → `expression_drive` 高、`situational_relevance` 低）。搭 §7.3 的累积模型后，权重决定驱动力涨多快，性格直接变成节奏。这部分成本低、可独立于 §7.3 先做。
- **必须归一化**：权重和不为 1 会让 `total_drive` 量纲随角色漂移，阈值失去跨角色可比性。选定「归一化到和为 1 + 阈值全局共享」，`character_validator` 加 warning 提示已自动归一。
- **情境修正器只接受结构化条件**，不要自然语言 `trigger`：字符串条件要么退化成脆弱的关键词匹配，要么每轮多一次 LLM 判定——后者正好抵掉对话欲累积省下的 token，自相矛盾；embedding 方案也与 §10.5「默认关」冲突。改用系统已算出的信号纯查表（`scene_id`、话题 `valence` 阈值、`WorldStage` 在场 actor 等），零 token。
- `floor`（权重下限）的价值绑在修正器上——没有能压低权重的东西就无物可守，砍修正器时一起砍。
- **永久权重漂移（角色弧光）暂缓**：写回角色卡等于程序偷改用户 YAML，不可做；存 session/world 状态则意味着同角色不同存档性格不同（这点反而与阶段 2.3 世界隔离自洽）。且漂移会破坏可复现性、长期容易把权重糊成均值。真要做需限定：仅明确剧情节点触发、幅度极小、有上限、当前值可在存档中查看。放到最后或作为 v2 实验特性。

**对话欲最终定稿（用户 2026-07-30 三次澄清，已实施，别再理解错）**
- 流程：**ThinkEngine 四维心情模型打分（一次短 JSON LLM）→ 算 `total_drive` → 超 `drive_threshold`（默认 0.6）即「想说」→ ActionPlanner 发 SPEAK / 定时器路径 schedule_intent；不超过 → WAIT（沉默）**。
- **无累积器**（`DriveAccumulator` 已删）：不做时间累积、无心情半衰期、无泄压，每轮独立计算总分判断。
- **LLM 只负责打分与候选内容**（四维 + 候选发言 + 建议延迟 + 热情度）；「说不说」由代码按阈值独立判定，模型不参与二元决定。
- **ThinkEngine 是决策区（模块化）**：`evaluate_speaking_drive()` 是唯一评估入口；ActionPlanner 只执行 SPEAK/WAIT（冲突检测保留为性格层），不再有 `_llm_decide` 二次判断与 0.7 强制降级。
- 已删除：`decide_initiative`（旧每轮二元决策）、`decide_drive_initiative`（累积器版）、`DriveAccumulator`、`drive_enabled` 开关（唯一路径无需开关）、hesitation 犹豫链全部（`hesitation_*` 配置键、`initiative_timer.hesitation[.set]` RPC 已 legacy/deprecated `remove_after="3.0.0"`，调用返回退役载荷）。
- **INITIATIVE_SPEAK 的 content 语义 = 「待表达意图摘要」**（用户 2026-07-30 追加定稿）：评估时的候选发言**绝不直接当定稿发送**（存意图不存话术，与定时器路径一致）；`ActionExecutor._execute_initiative_speak` 统一走 `InitiativeCoordinator.generate_initiative_message()`（说话前思考 + 即时生成，持 `_request_semaphore` 与回复互斥）。别再改回「预写内容直接发 MESSAGE_SENT」。
- **主动说话统一动作出口（用户 2026-07-30 定稿）**：一切主动开口（长期思考冲动、主动定时器触发）都以 ActionPlanner 的 `INITIATIVE_SPEAK` 动作发出；`SPEAK` 只服务「回复用户」的被动响应。`ActionExecutor.execute_initiative_speak()` 是唯一执行出口（事件链与定时器转发共用，`ACTION_EXECUTED` 统一在此发布）；coordinator 触发只负责把定时器翻译成动作并转发，不再自己生成发送。

---

## 5.6 深度审查归档（2026-07-29 全项目审查，阶段 5 开工前执行）

6 个只读审查代理分区审查（core/agent、world/、tools、config、runtime、memory/session/scene），以下为分诊结果。**多角色增量（1a–4）本身扎实，无 critical**；发现的问题多为存量隐患。

**已修复（本批未提交，定向 17 例 `tests/test_review_robustness.py` + validator 6 例，全量 541 passed）**
- **C1 孤儿生成**：响应槽位请求代际绑定。`request_id` 由发送方铸造并全链透传（MESSAGE_RECEIVED→ACTION_DECIDED→GENERATE_RESPONSE）；超时/取消后旧生成的 feed/complete/MESSAGE_SENT/主动定时器调度全部作废；`_execute_wait` 同样按 id 校验。无绑定旧事件（request_id=None）保持兼容。
- **C1' 会话切换陈旧组件**：`Agent._reset_session_scoped_state()` 统一失效（builder/handler 懒加载重建；`planner.update_memory_context`、`think_engine.update_semantic_memory` 就地更新，不动事件订阅）；`SessionManager.replace_messages` 原地复用 wm 实例——前端编辑不再被静默回滚。
- **Agent 信号注册 opt-out**：`Agent(setup_signal_handlers=False)`。多 Actor 下进程信号 last-wins 且先 shutdown 者直接 `sys.exit`，**World 装配 Actor 必传 False，由 World 统一接管**。
- **topics.json**：tmp+原子替换+`.bak`+损坏隔离 `.corrupt-*`+bak 恢复（原先崩溃即整个语义记忆静默清空）；顺带修 LLM 打分窗口并发删除 KeyError 与降级话题名撞名。
- **单角色语义记忆路径净化** `sanitize_path_id(character_name)`（防 `../../` 路径遍历 + 与会话目录规则一致）。
- **SCENE_SWITCHED 载荷** +`from_scene_id`/`actor_id`（阶段 5 WorldStage 联动用）。
- **config validator world 节硬化**：标量类型（id/enabled/user_initial_scene/两个 bool/actor.enabled/initial_scene）、persistence 非 dict 报错、protagonist 禁指 disabled actor、`temperature≤2`、`context_entries > max_entries_per_scene` 交叉警告。
- **工具/事件**：`execute_batch` 分段执行（同批「先写后读」语义正确，结果仍按 id 对齐）；`execute_sync` 对 async 工具守卫（无 loop 走 asyncio.run 真执行，有 loop 结构化错误——原先静默假成功）；`ToolRegistry.get` 去全局回退（unregister 真生效、跨 Actor 不泄漏）；`remember` 总线无响应时诚实报失败（原先假「记住了～」）；`Event.id` 全量 uuid（request 键 32bit 会撞）；`flush_critical` 暂存普通事件、不再被截断丢关键事件。

**阶段 5 必做（装配期）**
- **world 记忆根按显示名碰撞**：roster 两个 actor 净化后同名（或同角色卡挂两个 actor）→ 共享 `memory/world_<id>/<name>` 互踩。配置校验不读角色文件做不了，**装配时校验净化后名字唯一并硬报错**。
- 共享 ModelClient 显式绑 World bus；Actor 全部 `setup_signal_handlers=False`。
- WorldStage 读方法无锁（单循环内安全）：**stage 只在事件循环内访问**，不得从工作线程读。

**阶段 6 注意**
- `WorldMemoryProjector` 直调 `add_async` 必须自己捕获异常（内部已部分兜底但不保证不抛）。

**阶段 7 处理（定时器重构时一并，§7.3 对话欲累积的前置）**
- `InitiativeTimerManager.trigger()` 持锁跨整个 LLM 生成，discard/update/cancel 全阻塞 → 用户输入卡一整轮；`_run_timer` 已正确移出锁外，`trigger()` 对齐。
- 已触发的定时器无法被 discard 打断：回合生成中途到点会对同一 Actor 发起第二次并发生成，私历顺序错乱。World 高频驱动下概率放大。

**阶段 8 处理**
- `WorldPersistence.create` 的 exists→write 有 TOCTOU 竞态（走 `_lock_for` 或 `open "xb"`）；`list()` 名义只读实际会 quarantine/回写（自愈），docstring 补副作用说明；roster diagnostics 并入 stage 键（除 `__user__`）与 `current_actor_id`，防幽灵占位。
- `core/migrations` 把更高未知 schema_version 当 legacy 静默降级 vs `world/persistence` 硬拒绝——跨版本契约需统一。

**阶段 9 处理（runtime，两提交重构后的审查发现）——✅ 四条已全部落地（阶段 9）**
- ~~WS 断连/取消落在发送窗口 → 幂等账本永久 pending~~：`_send_streaming_rpc_frames` finally 显式 `aclose()` + service 层 `_iter_message_stream_locked`/`iter_world_message_stream` 捕获 GeneratorExit 收敛 cancelled + 网络/本地两层确定性关闭链。回归：`tests/test_runtime_robustness.py`。
- ~~`handle_ws` finally 清理链可被心跳异常截断~~：`_await_cancelled_task` 改为 `suppress(CancelledError, Exception)`，任一任务失败不再截断清理链。
- ~~单租户 `operations.json` 损坏拖垮整个进程启动~~：`_load_tenant_catalog` 按租户 try/except 隔离，损坏只跳过该租户并 warning（不静默重建空账本）。
- ~~`dependency.install` 同步 `subprocess.run` 冻结循环~~：`RuntimeService.install_dependencies` 改 `asyncio.to_thread` + timeout 钳到 `dependency_install_timeout_seconds` 上限。
- **world.\* 接线清单（缺一不可）**：`rpc.py` `RPC_METHOD_SPECS`+`_PUBLIC_RESULT_SCHEMAS`；`auth.py` `required_role`（**默认 fallthrough 是 admin，不给 world. 加分支就全变 admin-only**）；`rpc.py` `_NETWORK_RESOURCE_PREFIXES` + `service.py` `_is_tenant_method`（**漏加 = 跨用户共享同一 world 状态**）；`NETWORK_SESSION/REVISION/IDEMPOTENCY_METHODS` 按写语义补；`http_adapter.py:556` WS 流式方法硬编码分流（且 ack 帧先于参数校验）；`RuntimeState` 加 `world` 字段但**绝不能碰 root 的 `state.agent`**（`_uses_network_tenancy` 依赖它为 None）；`event_contract`+`_runtime_event_category_map` 加 world 事件分类；两套脱敏（`sanitize_event_payload` vs `_redact_sensitive_fields`）选定一套。——✅ 全部按单落地（阶段 9）；脱敏统一为 `sanitize_event_payload`（service 私有实现已删，字段并集 + `[REDACTED]` 统一）。
- 其余 minor：`_begin_message_operation` 丢弃 replay 返回值（哑弹）；错误码不稳定三处（裸 ValueError→`runtime.error`）；SSE 无心跳半开泄漏订阅；replay 与订阅间丢失窗口；只读 RPC 写副作用（`_activate_tenant_session`）；反代下限流误伤（X-Forwarded-For 不可达）；RPC body 上限可被 chunked 绕过；`allowed_origins` 带 path 静默判 None；`list_sessions` 游标建在可变排序键上。

**待用户定夺（设计级，未擅自改）**
- **WorkingMemoryManager.add_message 从不 trim**：`max_turns` 形同虚设，长会话全量历史进上下文（token 无界）。episodic 压缩本应接管但已是死子系统（`persistence=None`、无 `add_message` 调用方）。与 §10.5 记忆简化方向直接相关，建议优先决策。
- 工具路径首段前言重复入记忆（`_record_tool_results` 存 tool_call 消息一份，`MESSAGE_SENT` 又拼全文一份）。
- 「响应中断」的部分回复被投递却永不记录（`_impl.py` MESSAGE_SENT 过滤）。
- `MEMORY_WORKING_ADDED` 无发布者（死代码）；ActionPlanner 的 REMEMBER/RECALL 是死路（request 自阻塞 + source 前缀过滤）——是否刻意下线？
- `configure_web_search_tool` 仍写模块级全局（多 Actor 后初始化者胜；配置相同时无害）。
- episodic 死子系统：启用前必须先修 `compress()` 竞态与持久化。

---

## 6. 验证命令（每阶段必跑，对齐 CI）

```bash
uv run pytest -q                      # 全量测试，须全绿
uv run ruff check .                   # lint
uv run ruff format --check .          # 格式（不过就先 uv run ruff format .）
uv run pyright <改动的产品文件>        # 类型检查
```
项目也有 `./normalize_code.cmd`（ruff format + check + pyright + pytest 一条龙）。

## 7. 约定与风格

- 全中文 docstring / 注释，msgspec `Struct`，`field(default_factory=...)` 处理可变默认；enum 用 `StrEnum`（ruff UP042 会拦 `str, Enum`）。
- 目标 Python 3.14；行宽 100；ruff 规则 `E/F/I/UP/B/SIM`。
- 新增 world 相关代码放 `GensokyoAI/world/`；测试放 `tests/test_world_*.py`。
- 引用文件用真实存在的角色卡（`characters/zh_cn/` 下，注意没有 PatchouliKnowledge，用 RemiliaScarlet 等）。

## 8. 未提交改动清单（NoneBot2 QQ 适配器，2026-07-30）

**功能**：新增 NoneBot2 QQ 适配器（OneBot 11 反向 WS 收消息）。**架构关键决策（用户拍板）**：
适配器与 Runtime 同进程，经 `RuntimeHost`（backends/nb2/runtime_host.py）以网络主体
上下文进程内驱动 `RuntimeService` 多租户路径（驱动方式同 tests/test_runtime_multi_user.py
的 `set_current_principal`），不走 HTTP/WS——租户隔离、资源闸、幂等、revision 全保留，
零 socket 零鉴权配置。群/私聊映射 `agent_id = qq-group-<群号>` / `qq-user-<QQ号>`；
幂等键 `nb2:<botQQ>:<message_id>`。主动消息：`create_event_subscription` 返回的
asyncio.Queue 进程内推送（每租户一队列，免路由）；`GSK_NB2_INITIATIVE=false` 时改用
`initiative_timer.update {enabled:false}` 停用租户主动定时器。

**runtime 加法改动**（协议主版本内合法加法，params_schema 签名驱动自动生效）：
`initiative_timer.update` 新增 `enabled` 进程内开关（不落盘；false 废止待发计划并短路
后续调度；service.py / _impl.py / initiative_coordinator.py 三处透传）。

**提示词集中化（用户拍板）**：全部提示词收进 `core/agent/prompts.py` 的 `build_*_prompt`
纯函数（文本与逻辑分家；JSON 契约在 docstring 注明解析方）。同时完成反自说自话提示词
修复（生成上下文 + 说话前思考 + 对话欲评估三处：用户未回应时不得虚构用户回应、不得
独自推进话题，message 必须是新拍，situational_relevance 打低）。`message_builder` 与
provider 转换属组装逻辑，不搬。

新增（A）：`GensokyoAI/backends/nb2/{__init__,runtime_host,store,config,plugin,__main__}.py`、
`tests/test_nb2_adapter.py`、`docs/nb2_adapter.md`、`tmp/nb2.env.example`
已改（M）：`GensokyoAI/runtime/service.py`、`GensokyoAI/core/agent/{_impl,initiative_coordinator,think_engine,prompts}.py`、
`GensokyoAI/world/{director,initiative,memory_projector}.py`、`GensokyoAI/memory/{topic_store,episodic}.py`、
`tests/test_initiative_timer.py`、`docs/runtime_api.md`、`docs/en/runtime_api.md`、
`pyproject.toml`（nb2 extra）、`.gitignore`（.env / .env.* / nb2_data）、`docs/todo.md`（本节）
已删（D）：`GensokyoAI/backends/nb2/client.py`（HTTP/WS 版客户端，被进程内方案取代，未曾入库）

验证基线：610 passed, 3 subtests passed；ruff / pyright 全绿；
`python -m GensokyoAI.backends.nb2` 冒烟通过（插件加载、OneBot V11 注册、uvicorn 8080）。
未做：NapCat 真连 QQ 端到端（需真实 QQ 号）。

建议 commit message（待用户授权后提交）：
`feat(nb2): 新增 NoneBot2 QQ 适配器（进程内多租户宿主 + 主动消息事件推送），initiative_timer.update 支持 enabled 开关`

## 8.41 claude usage 链路补全（2026-08-01，用户提问「调用库基本都返回 token_used 吧」牵出）

- 答案：是（OpenAI 系 usage 自带/流式 include_usage，Anthropic 非流式 usage 字段、
  流式 message_start/message_delta 事件），但 claude_provider 整条链路把它丢了，
  §8.40 的成本采样在「claude + Moonshot + 流式」配置下永远拿不到样本。
- 修复：`UnifiedResponse` 新增 `usage` 槽位（非流式数据源）；`model_client.chat`
  非流式路径填充 `timing.usage`（此前只有流式 finish chunk 一条通道）；
  `claude_provider` 流式捕获 message_start（input 初始）/message_delta（output
  累计）附着终止 chunk，非流式 `_convert_response` 映射 response.usage。
- 测试：test_claude_provider_conversion.py 增 3 例（非流式映射/无 usage 兼容/
  流式事件捕获到 finish chunk）。基线：增量 41 passed，ruff / pyright 全绿。
- 落地：local.yaml / local_world.yaml 已按官方价目配置 kimi-k2.5 单价
  （输入 ¥4.00、输出 ¥21.00、缓存命中 ¥0.70 每百万 token，
  来源 platform.moonshot.cn/docs/pricing/chat-k25，gitignore 本地文件不入库）。
- 真消耗计费（用户「最好按真消耗来」）：新增 `price_input_cached_per_million`
  ——usage 拆缓存分项计价（Anthropic 风格 cache_read/cache_creation 独立字段、
  OpenAI 风格 cached_tokens 子集两种结构都认；缓存创建按全价），未配缓存价
  时回落全价保守；claude 流式/非流式 usage 提取同步带上缓存字段。

建议 commit message（待用户授权后提交）：
`fix(claude): usage 链路补全——流式事件捕获 + 非流式映射进 UnifiedResponse.usage，成本采样在 claude 链路可用`

## 8.40 额度健康动态阈值 + 紫色耗尽级（2026-08-01，用户定稿设计）

- 设计（用户原话）：阈值动态计算、放框架中心模块统一算、ModelClient 提供
  费用计算机制与消耗量、按中位数消耗算黄/红阈值、加紫色（消耗没了）。
- **ModelClient 费用采样**：`model.price_input/output_per_million`（元/百万
  token，schema/merge/validator 齐备，可空 = 不估算）；`_finish_timing`
  统一收口处按 usage 估算单次成本（OpenAI prompt/completion 与 Claude
  input/output 双键名兼容），滚动窗口 100 样本；`cost_stats()` 中位数/累计。
- **框架统一算法** `core/agent/quota_health.py`：`compute_quota_health`
  （纯函数）——warn = 中位成本 × 100 次、crit = × 20 次、余额 < 中位成本
  （撑不起一次典型调用）= **DEPLETED（紫）**；指数 = 余额/warn 封顶 100；
  无样本返回 None（绝不拿拍脑袋数字冒充动态阈值，调用方回落静态）。
- **/status 接入**：host 全租户成本样本聚合（`status["cost"]`，与内心戏
  延迟同构）；动态优先——`额度：🟡 健康指数 61（余额 ¥16.60，中位单次
  ¥0.0270，约可再聊 614 次）`；无样本回落 env 静态阈值（静态路径余额 ≤ 0
  也显示 🟣 耗尽）。
- 测试：tests/test_quota_health.py 17 例（四级边界/耗尽边界/指数封顶/
  无样本 None/双键名估算/中位数/动态优先/静态回落/紫色显示）。基线：
  增量 132 passed，ruff / pyright 全绿。
- 待用户配置：local.yaml 填服务商真实单价后动态路径才激活（模板已加注释示例）。

建议 commit message（用户已授权提交）：
`feat(core): 额度健康动态阈值——ModelClient 按单价×usage 采样单次成本，quota_health 按消耗中位数算黄/红阈值，新增紫色耗尽级`

## 8.39 /status 增强：额度健康指数 + 运行时长 + 版本 + 复读防护 + 记忆规模（2026-08-01，用户点单）

- **额度健康指数**（用户点名）：/status 内嵌余额行（此前要单独 /quota）。
  指数 = `min(100, 余额/quota_warn×100)`，🟢 ≥ warn（默认 ¥20）/ 🟡 ≥ crit
  （默认 ¥5）/ 🔴 告急；阈值走 `GSK_NB2_QUOTA_WARN` / `GSK_NB2_QUOTA_CRIT`。
  **5 分钟 TTL 缓存**（commands 模块级）——/status 是 USER 级全员指令，不能
  每次都打余额 API；查询失败回落「暂不可用」不阻断主体。
- **复读防护行**：`RepeatGuard.stats()`（冷却中/观察中/追踪数快照），
  plugin 经指令 metadata 把 `_repeat_guard` 递给 cmd_status；全员平静也如实显示。
- **记忆规模行**：host 聚合全租户 `semantic_memory.topic_count/memory_count`
  （同步属性，未启用语义的租户自然跳过）。
- **版本 + 运行时长行**：`importlib.metadata` 包版本（源码运行回落 dev）+
  RUNTIME_PROTOCOL_VERSION + host 启动至今 uptime（人性化格式化取两级单位）。
- 兼容性：`get_system_status` 纯增量键（适配器公开契约只加不改）；
  `_format_status` 新行全部按 key 存在才显示，旧调用/旧测试不受影响。
- 测试：_format_status 全字段/最小字典、额度三级+不可用、uptime 格式化、
  缓存只取一次、RepeatGuard.stats 快照（含冷却过期不计）、阈值 env 解析。
  基线：增量 101 passed，ruff / pyright 全绿（沿用用户「增量验证」要求）。

建议 commit message（待用户授权后提交）：
`feat(nb2): /status 增强——额度健康指数（TTL 缓存+阈值可配）、复读防护快照、全租户记忆规模、版本与运行时长`

## 8.38 nb2 多人同时 @ 交替回修复：待发合并（2026-08-01，用户报体验问题）

- 根因：`_chat` 按会话锁串行，A 的 LLM 回合进行中 B 的 @ 排队等锁，
  A 回完再单独回 B——两个人同时 @ 时 bot 交替回两条，像分别和两个人对话。
- 方案（待发合并）：新增 `backends/nb2/pending.py`（无 nonebot 依赖可单测）
  `PendingChatQueue`——按会话 key 的待发队列替代按会话锁：首个发言的调用方
  成为处理者，先等 `merge_window_seconds`（默认 1.5s，`GSK_NB2_MERGE_WINDOW_`
  `SECONDS`，0 = 不等待）合并窗口，再把攒下的全部消息经 `merge_batch` 合成
  一轮（文本按到达顺序逐行拼接、各自带【昵称】标记；上下文去重保序；幂等键
  取批次首条），一次生成、一条回复同时回应所有人。处理期间到达的消息并入
  下一批。竞态分析：入队/取批/收尾之间无 await，单线程语义无间隙丢消息。
- 多人提示：`build_multi_speaker_context`（prompts.py）在合并批注入「把每个
  人都回应到」；DEFAULT_EXTRA_PROMPT 说话人标记说明改「每行一人」。
- 行为边界：复读防护/印象/指令仍在入队前按条处理（muted 丢弃不合并）；
  第一印象生成按批去重；单人快速连发同样受益（合成一轮）；批处理失败时
  重建租户重试语义不变（同键幂等）。已知取舍：合并批幂等键取首条，QQ 对
  批内后续消息的罕见重投无法被幂等账本去重。
- 测试：tests/test_nb2_pending.py 11 例（队列处理权/保序/收尾间隙/清理，
  merge 拼接/去重/幂等键，窗口配置解析）。基线：增量 88 passed（nb2 相关），
  ruff / pyright 全绿（应用户要求本轮不跑全量）。

建议 commit message（待用户授权后提交）：
`fix(nb2): 多人同时 @ 交替回改待发合并——PendingChatQueue 按会话攒批，一次生成一条回复同时回应所有人（merge_window_seconds 可配）`

## 8.37 情景记忆（episodic）子系统整体删除：三层记忆收口为两层（2026-08-01，用户定稿「实事求是」）

- 背景：§5.6 待定夺最后一条。episodic 是「名义三层」的中间层——写入路径
  从未接线（唯一调用点是一行注释）、持久化 `persistence=None`、配置与事件
  空挂，每轮 `get_relevant_context` 永远返回空。其「历史压缩摘要」职责已被
  §8.29 定期记忆蒸馏（写进语义记忆）实质接替。用户拍板：整体删除，文档
  不再叫三层记忆。
- 删除面：`memory/episodic.py` 整文件、`EpisodicMemory` / `MemoryRecord`
  结构体（后者仅 episodic 使用）、`MEMORY_EPISODIC_COMPRESSED` 事件及监听、
  composition / runtime_context / _impl / message_builder 全部接线（含
  message_builder 每轮空调用的【历史记忆摘要】注入块）、
  `build_episodic_summary_prompt`、配置三键 `episodic_threshold` /
  `episodic_summary_model` / `episodic_keep_recent`（schema/merge 删除）。
- 配置退役通道：三键进 `_REMOVED_MEMORY_EPISODIC_KEYS` + `DEPRECATED_FIELDS`
  ——旧配置不报未知字段错误，只给 `config.field.deprecated` 迁移警告
  （指向 `memory.distill_turns`）；模板与本地配置已清理。
- 文档：README 中英「三层记忆系统」改「两层」；project_design 中英记忆表
  删情景行（语义记忆实现方式补「定期蒸馏」）；multi_character_design、
  包根 docstring 同步。user_guide 的「情景相关」（四维心情维度）与 episodic
  无关，未动。changelog 与 gsk-ai-multi-character 为历史档案，不改写。
- 测试：tests/test_episodic_removal.py 2 例（schema 无 episodic 字段、旧键
  警告不报错）；清理 test_agent_composition / test_web_search_tool /
  test_tool_build_service 三处构造参数。基线：755 passed, 3 subtests
  passed，ruff / pyright 全绿。至此 §5.6 待定夺六条全部收口。

建议 commit message（待用户授权后提交）：
`feat(memory)!: 删除情景记忆（episodic）死子系统——三层记忆收口为「工作+语义」两层，旧配置键走退役警告通道`

## 8.36 MEMORY_WORKING_ADDED 死代码删除（2026-08-01，用户点单）

- §5.6 待定夺第 4 条残余：`SystemEvent.MEMORY_WORKING_ADDED` 只有定义、全项目
  无发布者无订阅者，删除（`core/events.py`）。同条的 REMEMBER/RECALL 死路
  已于 §8.28 解决。至此 §5.6 待定夺只剩 episodic 死子系统（启用前再修）。
- 基线：753 passed, 3 subtests passed，ruff 全绿。

## 8.35 HalfCompletionMessage：响应中断半截回复计入中间状态并续说（2026-08-01，用户定稿）

- 背景：§5.6 待定夺第 3 条——响应中断时半截回复只投递给用户却永不入记忆
  （`_impl.py` 的「响应中断」过滤直接跳过 MESSAGE_SENT），角色对用户已看到的
  半句话失忆。用户定稿方案：半截回复计入中间状态 `HalfCompletionMessage`
  作为上下文，错误消息不提供给模型，下轮让它继续说完，说完后按普通消息处理。
- `core/agent/types.py`：新增 `HalfCompletionMessage`（msgspec Struct，仅
  `content`——剥离错误标记后的干净正文；不落工作记忆/会话存档）。
- `response_handler.py`：中断标记提取为单源常量 `STREAM_INTERRUPT_MARKER` +
  `strip_interrupt_marker()`（剥离 `\n[响应中断: ...]\n` 标记）；`_safe_stream`
  产出标记处改用常量。
- `_impl.py` 收尾分支重构：含中断标记 → 干净半截计入 `_half_completion`
  （不发 MESSAGE_SENT、不调度定时器）；正常完成 → 清除状态并按原路径发
  MESSAGE_SENT（即「说完后按普通消息处理」）。注入点在 `_on_generate_response`
  场景注入之后：`build_half_completion_context`（prompts.py，明令不重复已说
  部分、不提「中断」）随本轮 system_contexts 进模型，单角色与 World 回合
  同链生效。`_reset_session_scoped_state` 一并清除（会话级状态不跨会话）。
- 行为边界：用户可见侧零变化（标记文本照常随流投递）；空半截（首 chunk 即
  失败）不留状态；连续中断取最新半截；孤儿生成（request_id 失效）依旧零副作用。
- 测试：tests/test_half_completion.py 5 例（标记剥离 2 例 + Agent 级中断→续说
  →入记忆→清除全流程、空半截不留状态、会话切换清除）。基线：753 passed,
  3 subtests passed，ruff / pyright 全绿。

建议 commit message（待用户授权后提交）：
`feat(agent): HalfCompletionMessage——响应中断的半截回复计入中间状态，下轮注入提示词让角色接着说完，错误标记不进模型上下文`

## 8.34 §5.6 待定夺问题修复批次（2026-08-01，用户点单：修 1/2/5 + §8.27 存疑两条）

- 背景：§5.6 末尾「待用户定夺」六条与 §8.27 存疑两条悬置已久，用户拍板修
  第 1（工作记忆 trim）、2（工具前言重复入记忆）、5（web_search 模块级全局）
  与 §8.27 两条存疑；第 3（响应中断不记录）、4（MEMORY_WORKING_ADDED 死代码）、
  6（episodic 死子系统）维持原状不动。
- **工作记忆 trim 生效**：`WorkingMemoryManager.add_message` 末尾补 `_trim()`——
  按 `max_turns * 2` 截断，并丢弃头部孤儿 `tool` 消息（其 assistant tool_call
  已被裁掉，Provider 会拒绝非法配对）。此前 `WorkingMemory._trim` 只被无人
  调用的 `WorkingMemory.add()` 使用，`working_max_turns` 形同虚设、长会话
  token 无界。`replace_messages` / 回滚等显式操作不 trim（尊重前端编辑语义）。
- **工具前言去重**：`_record_tool_results` 写入的 assistant tool_call 消息
  content 置空——前言文本已包含在 MESSAGE_SENT 记录的完整回复（前言+续写）
  中，此前同一段前言在工作记忆里存两份，白占上下文 token。
- **web_search 去模块级全局**：`ToolRuntimeContext` 新增 `web_search_service`
  字段，`ToolExecutor` 按调用注入（composition 为每个 Actor 各建一份
  `WebSearchService`）；工具函数 `_current_service()` 优先读上下文注入，
  回落模块级兜底（兼容裸调用与测试，`configure_web_search_tool` 保留）。
  多 Actor 不再「后初始化者胜」。错误 details 的 provider 字段改读当前
  service 配置（测试 duck 类型无 config 时兜底模块配置）。
- **PersistenceListeners session_id 现查现报**：删掉注册时缓存的 `_session`
  （注册早于会话创建，曾导致保存完成事件的 session_id 恒为 None），上报处
  改为 `session_manager.get_current_session()` 现查。
- **World 模式 /know /meta /attention 生效**：`World.send_message[_stream]`
  新增 `system_contexts` 参数，经 `_dialogue_events` → `_run_actor_turn_stream`
  合入 Actor 回合上下文（仅本轮、不持久化）；`WorldConsoleBackend` 补
  `_build_system_contexts()`（最近 5 条，镜像单角色语义）并在 send 时透传。
  此前 world_backend 只初始化 `_prompt_context` 列表而全文无消费，命令写进
  去的提示词永远到不了模型。
- 测试：新增 test_working_memory_trim.py（3 例）、test_response_handler.py
  （前言只存一次）、test_persistence_listeners.py（注册后建会话也上报正确
  session_id）；test_web_search_tool.py 加注入优先/兜底 2 例；test_world_main.py
  加 contexts 到达模型/缺省不注入 2 例；test_world_console.py 加 /know 注入
  World 回合 1 例。基线：748 passed, 3 subtests passed，ruff / pyright 全绿。

建议 commit message（待用户授权后提交）：
`fix(core): §5.6 待定夺问题批次——工作记忆 trim 生效、工具前言去重、web_search 按 Actor 注入，PersistenceListeners session_id 现查、World 模式提示词命令注入回合`

## 8.33 写入侧话题淘汰：max_topics 死参数激活（2026-08-01，用户点单）

- 背景：§8.32 审查发现 `SemanticMemoryManager` 传的 `max_topics=50` 从未被
  `TopicAwareStore` 执行，话题数实际无上限。本节补上写入侧淘汰。
- `TopicAwareStore`：新增 `_evict_for_new_topic`（两个新话题创建点统一调用）——
  达到上限时淘汰「回忆权重最低（`_calculate_recall_weight`，并列取最久未更新）的
  非 pin 话题」，全 pin 才兜底淘汰；`_remove_topic` 连带移除其全部记忆、
  清理关联边并重建索引；构造参数新增 `pin_importance`（默认 8.0，复用 decay 模块常量）。
- 配置：`memory.semantic_max_topics`（默认 50）进 schema/merge/validator/模板；
  `SemanticMemoryManager` 改为从配置读 max_topics 与 topic_pin_importance。
- 顺带发现的 API 怪癖（未改）：`list_memories(topic_name=...)` 传不存在的话题名时
  不过滤、返回全部记忆——测试里改用 `get_memory(id)` 断言淘汰结果。
- 测试：test_memory_decay.py 增 TopicEvictionTests 3 例（上限执行+最弱淘汰、
  记忆连带移除、pin 免疫）。基线：增量 59 passed，ruff/pyright 全绿。

## 8.32 话题热度淘汰器（2026-08-01，用户点单，参考 Lumi_Nox memory/decay.py）

- 背景：原有「遗忘曲线」`_calculate_recall_weight` 只在检索打分里占 0.1 权重加成，
  不会淘汰任何东西；`max_topics=50` 是死参数从未执行；ThinkEngine 思考游走不看热度，
  老话题永远会被翻牌子。本节补上真正的读取时淘汰。
- `GensokyoAI/memory/decay.py`（新）：`topic_heat`（以 max(last_updated, last_accessed)
  按半衰期指数衰减，0.5^(age/half_life)）、`is_pinned`（importance ≥ pin 阈值免疫）、
  `filter_active_topics`（隐藏而非删除，保序）。读取时现算、无后台任务；
  冷话题被重新谈起时检索 `_refresh_topic` 刷新时间戳自然复活。
- 接线：ThinkEngine `_long_term_think` 游走池先过滤冷话题（全冷则不思考）；
  检索路径不做硬过滤（用户明确问起旧话题仍能回忆，只靠既有 recall_weight 软下沉）。
- 配置（memory.*，均进 merge/validator/模板）：`topic_decay_enabled`（默认 true）、
  `topic_half_life_hours`（默认 72，约 10 天无人提起即隐藏）、
  `topic_decay_threshold`（默认 0.1）、`topic_pin_importance`（默认 8.0）。
- 测试：tests/test_memory_decay.py 12 例（热度曲线/复活/pin/过滤保序/ThinkEngine
  冷话题不思考、热话题照常、关闭开关、pin 存活）。基线：增量 56 passed，ruff/pyright 全绿。

## 8.31 /status 负载水位与闸门用量（2026-07-31，用户点单）

- `RuntimeHost.get_system_status` 扩展：`gates`（跨 root 与全部租户服务聚合同名
  资源闸：active/waiting 求和、max_concurrent 求和即系统总容量）+ `load_level`
  （水位计算：healthy/warning/critical/unavailable——满载或排队 → 临界、
  利用率 ≥60% 或思考延迟中位 >15s → 警告、Runtime 排空 → 不可用）。
- /status 新格式：首行负载水位（🟢/🟡/🔴/⚫ + 原因），闸门行只显示 runtime 总闸
  与非空闲闸（`model 2/8（排队 1）`），空闲时显示「全部空闲」。
- 测试：聚闸容量、水位四态转移、格式输出。基线：增量 27 passed。

## 8.30 文档过时点清扫（2026-07-31，用户点单）

- 对照 §8.27-8.29 变更全面核查：README/README_en（记忆段改述为「定期蒸馏 + 自动
  检索注入」）、project_design 双语（记忆管理节重写、行动规划表删 THINK/REMEMBER/
  RECALL、内置工具清单更新）、user_guide 双语（builtin_tools 去 memory、补
  emotion_baseline 字段、补 tenant_max_agents_per_user）、QUICKSTART（builtin_tools
  去 memory）、gsk-ai-multi-character（parallel_safe 工具行去 remember/update_memory）、
  nb2_adapter（指令补 /status、新增引用原文与复读防护行为说明、配置表补
  GSK_NB2_QUOTE_CONTEXT）、runtime_api（租户上限 LRU 语义补充）。
- 保留：docs/todo.md 历史 §5.x-§8.x 流水（历史档案不改写）、docs/changelog.md。

## 8.29 定期记忆蒸馏（2026-07-31，用户点单「AI 主动写没用了就做定期提取」）

- 背景：§8.28 删除 AI 主动记忆工具后，单角色语义记忆只剩读取侧（存量注入/检索），
  没有写入路径；World 侧有记忆投影，单角色没有。用户定稿：做成定期从工作记忆提取。
- 机制（ThinkEngine 决策区新增）：`_impl` 回复完成后 `note_turn_for_distillation`
  计数，达到 `memory.distill_turns`（默认 10）后台执行 `distill_memories`——
  一次短 JSON 调用（`build_memory_distill_prompt`，角色第一人称提炼 0~3 条
  「事实/偏好/关系变化/情感重量事件」，明确不记寒暄琐事），逐条 `add_async`
  写入语义记忆（topic/情感效价随附）。确定性周期触发，替代已删除的 AI 主动工具。
- 隔离：挂在主动机制总闸（`_manage_initiative_timer` 且 `initiative_timer.enabled`）
  ——nb2-meta 元租户（enabled=False）与 World Actor（manage=False）天然不触发，
  不与 World 记忆投影双写；会话切换重置计数。
- 配置：`memory.distill_enabled`（默认 true）/ `memory.distill_turns`（默认 10，
  校验 ≥1），local.yaml 与模板已同步。
- 测试：计数触发/写入映射/禁用短路/空与畸形 JSON/截断与空项过滤。
  基线：721 passed, 3 subtests passed。

## 8.28 记忆工具与 REMEMBER/RECALL/THINK 动作子系统删除（2026-07-31，用户拍板）

- 用户决定：记忆工具「AI 主动记住」在实测中见啥记啥、语义记忆全变噪音，删除；
  死动作子系统（§8.27 挂起项）一并删除。**读取侧保留**：已有语义记忆仍经
  message_builder.get_relevant_context 自动注入提示词，memory.search RPC 与
  World 记忆投影（add_async 直调）不受影响；semantic store/topic graph 变只读存量。
- 删除面：tools/tool_builtin/memory_tool.py（remember/recall/update_memory 三工具）、
  MemoryServiceListeners（工具事件的唯一消费方，随之失效）、
  ActionType.THINK/REMEMBER/RECALL + ActionFactory.remember/recall +
  ActionExecutor 两个 case 与 _execute_remember/_execute_recall、
  build_service._MODULE_TOOL_PREFIXES 的 memory 条目。
- 配置：local.yaml builtin_tools「memory」→「web_search」（顺带修正：web_search
  工具必须在名单内才注入模型——此前 §8.23 实测走的是 service 直调，未验证注入链）；
  模板 builtin_tools 去掉「memory」。
- 测试：删 3 个 listener 用例 + 1 个 remember 用例。基线：717 passed, 3 subtests passed。

## 8.27 全项目结构优化（2026-07-31，用户「代码太大感觉屎山」）

- 审查：4 个 explore 子代理分区（runtime/core/backends/world-memory）+ 主审逐条复核。
- 已落地（全部全绿 push）：
  - 死代码清除（-501 行，23 文件）：MessageOperation、MetricsListeners 链、
    Lifecycle 死方法、parser/executor 死方法、utils 死函数、session/background/
    topic_store 死接口、tool_context 兼容壳、console 死方法。
  - service.py 去重：租户初始化共享尾部（_check_tenant_admin_gate/
    _get_or_create_tenant_service）、幂等键校验归一、_resolve_target_session
    消灭 5 处会话解析、_is_tenant_method/_requires_explicit_session 改读 rpc 常量单源。
  - World 编排块抽取为 runtime/service_world.py（WorldOpsMixin，-457 行，
    dispatch getattr 语义不变、调用方零改动；mixin 属性访问按文件定向关 pyright
    reportAttributeAccessIssue，其余诊断保留）。
  - 零散：config_merge choose 工厂（11 闭包）、character_validator 权重/基线
    校验参数化、nb2 store JSON 公共函数、world/_llm_json 三文件共用、提示词归位
    （开场场景/主动兜底 cue 入 prompts.py）、cli 配色常量、删键清单单源。
  - providers：update_config 下沉 BaseProvider（_build_client 钩子，删 6 份逐字
    override）；五个 provider 提取 _build_call_kwargs/_build_content_config
    （chat/chat_stream 参数组装各归一处，~140 行重复消除；注意 options 归一化
    保留在公开方法顶部，下游诊断还要用）。
- 结果：service.py 3523→3018；全仓净减约 1100 行；721 passed 全绿，ruff/pyright 干净。
- 暂缓（下轮或待决策）：租户块整体抽 tenancy.py（host/tests 直戳 _tenant_services，
  需改 ~10 处）、http_adapter WS 区域拆分、world.py 装配区抽 assembly.py、
  三套 JSON 容错存储合并（session/world/topic_store 微差需保留）、session 与
  persistence 的 sync/async 双胞胎合并、model_client 错误尾巴/embedding 双生、
  providers 深层共享（OpenAIClientBase/embeddings 下沉，注意 supports_embeddings
  的类型判断依赖）、config_env 表驱动、InitiativeScheduler 与 InitiativeTimerManager
  统一（架构决策）、REMEMBER/RECALL/THINK 动作子系统存废（词汇表级，需作者拍板）、
  ProviderDefinition.capabilities 与实例声明双源漂移（注册表缺 STRUCTURED_OUTPUT）。
- 存疑复核结论：service._lock 包 LLM 生成=承重不动；PersistenceListeners._session
  缓存恒 None（:673 上报 session_id 永 None）疑似潜伏 bug，待单独修；
  world 模式 /know /meta /attention 疑似静默失效（prompt context 未注入 world 回合），
  待单独修。

## 8.26 /status 思考延迟口径优化（2026-07-31，用户点单）

- `ModelClient.chat` 新增 `call_context` 调用方标签（写入 ModelCallTiming.context）；
  ThinkEngine 三处内心戏（长期思考/说话前思考/对话欲评估）打标 "think_engine"。
- `latency_stats(context=...)` 支持按方过滤并改报**中位数**（median_ms，
  保留 avg/last/max）；host.get_system_status 延迟专取 think_engine 上下文。
- /status 文案：`思考延迟：中位 X.Xs（近 N 次内心思考，峰值 Y.Ys）`；
  无样本显示 `思考延迟：预计中…`。
- 测试：中位数奇偶样本、上下文过滤、fake 签名适配、新文案断言。增量 52 passed。

## 8.25 情绪→行为倾向映射（2026-07-31，用户「just do it」）

- 阈值调制：`Emotion.threshold_adjustment()`（[-0.10,+0.12]）——happy/love/
  surprised 降阈（更爱说）、sorrow/fear/shame 升阈（消沉少言）、anger 微降
  （易呛）、disgust 微升（懒得理）；ThinkEngine 评估时叠加到 drive_threshold
  并钳制 [0.3, 0.9]，二元判断结构不变（§7.3），日志带调制明细。
- 行为倾向注入：`Emotion.behavior_tendency()`（显著情绪 ≥0.4 才给倾向）——
  消沉→简短低沉点到即止、心情好→话多愿延展、气头上→带刺易呛、嫌弃→冷淡
  疏离；经 `ThinkEngine.emotion_tone_context()` 合并进语气注入
  （build_emotion_tone_context 加 tendency 段），全 send 路径生效。
- 测试：调制方向/钳制、倾向文本、同一份 0.55 打分在 happy（说）/sorrow（沉默）/
  平静（沉默）三态对照、tone_context 合成。增量 28 passed。

## 8.24 memory 模型字段跟随主模型（2026-07-31，用户报 provider 不支持）

- `episodic_summary_model` / `auto_memory_model` 原默认值写死 Ollama 本地模型名
  （qwen3.5:9b），改为 `None = 跟随主模型`（ModelClient.chat 的 model=None
  回落 config.name）；merge 哨兵同步改 None；local.yaml 与模板同步。
- 事实记录：auto_memory_model 当前无消费方（纯预留字段）；
  episodic 写入路径（event_listeners:81）当前注释停用，episodic_summary_model 同为预留。

## 8.23 nb2 web_search 启用（ddg 免 key）（2026-07-31，用户点单）

- 启用路径（纯配置，零代码）：`tool.web_search.enabled: true` + `provider: ddg`
  （ddgs 包是主依赖已内置）+ `trigger_strategy: explicit`；
  不需要进 `tool.builtin_tools` 名单（web_search 不在 _MODULE_TOOL_PREFIXES，
  不受名单过滤）；provider 内建搜索（model.web_search_enabled）须保持 off 才不打架。
- 已实测 ddg 搜索返回正常（local.yaml 已开，模板 provider 本就是 ddg）。

## 8.22 nb2 /status 系统状态指令（2026-07-31，用户点单）

- `ModelClient` 新增 `_latency_samples` 滚动窗口（近 50 次模型调用耗时，
  挂 `_finish_timing`）+ `latency_stats()`（count/avg/last/max）。
- `RuntimeHost.get_system_status()`：按 agent_id 前缀统计开户（群/私聊/元租户），
  在途网络操作数（`_active_network_operations`），延迟借元租户模型客户端。
- nb2 `/status`（别名 /状态，USER 级人人可查）：开户数、处理中会话数、
  思考延迟三行。测试：格式化、handler、host 统计、latency_stats 滚动窗。

## 8.21 八维情绪状态机 emotion.py（2026-07-31，用户起骨架、AI 补全接线）

- 定位：**持续情绪状态**（mood），与四维动机（瞬态「说不说」）分层；
  八维 Ekman（anger/sorrow/fear/happy/love/surprised/disgust/shame，各 0~1）。
- 状态机 `EmotionState`：LLM 自评按 alpha=0.6 指数混合（防单轮跳变），
  随时间向基线半衰期衰减（默认 30min，clock 可注入测试）。
- **零新增 LLM 调用**：情绪自评挂进对话欲评估 JSON（schema 加 emotion 对象），
  一次评估调用同时产出四维动机 + 八维情绪；缺字段不清空当前状态。
- 三个消费口：回复语气注入（`_publish_message_received` 追加
  build_emotion_tone_context，全 send 路径生效）、对话欲评估输入（user prompt
  带当前情绪行）、角色卡 `emotion_baseline`（dict 字段，校验八维 0~1，
  Agent 启动时装配进 ThinkEngine；example.yaml 补四型示例）。
- 测试：lerp/clamp/dominant/context_line、衰减半衰、评估驱动状态、
  提示词字段、角色卡校验。基线：增量 150 passed。

## 8.20 RP 反模板 + 「不理」LLM 破例判定（2026-07-31，用户定稿）

- 反模板：`build_roleplay_system_prompt` 追加第 7 条【禁止模板化回复】——
  长短/结构/节奏随情绪变化、不沿用最近几条的开头句式与结尾模式、
  口癖点到即止、不强制三段式不以问句收尾。
- 「不理」从纯算法硬丢改「算法拦复读 + LLM 裁新内容」（用户：随角色性格自由点，
  比如求她可以偷偷理一下）：
  - RepeatGuard 新 verdict `MUTED_NOVEL`：冷却期内继续复读 → 照旧白丢（零 token）；
    内容有新意 → 交调用方。冷却期保留判重窗口（识别持续刷屏），到期/原谅才清零。
  - plugin `_judge_mute_break`：元租户脱稿短 JSON（build_mute_break_judge_prompt，
    {"forgive","respond"}），fail-closed 默认 ignore。
  - forgive → `guard.forgive()` 解除冷却 + 注入原谅上下文（嘴硬/下台阶随性格）；
    respond → 注入破例上下文（别扭/端架子回一句，「不理」状态不解除——偷偷理一下）。
  - 开关 `repeat_guard.llm_break`（默认 true；false = 一律静默到冷却结束，零额外 token）。
- 测试：冷却期复读白丢/新内容分流、forgive 提前解除且不影响他人、llm_break 配置读取。
  基线：697 passed, 3 subtests passed。

## 8.19 全身检查：伪异步专项审查 + 修复批次一（2026-07-31，用户点单）

- 审查：5 个 explore 子代理分区（core/agent、runtime、memory/session、
  backends/adapters/cli/commands、world/scene/tools/background/utils）+ 主审逐条亲验。
  总评：骨架健康（provider 全异步、持久化 worker 池、锁粒度大多正确）；
  真问题集中在 runtime 层幂等/会话文件 I/O 绕过了已有的异步变体。
- 修复批次一（A 热路径同步 I/O + B 免费小胜利）：
  - A1 `operation_store`：begin/succeed/fail/cancel 全链路 async，`_save_async`
    走 to_thread（账本随时长增长，原每条消息同步全量重写 2 次）。
  - A2 `_idempotent_response`/`_finalize_message_operation` 改 async，
    换用现成的 `load_messages_async`/`replace_messages_async`；
    SessionManager 新增 `replace_messages_async`。
  - A3 rollback/export RPC 换 `save_current_async`（新增）+ async 读。
  - B4 两处大 json.dumps trace 日志改 `logger.opt(lazy=True)` 惰性求值。
  - B5 coordinator 主动生成：`_build_tools` 与 `pre_speak_thought` 改 gather
    （缩短回合信号量占用一个 LLM 往返）。
  - B6 background stats 计数去掉 create_task/锁，直接同步自增（单线程原子）。
- 审查遗留（批次二候选，均未修）：media/providers 图片 base64 同步 I/O、
  topic_store O(N) 关键词打分、semantic 全量重 embed、nb2 sessions.json 每消息
  重写、repeat_guard difflib 无上限、external_manager 串行 refresh、world 投影
  串行、console /history 同步读写；存疑裁决：service._lock 包 LLM 生成=承重不动、
  world 段尾规划=有意、episodic=死代码启用前再修。
- 测试：store.begin 测试异步化、robustness/dependencies fake 补 async 镜像。
  基线：695 passed, 3 subtests passed。

## 8.18 nb2 引用原文进上下文（2026-07-31，用户点单 backlog）

- 起因：A 引用 B 的话 @bot 问「你认识她吗」，角色看不到被引用的内容只能瞎猜
  （reply 段此前被 _extract_group_text 整个丢弃）。
- 修复：reply 段经 get_msg 取原消息，拼成「（引用 昵称：…）」插入文本原位；
  纯文本截断 120 字（_QUOTED_TEXT_MAX_CHARS）；1200「消息为空」等失败静默跳过
  （引用上下文是增强不是必需）。与 to_me() 的回复检查同一接口，NapCat 侧有缓存。
- 开关 `GSK_NB2_QUOTE_CONTEXT`（默认开）；默认 extra_prompt 补标记说明
  （「（引用 昵称：…）是说话人引用回复的上一条消息内容」）；启动日志加一行。
- 测试：quote_context 默认值与 env 解析。基线：增量 46 passed（nb2）。

## 8.17 四维心情权重进角色卡（2026-07-31，用户点单 backlog）

- 角色卡新字段 `motivation_weights`（可选）：expression_drive / emotional_charge /
  relational_need / situational_relevance，各 0~1，默认 0.3/0.35/0.2/0.15
  （= 原硬编码通用人格基线，不写行为不变）；缺失维度回落默认，总和保持 1 量纲
  不变，刻意放大总和 = 整体更话痨。校验器：未知维度/越界/非对象均报错。
- 装配链：CharacterConfig.motivation_weights → Agent.start 传 ThinkEngine →
  `_parse_speaking_drive` 构造 MotivationProfile(weights=...)，total_drive 按
  角色权重加权（原硬编码权重移入 MotivationWeightsConfig 作默认值）。
  `_parse_speaking_drive` 顺带从 staticmethod 改实例方法（要读 self._motivation_weights）。
- example.yaml / example_en.yaml 补注释段（含四型人格调参思路）；user_guide 字段说明同步。
- 测试：权重解析/默认值/校验报错、同一份四维打分在不同权重下想说↔沉默对照。
  基线：694 passed, 3 subtests passed。

## 8.16 「两句两句回」修复（2026-07-31，用户报 bug）

- 现象：热聊中用户每回一句，角色回一句 + 过一会儿又追一句（=被动回复 +
  回复后对话欲评估超阈值排的主动消息），且评估在热聊里几乎总是超阈值。
- 排查结论：无双发 bug——定时器替换语义正常（schedule_intent 先弃旧排新，
  至多一个待发）；用户消息进来自动弃旧定时器 + 重置连续计数。问题在两点：
  1. 对话欲评估提示词只有「自己连说时打低」的约束，没有「对方正热聊时
     把话头留给对方」的约束 → 热聊中四维总分几乎必超 0.6。
  2. 长期思考路径（think_interval 每分钟一次思考 → INITIATIVE_SPEAK）
     不经 schedule_intent，不受 max_initiative_times 上限约束，可无限连发。
- 修复：build_speaking_drive_prompts 加节奏规则（对方积极回应、节奏紧凑
  时 situational/relational 明显打低，把话头留给对方，除非有等不及的事）；
  generate_initiative_message 入口统一检查 _has_reached_initiative_limit()，
  思考路径同闸——max_initiative_times 成为真正的全局连续主动上限
  （用户回复后计数重置自动恢复）。
- 测试：提示词节奏断言 + 管线上限拦截/重置恢复。基线：691 passed, 3 subtests passed。

## 8.15 主动消息提示词去机制化（2026-07-31，用户报「暴露内部」）

- 起因：QQ 主动消息漏出「……也罢，既然主动开口的时机到了——」——说话前思考
  的元叙述被学进了发送文本。根因链：`build_pre_speak_thought_prompt` 用
  「主动定时器到点了」机械框架起笔 → 思考文本自带机制词汇 → 逐字注入
  `build_initiative_message_context` → 生成时复述进用户可见消息。
- 修复（全链路提示词去机制词汇，纯 prompts 改动）：
  - pre_speak_thought：去掉「主动定时器到点」框架，改为「你之前就想找用户说点
    什么，现在觉得是时候开口了」；明令思考中不得出现「定时器/主动开口/主动发言/
    时机到了/系统」等幕后词汇（思考会参与后续生成）。
  - initiative_message_context：标题改「自然开口」；摘要/思考标注「参考，不要
    原样复述」；明令第一句不要承接内部思考过渡（「也罢」「既然……」之类）。
  - speaking_drive：message 字段禁「主动开口/主动发言」元描述词汇。
  - 合成 user 兜底消息（initiative_coordinator）同步去「主动开口」措辞。
- 测试：test_think_engine 同步新文案 + 防回归断言（不得再现「主动定时器到点」）。
  基线：690 passed, 3 subtests passed。

## 8.14 nb2 指令对接框架 commands 体系 + 框架四级权限（2026-07-31，用户定稿）

- 起因：nb2 指令分发是自建管线，没有框架 executor 的「issued command」执行日志；
  用户要求 nb2 对接框架 command 子模块，并把四级权限下沉到框架（未声明默认 OWNER）。
- 框架 `GensokyoAI/commands/`：
  - 新 `permission.py`：`PermissionLevel`（VISITOR0<USER1<ADMIN2<OWNER3，IntEnum）。
  - `@command(..., permission=..., registry=...)`：权限默认 OWNER（未声明即仅主人，
    开放需显式降级）；`registry` 可指定本地注册表（适配器隔离，避免与全局同名命令
    互相覆盖——console 与 nb2 都有 /help，共享进程/测试会话里必须隔离）。
  - `CommandContext.permission` 默认 OWNER（console 等本地后端向后兼容、行为不变）。
  - `CommandExecutor(registry=...)`：本地注册表查命令/同步 parser；`_execute_single`
    内置权限闸门，不足返回 failure「权限不足」（审计日志在执行器，是否告知用户由调用方定）。
- nb2：`commands.py` 重写为框架注册（NB2_COMMANDS 本地注册表；/help VISITOR、
  /quota USER；resolve_level 保留为 QQ 身份→四级映射）；plugin `_dispatch_command`
  改为构造框架 CommandContext（source="nb2"、issuer=昵称(QQ)、permission=解析级、
  metadata 带 host/send/config）后交给 CommandExecutor——未注册指令解析为空静默，
  权限不足对用户静默、日志有 issued+failed 审计。QQ 回复仍由 handler 经 send 发送，
  result.message 只进日志。
- 测试：tests/test_command_executor.py 新（默认 OWNER、闸门、本地注册表隔离、
  Minecraft 日志行）；tests/test_nb2_commands.py 重写。基线：690 passed, 3 subtests passed。

## 8.13 租户日志标签（2026-07-31，用户提议）——**已 revert（89ffe67）**

> 教训：loguru 全局 patcher 会被 nonebot.init 的 `logger.configure(patcher=...)`
> 覆盖，导致格式键缺失 KeyError。正确做法（若重做）：per-sink format 函数内补默认值。

## 8.12 租户上限 bug 修复：LRU 休眠驱逐（2026-07-31，用户报 bug）

- 起因：nb2 跑一天后新群/新私聊全部 `agent.limit_exceeded`——旧的
  `MAX_TENANT_AGENTS_PER_USER = 8` 硬常量对所有 QQ 租户共享（nb2 宿主是单一
  user），满 8 个后新租户初始化被硬拒绝，且无驱逐机制，永久堵死。
- 修复：`resource_control.tenant_max_agents_per_user`（默认 32，可配置，
  校验 minimum=1）替代硬常量；达到上限时 `_evict_idle_tenant` 休眠最久未活跃
  租户（优雅 shutdown 会话照常保存、manifest 与磁盘数据保留），新租户立即
  递补；被休眠租户再发言时插件走 agent.not_found 自愈链（重建租户恢复最新
  会话 + 重订阅），用户无感。正在处理请求的租户（_tenant_operation_lock
  持有中）不驱逐；全部繁忙才真的报 agent.limit_exceeded（背压语义保留）。
- 活跃度追踪：`_tenant_last_active` 在每次租户 RPC 派发与 init 成功时刷新
  （monotonic）；目录恢复的租户默认 0.0（最先被休眠）；删除/驱逐/shutdown 清理。
- 顺手开大 local.yaml 闸门（5 群并发不再排队误拒）：runtime 4→8、queue 8→16、
  provider/model 2→4、stream 1→2、tool 2→4、acquire_timeout 0.25→1.0；
  模板默认值保持保守，仅补 tenant_max_agents_per_user 注释。
- 测试：tests/test_runtime_multi_user.py 新增 4 例（LRU 驱逐、全忙报错、
  活跃度刷新、上限读配置）。基线：681 passed, 3 subtests passed。

## 8.11 复读烦躁模型（2026-07-31，用户定稿，未提交）

- 起因：群友反复刷「转」「停」等无意义内容，角色每条都认真回复，很 bot 且烧 token。
- 机制（接入层状态机，纯内存、零 token）：`backends/nb2/repeat_guard.py` 的
  `RepeatGuard` 按 (会话, 用户) 追踪——与近期消息窗口判重（归一化后相同或
  difflib 相似度 ≥ similarity）连击 +1，正常发言清零；连击 ≥ warn_streak 注入
  厌烦上下文（角色回复转冷淡），≥ mute_streak 注入「最后一句话」上下文
  （角色当面表态不理他，符合性格），随后进入 mute_minutes 冷却：期间该用户
  消息在适配器侧静默丢弃、不进 Runtime（零 token）；冷却结束自动消气从零计数。
- 阈值在**全局配置** `repeat_guard` 节（RepeatGuardConfig：enabled/similarity/
  history_size/warn_streak/mute_streak/mute_minutes，warn > mute 校验报错）；
  nb2 经 `RuntimeHost.get_app_config()` 读取（优先已装配租户配置，否则兜底链现加载）。
- 厌烦/告别文案不走罐头文本：prompts.py 的 `build_repeat_annoyance_context` /
  `build_repeat_farewell_context` 注入 system_contexts，由角色用性格表达；
  私聊同样生效（按 QQ 号分桶）。
- 设计说明：心情四维模型只门控主动发言，被动回复本就不经它，所以烦躁判定
  放在接入层（发送者身份只存在于接入层）——这与「心情模型」是同一意图的
  不同落点，勿把判重挪进 Runtime。
- 测试：tests/test_repeat_guard.py（状态机全转移、隔离、归一化、配置解析/校验/模板加载）。

## 8.10 nb2 指令系统四级权限模型（2026-07-30，未提交）

- `backends/nb2/commands.py`：`PermissionLevel`（VISITOR=0 < USER=1 < ADMIN=2 < OWNER=3，
  IntEnum 天然可比较）+ `BotCommand` 注册表 + `resolve_level` / `can_execute` 纯逻辑
  （nonebot 无关可单测）；新指令 = COMMANDS 加一行。
- 指令分发在 handler 层（指令不进会话）：群名片查询拿 role（失败 → VISITOR，fail-lower），
  未注册指令静默忽略，无权限静默拒绝（不提示指令存在）。
- 当前指令：`/help`（VISITOR 级，按调用者权限动态列出可见指令）、
  `/quota`（USER 级 = 全员开放，查 Provider 余额）。`GSK_NB2_OWNER_QQ` 语义变为
  OWNER 级名单，只影响 OWNER 级指令。
- 测试：tests/test_nb2_commands.py（权限解析、别名索引、help 按级过滤、quota 格式化）。
  基线：662 passed, 3 subtests passed。

## 8.9 额度查询与 nb2 指令系统（2026-07-30，未提交）

- Provider 额度接口（可选能力）：`BaseProvider.get_quota()` 默认 None；
  `claude_provider` 实现 Moonshot 余额端点（`_balance_url()` 从 base_url 推导
  `/v1/users/me/balance`，anthropic/v1 后缀自适应；非 Moonshot/无 key 返回 None）；
  `ModelClient.get_quota()` 透传；`RuntimeHost.get_quota(character)` 借元租户查询。
- nb2 指令系统（v1 仅额度查询）：`/quota` 或 `/额度`，**白名单鉴权**
  `GSK_NB2_OWNER_QQ`（空名单 = 全部静默拒绝，fail-closed）；指令不进会话、
  不触发租户初始化。回复形态：「当前额度：¥x（现金 ¥a，代金券 ¥b）」。
- 测试：claude 端额度桩测（URL 推导 + 鉴权头 + None 分支）、host 透传、owner 解析。
  基线：653 passed, 3 subtests passed。

## 8.8 Provider 校验元数据化（2026-07-30，用户提出「校验硬编码不优雅」）

- 新 `core/agent/providers/specs.py`：`ProviderSpec`（requires_api_key /
  allow_private_base_url / unsupported / discouraged / supported_web_search /
  unsupported_messages / extra_rule）+ `PROVIDER_SPECS` 全表——provider 校验知识的
  唯一事实源；新增 Provider 只需登记一行。deepseek 的 reasoning_effort 语义进
  `extra_rule` 钩子，ollama 的 api_path 定制诊断进 `unsupported_messages`。
- `config_validator.py` 删除三处硬编码（PROVIDERS_REQUIRING_API_KEY /
  KNOWN_PROVIDERS / PROVIDER_FIELD_MATRIX），全部改消费 PROVIDER_SPECS；
  诊断 code 全部不变（config.provider.unknown / api_key_missing / field_unsupported /
  api_path_unsupported / reasoning_effort_ignored 等），行为等价。
- 基线：647 passed, 3 subtests passed，ruff/pyright 全绿。

## 8.7 适配器约定（2026-07-30，用户设计，未提交）

- 新公共面：`GensokyoAI/adapters/__init__.py`（`RuntimeAdapter` 协议 + `run_adapters`/
  `serve_adapters` 组装入口，Ctrl+C 逆序停止 + host.close 统一保存）；
  `RuntimeHost` 从 `backends/nb2/runtime_host.py` 迁至 `GensokyoAI/runtime/host.py`，
  其方法签名即适配器公开契约。组装形态：`run_adapters(Nonebot2Adapter())`；
  外部包实现同协议即可接入。
- nb2 侧：`adapter.py` 的 `Nonebot2Adapter`（uvicorn 以 task 嵌入宿主循环，不用阻塞式
  nonebot.run()；`.env` 加载与 LOG_LEVEL=CRITICAL 收进 start）；plugin 改为
  `bind_host()` 注入宿主（**load_plugin 返回 Plugin 对象，模块在 .module 上；
  直接 import 插件模块会被 nonebot 拒绝登记**）；包根不导出符号（防无 nb2 extra 时
  测试套误 import nonebot）。
- 顺手修：`setup_logging` 幂等化——handler id 被外部 `logger.remove()`（nonebot.init）
  作废时容忍 ValueError（utils/logger.py）。
- 测试：test_adapters.py 生命周期（顺序启动/逆序停止）；test_nb2_adapter.py 导入路径更新。
  基线：644 passed, 3 subtests passed。

## 8.6 QQ 群聊风格适配（2026-07-30，群友反馈，未提交）

- `GSK_NB2_EXTRA_PROMPT`：随每条回复注入 `system_contexts` 的附加要求（RPC 原生通道，
  只影响当轮回复、不写入会话）；内置默认 = 群聊风格（简短口语、每句一行、不写动作、
  最多三句），`.env` 可覆盖。**目前不影响主动消息**（生成在 Runtime 侧，无每租户注入口）。
- `GSK_NB2_SPLIT_REPLY`（默认开）：回复按行拆成多条短消息发送（≤5 条、间隔 0.8s），
  工具函数 `split_reply_segments` 在 `utils/helpers.py`——按行拆，不做句读分析
  （用户拍板：模型按行写、适配器按行发，比程序切句子可靠；空格分隔被否，会误伤
  「Master Spark」这类文本）。主动消息同样走分段发送。
- 测试：test_nb2_adapter.py 增配置解析 / 按行拆段 / system_contexts 透传共 7 例。
  基线：624 passed, 3 subtests passed。

## 8.5 外部审计修复（2026-07-30，已修，未提交）

1. **World 自定义配置权限闸门缺项 → 已修**：核实 world.init 参数全集仅
   `config_path/session_id/start`，但闸门只拦 `config_path`。现抽出共享常量
   `_TENANT_ADMIN_ONLY_PARAMS`（service.py 顶部），`_init_tenant_agent` 与
   `_init_tenant_world` 同一道四项闸门（config_path/character_path/model_overrides/
   embedding_overrides）——纵深防御：world.init 将来新增同类参数不会静默绕过。
2. **agent_id 契约不一致 → 已修（取豁免方向）**：`world.init` 加入
   `_NETWORK_AGENT_ID_EXEMPT_METHODS`（rpc.py），schema 与行为统一为「可选、
   省略自动生成（结果返回）」，与 agent.init 先例一致；文档中英同步
   （资源模型 bullet + world.init 参数行）。
测试：test_runtime_multi_user.py 新增 3 例（world 四项参数非 admin 全拒、agent 闸门
回归、豁免集契约）。注意该文件 `_as_user` 帮手带 admin 角色，闸门测试须用新增的
`_as_chat_user`（read+chat）。基线：613 passed, 3 subtests passed，ruff/pyright 全绿。

