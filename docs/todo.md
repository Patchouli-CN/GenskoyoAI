# GensokyoWorld 多角色功能 — 交接文档

> 给接手的 AI：本文件是**唯一入口**。先通读本文，再看 `docs/gsk-ai-multi-character.md`（完整实施计划，含每阶段文件级细节与状态标注）。
> 两份文件都在 `docs/`，随仓库一起提交，clone 即可见。

## 0. 一句话现状

多角色 `GensokyoWorld` 分阶段实施中。**阶段 1a / 1b / 2.1 / 2.2 / 2.3 / 3 / 4 已完成、已提交（最新 `4ca2a83`）；阶段 5 开工前已完成全项目深度审查 + 阻塞项修复（审查修复批未提交）**。下一步从 **阶段 5** 继续。

- 基线：上一轮事件总线解耦 `4f2b0a2`；本次交接在其上新增数个 commit，用 `git log --oneline` 查看最新 HEAD。
- 当前测试：`541 passed, 3 subtests passed`，ruff check / pyright 全过。注意：`ruff format --check .` 有 2 个历史遗留未格式化文件（`GensokyoAI/runtime/media_store.py`、`tests/test_session_message_restore.py`，来自 runtime 重构提交），其余全部 format 干净。
- 所有新代码都是**纯增量**：单角色模式行为零变化，旧测试全绿。
- **阶段 5 开工前必读 §5.6**：全项目深度审查的已修清单与各阶段遗留任务都在那里。

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
- **⏳ 5 — GensokyoWorld 主类与状态机**（下一步）：`world/world.py` / `events.py` / `memory_projector.py` / `initiative.py`。开场（protagonist 是角色→主动开场；是 `__user__`→等用户）、用户回合、场景切换联动 WorldStage + 用户跟随。
- **⏳ 6 — 私有记忆投影**：`WorldMemoryProjector`，段落结束批量为在场角色各写各视角，失败降级不阻塞。
- **⏳ 7 — DialogueLoop 抽象**（去重关键）：`core/dialogue_loop.py` Protocol；`initiative_timer.py` 提取纯调度器依赖回调；`_impl.py` 单角色适配器 + `manage_initiative_timer: bool=True`（World Actor 设 false）；`world/initiative.py` World 主循环计划/触发。
- **⏳ 8 — 持久化恢复**：world bundle + actor session 关联 + export/delete/security。
- **⏳ 9 — Runtime / WebSocket / Console**：`world.*` RPC（init/start/send_message[_stream]/state/roster/transcript/move/session.*/shutdown）、流式 actor 事件、console `world_backend.py` + `--world` + `/world` `/roster` `/stage` `/transcript`。`RuntimeState` 加 `world: GensokyoWorld | None`。
- **⏳ 10 — 文档与完整验收**：README 中英、QUICKSTART、runtime_api、changelog/version、草案状态。

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

**阶段 9 处理（runtime，两提交重构后的审查发现）**
- WS 断连/取消落在发送窗口 → 幂等账本永久 pending（fail-closed 放大）：`_send_streaming_rpc_frames` finally 显式 `aclose()` + service 层把 GeneratorExit 也收敛 cancelled（**需验证**）。
- `handle_ws` finally 清理链可被心跳异常截断 → 事件订阅/流任务泄漏：清理助手按任务 `suppress(Exception)`。
- 单租户 `operations.json` 损坏拖垮整个进程启动：catalog 按租户隔离失败。
- `dependency.install` 在 async 链路同步 `subprocess.run` 最长 600s 冻结全循环：`to_thread` + timeout 钳上限。
- **world.\* 接线清单（缺一不可）**：`rpc.py` `RPC_METHOD_SPECS`+`_PUBLIC_RESULT_SCHEMAS`；`auth.py` `required_role`（**默认 fallthrough 是 admin，不给 world. 加分支就全变 admin-only**）；`rpc.py` `_NETWORK_RESOURCE_PREFIXES` + `service.py` `_is_tenant_method`（**漏加 = 跨用户共享同一 world 状态**）；`NETWORK_SESSION/REVISION/IDEMPOTENCY_METHODS` 按写语义补；`http_adapter.py:556` WS 流式方法硬编码分流（且 ack 帧先于参数校验）；`RuntimeState` 加 `world` 字段但**绝不能碰 root 的 `state.agent`**（`_uses_network_tenancy` 依赖它为 None）；`event_contract`+`_runtime_event_category_map` 加 world 事件分类；两套脱敏（`sanitize_event_payload` vs `_redact_sensitive_fields`）选定一套。
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

## 8. 未提交改动清单（截至深度审查修复批完成）

审查修复已改（M）：`GensokyoAI/core/agent/{_impl,action_executor,action_planner,think_engine,runtime_context}.py`、`GensokyoAI/core/{events,event_listeners,config_validator}.py`、`GensokyoAI/session/manager.py`、`GensokyoAI/memory/topic_store.py`、`GensokyoAI/tools/{executor,registry,tool_context}.py`、`GensokyoAI/tools/tool_builtin/memory_tool.py`、`tests/test_world_config.py`、`docs/todo.md`
审查修复新增（??）：`tests/test_review_robustness.py`

建议 commit message（供用户参考，AI 不要自己提交）：
`fix(core): 深度审查修复（孤儿生成请求绑定/会话组件失效/记忆原子写/工具与事件健壮性）`

