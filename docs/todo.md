# GensokyoWorld 多角色功能 — 交接文档

> 给接手的 AI：本文件是**唯一入口**。先通读本文，再看 `docs/gsk-ai-multi-character.md`（完整实施计划，含每阶段文件级细节与状态标注）。
> 两份文件都在 `docs/`，随仓库一起提交，clone 即可见。

## 0. 一句话现状

多角色 `GensokyoWorld` 分阶段实施中。**阶段 1a / 1b / 2.1 / 2.2 / 2.3 / 3 已完成、已提交；阶段 4 已完成、已验证全绿、尚未提交**。下一步从 **阶段 5** 继续。

- 基线：上一轮事件总线解耦 `4f2b0a2`；本次交接在其上新增数个 commit，用 `git log --oneline` 查看最新 HEAD。
- 当前测试：`518 passed, 3 subtests passed`，ruff check / pyright 全过。注意：`ruff format --check .` 有 2 个阶段 4 之外的历史遗留未格式化文件（`GensokyoAI/runtime/media_store.py`、`tests/test_session_message_restore.py`，来自 runtime 重构提交），world 相关文件全部 format 干净。
- 所有新代码都是**纯增量**：单角色模式行为零变化，旧测试全绿。

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

## 8. 未提交改动清单（截至阶段 4 完成）

阶段 4 已改（M）：`GensokyoAI/core/events.py`（+`WORLD_DIRECTOR_DECISION`）、`GensokyoAI/world/types.py`（+`DirectorPhase`/`ActorBrief`/`DirectorContext`）、`GensokyoAI/world/__init__.py`、`docs/{todo,gsk-ai-multi-character}.md`（todo.md 另含另一会话 AI 追加的 §5.5 老手笔记，与阶段 4 无关）
阶段 4 新增（??）：`GensokyoAI/world/director.py`、`tests/test_world_director.py`

建议 commit message（供用户参考，AI 不要自己提交）：
`feat(world): Director 智能选角与决策降级（阶段 4）`

