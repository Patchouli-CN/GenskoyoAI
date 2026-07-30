# Runtime API 契约

本文档描述 GensokyoAI Runtime 对前端、桌面客户端、CLI 与第三方集成暴露的稳定 JSON RPC 契约。

## 版本与兼容性

- 当前 package 版本：`2026.7.30.0`
- 当前协议版本：`2.1.0`
- 当前协议主版本：`2`
- 兼容性策略：同一主版本内可以新增字段和方法；删除字段、修改语义或改变错误结构需要进入 breaking changes。
- 客户端应优先调用 `runtime.info`，再根据 `protocol_version`、`capabilities`、`methods`、`legacy_methods` 与 `method_specs` 决定可用功能。
- 文档中的方法清单应以 `runtime.info.methods` 和 `runtime.info.method_specs` 为准；示例会尽量列出当前 `GensokyoAI.runtime.rpc.RPC_METHOD_SPECS` 的完整非 legacy 方法，避免只展示子集导致客户端误解。

## 远程多用户资源模型

HTTP、WebSocket 和 SSE 使用 `user_id -> agent_id -> session_id -> message_id` 资源层级。JWT 的 `sub` 是稳定 `user_id`；客户端不能提交或覆盖 `user_id`。同名 `agent_id` 和 `session_id` 在不同用户下完全隔离。

- `agent.init` 可传 `agent_id`；省略时由服务生成。`agent.list` 只返回当前用户的 Agent，`agent.delete` 需要 `admin`。
- 网络侧 Agent / Session RPC 必须传 `agent_id`。所有对话上下文操作必须再传 `session_id`，不使用进程级“当前会话”。
- `world.*` 多角色编排方法同样按 `user_id -> agent_id` 租户隔离；`world.send_message` / `world.send_message_stream` 必须传 `idempotency_key`（与 Agent 消息共用 `operations.json` 操作账本，账本槽位为 World 存档 id）。World 没有 session revision 概念，写操作不要求 `expected_revision`（World 回合锁已串行化）。
- 会话写操作必须传读取时取得的 `expected_revision`。冲突返回 `session.revision_conflict`，客户端应重新读取后再合并。
- `agent.send_message` 和 `agent.send_message_stream` 还必须传 1-128 字符的 `idempotency_key`；网络重试必须复用同一键。
- Runtime 会在调用 Provider 前将消息操作写入 `operations.json`。客户端可用 `message.status` 按 `agent_id`、`session_id` 和 `idempotency_key` 查询 `pending`、`succeeded`、`failed` 或 `cancelled` 状态。
- JSON Lines bridge 是本机受信任入口，继续保留单用户和隐式当前会话兼容行为。

远程认证推荐由 TLS 反向代理/OIDC 网关签发短期 HS256 JWT，并配置：

```text
GENSOKYOAI_RUNTIME_JWT_SECRET=<至少 32 字符>
GENSOKYOAI_RUNTIME_JWT_ISSUER=<可选，严格匹配 iss>
GENSOKYOAI_RUNTIME_JWT_AUDIENCE=<可选，严格匹配 aud>
```

JWT 必须包含 `sub`、`exp`，可包含 `roles`。支持 `read`、`chat`、`admin`；`admin` 包含其他权限。旧 `GENSOKYOAI_RUNTIME_TOKEN` 仍可用，但只代表一个共享的 `runtime-admin` 管理身份，不提供多用户区分。

HTTP/WS 默认关闭 `runtime.shutdown`、`dependency.install`、`character_package.import` / `export` 等远程管理方法。仅在受信任管理网络中设置 `GENSOKYOAI_RUNTIME_ALLOW_REMOTE_ADMIN=true` 或使用 `--allow-remote-admin` 后才会开放；`GET /info` 的 `active_transport.disabled_methods` 是当前传输的权威声明。

默认文件后端将数据写入 `runtime_data/users/<user-hash>/agents/<agent-hash>/`，可用于单节点远程多用户服务。`BlobStore` 已定义对象存储替换边界；多节点部署仍需将会话/Agent catalog 迁移到 PostgreSQL，并将 blob 后端切换为共享对象存储。当前文件后端不能宣称支持多节点一致性。

## 发现接口

`runtime.info` 返回运行时元数据：

```json
{
  "name": "GensokyoAI Runtime",
  "package_version": "2026.7.14.0",
  "protocol": "gensokyo-runtime-rpc",
  "protocol_version": "2.1.0",
  "protocol_major_version": 2,
  "capabilities": ["agent.lifecycle", "agent.messaging", "agent.reasoning.public", "agent.streaming", "character.discovery", "character.validation", "character_package.management", "dependency.management", "external_tool.status", "memory.management", "memory.search", "memory.graph", "media.upload", "media.image_input", "message.operation_status", "model.discovery", "config.validation", "migration.diagnostics", "resource_control.runtime_gates", "runtime.events", "runtime.health", "runtime.readiness", "runtime.graceful_drain", "runtime.multi_user", "runtime.rbac", "runtime.transport_discovery", "runtime.versioning", "session.management", "initiative_timer.management", "world.orchestration"],
  "methods": ["runtime.info", "runtime.health", "runtime.ready", "runtime.shutdown", "config.validate", "character.validate", "character_package.validate", "character_package.preview", "character_package.import", "character_package.export", "agent.init", "agent.list", "agent.delete", "agent.send_message", "agent.send_message_stream", "message.status", "character.list", "model.list", "model.info", "session.create", "session.list", "session.current", "session.resume", "session.delete", "session.export", "session.rename", "session.messages", "session.replace_messages", "session.regenerate_from", "session.rollback", "dependency.status", "dependency.install", "external_tool.status", "initiative_timer.current", "initiative_timer.update", "initiative_timer.cancel", "initiative_timer.trigger", "memory.list", "memory.search", "memory.get", "memory.update", "memory.delete", "memory.graph", "media.list", "media.delete", "scene.current", "scene.list", "scene.get", "scene.switch", "scene.graph", "world.init", "world.start", "world.send_message", "world.send_message_stream", "world.state", "world.roster", "world.transcript", "world.move", "world.session.create", "world.session.list", "world.session.resume", "world.session.delete", "world.session.export", "world.shutdown"],
  "legacy_methods": ["init", "send_message", "send_message_stream", "list_characters", "create_session", "list_sessions", "current_session", "resume_session", "delete_session", "export_session", "rename_session", "rollback_session", "shutdown", "dependency_status", "install_dependencies", "external_tool_status", "initiative_timer.hesitation", "initiative_timer.hesitation.set"],
  "method_specs": [
    {"method": "runtime.info", "handler": "info", "legacy": false, "namespace": "runtime", "deprecated": false, "replacement": null, "remove_after": null},
    {"method": "init", "handler": "init", "legacy": true, "namespace": "legacy", "deprecated": true, "replacement": "agent.init", "remove_after": "2.0.0"}
  ],
  "schema_versions": {
    "config": 1,
    "session": 2,
    "memory": 2,
    "session_export": 1,
    "character_package": 1
  },
  "config_schema_version": 1,
  "deprecated_methods": [
    {
      "method": "init",
      "replacement": "agent.init",
      "remove_after": "2.0.0"
    }
  ],
  "breaking_changes": [
    {
      "scope": "runtime.rpc.error_envelope",
      "change": "RPC method failures now use the transport-level ok=false envelope instead of a nested result.ok=false payload.",
      "migration": "Check the top-level ok field and read the top-level error object."
    },
    {
      "scope": "runtime.websocket.stream_start",
      "change": "WebSocket streaming requests now receive a start acknowledgement before stream events.",
      "migration": "Read result.stream_id and result.generation_id from the acknowledgement frame before consuming event frames."
    },
    {
      "scope": "runtime.info.protocol",
      "change": "runtime.info.protocol now identifies the shared RPC protocol instead of naming one transport.",
      "migration": "Discover concrete transports through runtime.info.transports."
    },
    {
      "scope": "runtime.reasoning_visibility",
      "change": "Reasoning content is public by default in non-streaming and streaming agent responses.",
      "migration": "Render reasoning_content separately from content and apply client-side visibility policy when needed."
    }
  ],
  "transports": [
    {"name": "json-lines", "streaming": "aggregate"},
    {"name": "http", "streaming": "aggregate"},
    {"name": "websocket", "streaming": "incremental"},
    {"name": "sse", "streaming": "runtime-events"}
  ],
  "stream_protocol": {
    "version": 2,
    "reasoning_default": "public",
    "start_acknowledgement": true,
    "correlation_fields": ["stream_id", "generation_id"],
    "generation_resume_supported": false,
    "recovery": "message.status then session.messages"
  },
  "deprecated_fields": [],
  "compatibility_notes": [
    {
      "scope": "runtime.rpc.legacy_methods",
      "status": "deprecated",
      "message": "Legacy non-namespaced RPC methods remain available for compatibility; new clients should use namespaced methods from runtime.info.methods.",
      "replacement": "Use runtime.info.method_specs to map legacy methods to namespaced replacements."
    }
  ],
  "migration_diagnostics": {
    "recent": [],
    "counts": {"migrated": 0, "skipped": 0, "failed": 0}
  },
  "resource_control": {
    "enabled": true,
    "categories": {"model": 2, "tool": 2, "web_search": 1, "image_generation": 1, "dependency_install": 1},
    "provider_max_concurrent": 2,
    "default_timeout_seconds": 120.0,
    "dependency_install_timeout_seconds": 600,
    "gates": {
      "runtime": {"max_concurrent": 4, "queue_size": 8, "active": 0, "waiting": 0}
    }
  }
}
```

`method_specs[].params_schema` 会按调用上下文生成。HTTP/WS 返回的 `contract_scope: "network"` schema 已包含网络资源层额外要求的 `agent_id`、`session_id`、`expected_revision` 和 `idempotency_key`。`result_schema_complete` 只有在返回结构已显式建模时才为 `true`；为 `false` 时客户端不能把宽泛的 `dict` schema 当成完整字段契约。

非 legacy 方法清单按当前命名空间分组如下：

- `runtime.info`、`runtime.health`、`runtime.ready`、`runtime.shutdown`
- `config.validate`
- `character.validate`、`character.list`
- `character_package.validate`、`character_package.preview`、`character_package.import`、`character_package.export`
- `agent.init`、`agent.list`、`agent.delete`、`agent.send_message`、`agent.send_message_stream`
- `message.status`
- `model.list`、`model.info`
- `session.create`、`session.list`、`session.current`、`session.resume`、`session.delete`、`session.export`、`session.rename`、`session.messages`、`session.replace_messages`、`session.regenerate_from`、`session.rollback`
- `dependency.status`、`dependency.install`
- `external_tool.status`
- `initiative_timer.current`、`initiative_timer.update`、`initiative_timer.cancel`、`initiative_timer.trigger`（`initiative_timer.hesitation` / `initiative_timer.hesitation.set` 已废弃为 legacy）
- `memory.list`、`memory.search`、`memory.get`、`memory.update`、`memory.delete`、`memory.graph`
- `media.list`、`media.delete`
- `scene.current`、`scene.list`、`scene.get`、`scene.switch`、`scene.graph`

Legacy 兼容方法仍可用但已废弃：`init`、`send_message`、`send_message_stream`、`list_characters`、`create_session`、`list_sessions`、`current_session`、`resume_session`、`delete_session`、`export_session`、`rename_session`、`rollback_session`、`shutdown`、`dependency_status`、`install_dependencies`、`external_tool_status`。新客户端应使用 `method_specs[].replacement` 迁移到命名空间方法。

## Runtime 版本与迁移诊断

`runtime.info` 会暴露 package version、Runtime 版本和 schema version 摘要：

- `package_version`：当前 GensokyoAI 包 / 项目版本；优先来自安装包 metadata，源码运行时回退读取 `pyproject.toml`。
- `protocol_version` / `protocol_major_version`：Runtime RPC 协议版本。
- `schema_versions.config`：配置 schema version。
- `schema_versions.session`：会话文件 schema version。
- `schema_versions.memory`：记忆 topic store schema version。
- `schema_versions.session_export`：会话导出包 schema version。
- `schema_versions.character_package`：角色包 schema version；当前 `.gensokyo-character` 格式为 `1`。
- `deprecated_methods`：已废弃 RPC 方法及替代方法。
- `deprecated_fields`：已废弃字段；当前为空数组。
- `compatibility_notes`：兼容性提示；当前包含 legacy 非命名空间 RPC 方法仍兼容但建议迁移到命名空间方法的说明。

`runtime.info.migration_diagnostics` 返回最近迁移摘要：

```json
{
  "recent": [
    {
      "source": "session",
      "status": "migrated",
      "from_schema_version": null,
      "to_schema_version": 1,
      "format": "gensokyoai.session.file",
      "path": "sessions/reimu/example.json",
      "backup_path": "sessions/reimu/example.json.bak",
      "message": "Session file migrated to current schema version.",
      "diagnostics": [],
      "migrated_at": "2026-05-11T00:00:00+00:00"
    }
  ],
  "counts": {"migrated": 1, "skipped": 0, "failed": 0}
}
```

迁移诊断字段说明：

- `source`：迁移来源，例如 `session` 或 `memory.topic_store`。
- `status`：迁移状态，当前会产生 `migrated` 和 `failed`；`skipped` 为预留计数。
- `from_schema_version` / `to_schema_version`：迁移前后 schema version；无版本旧格式为 `null`。
- `format`：迁移后的目标格式名。
- `path`：被迁移文件路径。
- `backup_path`：迁移前备份路径；memory schema 1→2 自动迁移会在改写前创建 `.bak` 备份，失败时应保留原文件与备份并依据 diagnostics 修复或回退。
- `message`：用户可读摘要。
- `diagnostics`：结构化诊断列表；失败时包含稳定 `code`、`severity`、`message` 和修复建议。
- `migrated_at`：迁移诊断记录时间。

## RPC 请求格式

HTTP `/rpc` 与 WebSocket 普通 RPC 使用相同请求格式：

```json
{
  "id": "request-1",
  "method": "runtime.health",
  "params": {}
}
```

- `id`：客户端自定义请求编号，可为字符串或数字。
- `method`：方法名，推荐使用带命名空间的新方法名。
- `params`：对象；没有参数时传 `{}` 或省略。

## 成功响应格式

```json
{
  "id": "request-1",
  "ok": true,
  "result": {}
}
```

## 错误响应格式

```json
{
  "id": "request-1",
  "ok": false,
  "error": {
    "code": "method_not_found",
    "error_code": "method_not_found",
    "message": "请求的 Runtime RPC 方法不存在。",
    "technical_message": "Unknown method: bad.method",
    "user_message": "请求的 Runtime RPC 方法不存在。",
    "recoverable": true,
    "action_hint": "请改用 runtime.info 返回的 methods 或 legacy_methods 中列出的方法。",
    "details": {}
  }
}
```

客户端应基于 `code` 或 `error_code` 做稳定分支，不要解析自然语言 `message`。

资源控制触发时会返回 `resource.limit_exceeded`：

```json
{
  "ok": false,
  "error": {
    "code": "resource.limit_exceeded",
    "error_code": "resource.limit_exceeded",
    "message": "Runtime 当前资源繁忙，请稍后重试。",
    "recoverable": true,
    "action_hint": "请稍后重试，或调大 resource_control 中对应并发 / 队列配置。",
    "details": {
      "resource": "runtime",
      "reason": "queue_full",
      "max_concurrent": 4,
      "queue_size": 8,
      "active": 4,
      "waiting": 8,
      "action": "agent_message"
    }
  }
}
```

## WebSocket 流式帧

RuntimeService 当前提供两种流式消费形态：

- `iter_message_stream()`：async iterator，按 Agent 流式 chunk 即时产出 Runtime 事件；WebSocket `/ws` 的 `agent.send_message_stream` 使用该形态逐帧推送。
- `send_message_stream()`：聚合形态，收集完整 `events` 后一次性返回；JSON Lines RPC 与 HTTP `POST /rpc` 使用该形态，便于保持一请求一响应兼容性。

WebSocket 客户端发送：

```json
{
  "id": "stream-1",
  "method": "agent.send_message_stream",
  "params": {
    "agent_id": "agent-1",
    "session_id": "session-1",
    "expected_revision": 4,
    "idempotency_key": "send-018f...",
    "message": "你好"
  }
}
```

服务端会先返回启动确认帧，其中包含服务端分配的 `stream_id` 和本次生成的 `generation_id`：

```json
{
  "id": "stream-1",
  "ok": true,
  "result": {"stream_id": "...", "generation_id": "..."}
}
```

随后按生成进度返回事件帧：

```json
{
  "id": "stream-1",
  "ok": true,
  "stream_id": "...",
  "generation_id": "...",
  "event": {"type": "content", "index": 0, "content": "...", "generation_id": "..."}
}
```

结束帧：

```json
{
  "id": "stream-1",
  "ok": true,
  "stream_id": "...",
  "generation_id": "...",
  "done": true,
  "result": {
    "role": "assistant",
    "content": "...",
    "reasoning_content": "...",
    "generation_id": "...",
    "events": [
      {"type": "content", "index": 0, "content": "..."},
      {"type": "finish", "index": 1, "content": "..."}
    ],
    "session": {},
    "initiative_timer": {
      "timer_id": "abcd1234",
      "status": "scheduled",
      "generation": 3,
      "source": "ai",
      "created_at": "2026-06-07T09:00:00+00:00",
      "updated_at": "2026-06-07T09:00:00+00:00",
      "due_at": "2026-06-07T09:05:00+00:00",
      "delay_seconds": 300,
      "remaining_seconds": 299,
      "pending_summary": "刚才那件事我又想了想……",
      "reason": "角色想稍后补充一句",
      "user_modified": false,
      "editable_fields": ["due_at", "delay_seconds", "pending_summary"]
    }
  }
}
```

`reasoning_content` 在协议 `2.0.0` 中默认公开。纯 reasoning chunk 使用 `type: "reasoning"`，正文 chunk 使用 `type: "content"`；两者携带相同的 `generation_id`。客户端不应再从正文中猜测 reasoning，也不应把 reasoning 拼入 `content`。

RPC 方法失败统一使用顶层 `ok: false` 与顶层 `error` 对象。协议 `2.0.0` 不再返回 `ok: true`、`result.ok: false` 的嵌套错误。

HTTP/WebSocket 服务对非 loopback 监听强制要求 JWT secret 或共享 Runtime token。浏览器跨域访问还必须通过 `--allowed-origin` 明确配置完整的 scheme、host 和 port；服务支持 CORS `OPTIONS` 预检。

取消语义：

- 客户端可通过 WebSocket 发送 `runtime.cancel_stream`，参数为 `{"stream_id": "..."}`，Runtime 会取消对应的流式任务并尽量发送 `cancelled` 事件帧。
- 如果 WebSocket 连接直接断开，Runtime 会取消该连接上仍在运行的 stream task，并清理该连接创建的事件订阅。
- SSE `/events` 客户端断开或关闭响应时，Runtime 会关闭对应事件订阅；重复关闭客户端连接不会要求客户端再调用额外 RPC。
- HTTP `/rpc` 请求如果被客户端取消，底层请求协程会随连接取消而收敛；涉及 Runtime resource gate 的方法仍应依赖服务端 `finally` 路径释放资源。
- 多个 Runtime HTTP app / service 实例之间的 stream task、事件订阅、事件队列、shutdown 生命周期和资源状态相互隔离。

生成 token 流本身不能跨 WebSocket 连接续传；可回放的是 `/events` / `runtime.subscribe` 的 Runtime 事件日志。连接断开后，客户端应使用原 `idempotency_key` 调用：

```json
{
  "method": "message.status",
  "params": {
    "agent_id": "agent-1",
    "session_id": "session-1",
    "idempotency_key": "send-018f..."
  }
}
```

- `pending`：服务仍在处理，不要换 key 重发。
- `succeeded`：读取 `result`，并用 `session.messages` 刷新权威会话。
- `failed` / `cancelled`：读取结构化 `error`。重启遗留的未确认请求会收敛为 `message.operation_outcome_unknown`；先读取会话，确认没有结果后再使用新 key。
- 同一个 key 携带不同消息会返回 `message.idempotency_conflict`，不会执行 Provider。

`GET /health` 只表示进程存活；`GET /ready` 和 `runtime.ready` 表示是否接受新请求。服务进入 drain 后 readiness 返回 HTTP `503`，已有操作可在配置的超时内收敛，新操作返回 `runtime.draining`。

## World 多角色编排 API

`world.*` 方法驱动 GensokyoWorld 多角色编排：单模型演一整台戏，由 Director 根据剧情与在场角色决定每轮谁发言、何时交还用户。单角色 Agent 与 World 在同一 RuntimeService 实例上互斥（`world.init` 与 `agent.init` 双向拒绝，错误码 `world.agent_mode_active` / `world.world_mode_active`）；网络多租户进程可同时按 `user_id -> agent_id` 托管 Agent 与 World。

- `world.init`：装配（或恢复）World。参数 `config_path`（可选，网络侧仅 `admin`）、`session_id`（可选，提供时按存档恢复）、`start`（默认 `true`）。返回 `world.state` 同形快照，另附 `resume_diagnostics`（恢复时的 roster/会话差异诊断）。
- `world.start`：开场（幂等）。
- `world.send_message`：参数 `message`、`idempotency_key`；聚合返回 `{world_id, session_id, turns, waiting_for_user, generation_id, idempotent_replay}`，其中 `turns[]` 为 `{actor_id, actor_name, scene_id, content}`，即本段自动表演中各 Actor 的发言记录。
- `world.send_message_stream`：聚合形态同 `world.send_message` 并附完整 `events`；WebSocket 使用增量帧（见下）。
- `world.state`：World 状态快照（`world_id`、`session_id`、`protagonist`、`current_actor_id`、`waiting_for_user`、`stage`、`roster`、`transcript_counts`、`started`、`resume_diagnostics`）。
- `world.roster`：在场角色名单及各自舞台位置（`actor_id`、`name`、`scene_id`、`is_current`）。
- `world.transcript`：共享剧本（公开层，不含导演 reason 与角色私有内容）。参数 `scene_id`（可选，默认用户当前场景）、`limit`（1-500）。
- `world.move`：参数 `scene_id`；把用户移动到指定场景并广播公开过渡事件。
- `world.session.create` / `world.session.list` / `world.session.resume` / `world.session.delete` / `world.session.export`：World 存档管理；`world_id` 省略时使用当前 World。运行中的活动存档拒绝删除（`world.session_active`）。
- `world.shutdown`：保存存档、关停全部 Actor 与 World 总线。

角色权限：`world.state` / `world.roster` / `world.transcript` / `world.session.list` / `world.session.export` 为 `read`，其余 `world.*` 为 `chat`。

WebSocket `world.send_message_stream` 与 `agent.send_message_stream` 使用相同的 ack/task/cancel 机制：先发含 `stream_id` 与 `generation_id` 的确认帧，然后逐事件推送，最后 done 帧（`result` 与 `world.send_message` 同形并附 `events`）。事件帧序列：

```json
{"type": "world.actor.started", "actor_id": "marisa", "actor_name": "雾雨魔理沙", "scene_id": "magic_forest"}
{"type": "world.actor.chunk", "actor_id": "marisa", "content": "……"}
{"type": "world.actor.completed", "actor_id": "marisa", "actor_name": "雾雨魔理沙", "scene_id": "magic_forest", "content": "……"}
{"type": "world.waiting_user"}
{"type": "world.finish", "world_id": "gensokyo", "session_id": "...", "turns": [{"actor_id": "marisa", "actor_name": "雾雨魔理沙", "scene_id": "magic_forest", "content": "……"}], "waiting_for_user": true, "generation_id": "...", "idempotent_replay": false}
```

World 运行时事件（`world.started`、`world.shutdown`、`world.actor.started` / `chunk` / `completed`、`world.director.decision`、`world.scene.moved`、`world.waiting_user`）可通过事件订阅的 `world` 分类订阅，已并入 `runtime_observable`；订阅总线随 World/Agent 模式自动选择。取消与断连语义与 `agent.send_message_stream` 一致：连接断开或 `runtime.cancel_stream` 取消时，操作账本收敛为 `cancelled`，不会留下永久 `pending`。

## 主动定时器 API

主动定时器让 AI 在每次回答完成后决定是否积存一条稍后主动发言意图摘要，并设置触发时间。若用户在触发前发送新消息，或前端取消定时器，Runtime 会直接丢弃旧积存摘要；到点时不再二次判断是否要说话，而是基于仍有效的 `pending_summary`、当前上下文和说话前内部思考重新生成真正发给用户的主动消息。

「要不要主动开口」由对话欲阈值模型决定（2026-07-30 起）：ThinkEngine 用四维心情模型（表达欲/情感驱动力/关系需求/情景相关性）打分并给出候选意图，`total_drive` 超过 `initiative_timer.drive_threshold`（默认 `0.6`）即安排主动发言，否则保持沉默——无累积器、无犹豫复判链、无强制兜底；AI 不想说就是不说。

AI 决定不发言时系统尊重该决定——旧的 `initiative_timer.fallback_on_no_schedule` 强制兜底链、`hesitation_*` 犹豫复判链与 `drive_*` 累积器配置均已删除（旧配置键只会收到迁移警告，不再生效）。

`agent.send_message` 的返回结果和 `agent.send_message_stream` 的 `finish` 事件都会新增 `initiative_timer` 字段；无当前定时器时返回 `null`。

`initiative_timer.current` 获取当前定时器：

```json
{"method": "initiative_timer.current", "params": {}}
```

`initiative_timer.update` 修改当前定时器，可修改触发时间或积存摘要：

```json
{
  "method": "initiative_timer.update",
  "params": {
    "timer_id": "abcd1234",
    "delay_seconds": 180,
    "pending_summary": "我改了一下稍后要说的话。"
  }
}
```

字段规则：

- `timer_id` 可省略；提供时必须匹配当前定时器。
- `delay_seconds` 与 `due_at` 二选一，不可同时提供。
- `pending_summary` 只有在配置 `initiative_timer.allow_frontend_edit_summary` 为 `true` 时可编辑。
- 编辑后 `user_modified` 会变为 `true`，并刷新 `generation`，旧异步任务自动失效。
- `enabled` 是独立于定时器字段的进程内运行时开关（不落盘）：传 `false` 立即废止当前待发计划并彻底停用该 Agent 的主动发言（之后调度在 `config.enabled` 检查处短路，不再产生对话欲评估调用与主动消息），传 `true` 恢复；响应回显 `enabled` 当前值。典型用途：QQ Bot 等没有主动消息投递通道的接入方在 `agent.init` 后调用一次 `{"method": "initiative_timer.update", "params": {"enabled": false}}`（网络路径需同传 `agent_id` 与 `session_id`）。

`initiative_timer.cancel` 取消并丢弃积存摘要：

```json
{"method": "initiative_timer.cancel", "params": {"timer_id": "abcd1234", "reason": "user_cancelled"}}
```

`initiative_timer.trigger` 立即触发当前积存摘要，并返回触发摘要与最终生成结果：

```json
{"method": "initiative_timer.trigger", "params": {"timer_id": "abcd1234"}}
```

`initiative_timer.hesitation` / `initiative_timer.hesitation.set` 已废弃（`legacy`，`remove_after: "3.0.0"`）：犹豫复判链随对话欲阈值模型退役。两个方法仍可用于兼容，但只返回固定的退役提示载荷（`{"enabled": false, "deprecated": true, "remove_after": "3.0.0", ...}`），设置调用被忽略（`ignored: true`），不再读写 Agent 状态或配置文件。

可订阅的事件包括：`initiative_timer.created`、`initiative_timer.updated`、`initiative_timer.cancelled`、`initiative_timer.triggered`、`initiative_timer.discarded`。事件 payload 包含 `timer_id`、`generation`、`status`、`source`、`due_at`、`delay_seconds`、`reason` 和可选 `pending_summary`。其中 `source: "ai"` 表示模型主动设置，`source: "drive"` 表示对话欲路径调度。`initiative_timer.triggered` 只表示定时器有效触发，真正发出的主动消息仍通过 `message.sent` / 主动消息事件暴露 `content`。

相关配置段：

```yaml
initiative_timer:
  enabled: true
  min_delay_seconds: 30
  max_delay_seconds: 1800
  decision_temperature: 0.4
  decision_max_tokens: 180
  max_pending_summary_chars: 240
  allow_frontend_edit_summary: true
  replace_user_modified_timer: true
  expose_pending_summary: true
  drive_threshold: 0.6  # 对话欲阈值（§7.3）：total_drive 超过该值即主动发言
```

`allow_frontend_edit_summary` 是当前推荐字段名；旧配置中的 `allow_frontend_edit_message` 会作为兼容别名映射到它，建议客户端和配置文件逐步迁移到新字段名。旧的 `fallback_*` 强制兜底、`hesitation_*` 犹豫链与 `drive_*` 累积器配置键已删除，旧配置中的这些键只会收到迁移警告，不再生效。

自带控制台 CLI 也提供对应交互入口：

```text
/timer
/timer update delay 120
/timer update due 2026-06-07T21:30:00+08:00
/timer summary 稍后提醒用户继续刚才的话题
/timer cancel
/timer trigger
```

标签命令形式等价：

```text
<timer>summary 稍后提醒用户继续刚才的话题</timer>
<timer>trigger</timer>
```

CLI 命令复用同一套 Agent 定时器能力，不绕过 Runtime / Agent 的状态、失效和触发语义。`/timer hesitation on|off` 会立即更新当前 Agent 状态，并写回配置文件。

## 配置校验 API

`config.validate` 可在不初始化 Agent 的情况下校验配置文件、内联配置和 Runtime overrides：

```json
{
  "method": "config.validate",
  "params": {
    "config": {"model": {"provider": "openai", "temperature": 3}},
    "model_overrides": {"timeout": 60},
    "embedding_overrides": {"dimensions": 1536}
  }
}
```

返回字段：

- `ok`：是否没有 error 级诊断。
- `source`：`inline` 或 `file`。
- `config_path`：文件模式下的配置路径。
- `diagnostics`：每项包含 `code`、`path`、`severity`、`message` 和可选 `suggestion`。
- `error_count` / `warning_count`：错误和警告数量。

Provider 字段矩阵会区分两类兼容性诊断：

- `config.provider.field_discouraged`：字段通常不需要或只适合自定义网关场景，保持 warning。
- `config.provider.field_unsupported` / `config.provider.api_path_unsupported` / `config.provider.web_search_unsupported`：当前 Provider 明确不支持的字段或能力，返回 error。

## 角色校验 API

`character.validate` 可校验角色文件、角色名或内联角色数据：

```json
{
  "method": "character.validate",
  "params": {
    "character_data": {
      "name": "博丽灵梦",
      "system_prompt": "你是博丽灵梦。",
      "greeting": "你好。",
      "example_dialogue": [{"user": "你好", "assistant": "你好呀。"}]
    }
  }
}
```

返回字段：

- `ok`：是否没有 error 级诊断。
- `source`：`inline` 或 `file`。
- `character_path`：文件模式下的角色路径。
- `preview`：角色名、人设长度、示例数量和 metadata 预览。
- `diagnostics` / `error_count` / `warning_count`：结构化诊断信息。

`character.list` 条目也会包含 `ok`、`preview` 和 `diagnostics`，便于客户端在列表中展示坏角色文件。

## 媒体与角色包上传

远程媒体使用 `POST /media?agent_id=...`，请求体为 `multipart/form-data`，文件字段名为 `file`。成功返回稳定 `media_id`、MIME、大小、SHA-256 和当前用户/Agent 归属。随后可在发送参数中引用：

```json
{
  "agent_id": "agent-1",
  "session_id": "session-1",
  "expected_revision": 4,
  "idempotency_key": "send-with-image-1",
  "message": [
    {"type": "text", "text": "看看这张图"},
    {"type": "media", "media_id": "...", "detail": "auto"}
  ]
}
```

当前上传可保存常见图片、音频、PDF 和文本；公开模型输入已完成图片映射，其他 MIME 会明确拒绝，不会把服务器本地路径暴露给客户端。`media.list` / `media.delete` 同样受用户和 Agent 归属控制。文件后端实现 `BlobStore`，生产部署可替换为对象存储。

远程角色包使用管理员端点 `POST /character-packages`，且网络入口必须显式启用远程管理。上传包先执行 zip 路径、大小、manifest、YAML、checksum 等校验。现有 manifest 签名只有格式校验，不会伪装成密码学验签，因此默认返回 `character_package.untrusted`；管理员审阅来源后必须显式传 `allow_untrusted=true` 才能导入。

## 角色包 API

角色包使用 `.gensokyo-character` 扩展名，本质为安全受限的 zip 包，根目录必须包含 `manifest.yaml`，当前格式名为 `gensokyoai.character.package`，schema version 为 `1`。P3 生态规范扩展后，manifest 支持来源、作者主页、许可证链接、引用来源、外部链接、仓库索引元数据、可选签名字段和 `checksums.sha256`。

`character_package.validate` 校验角色包结构、manifest、包内路径安全、文件大小、角色 YAML、资源路径、生态字段、外部链接 URL scheme 和 checksum：

```json
{
  "method": "character_package.validate",
  "params": {"package_path": "packages/reimu.gensokyo-character"}
}
```

`character_package.preview` 返回同一套 diagnostics，并额外面向 UI 使用 manifest 摘要、角色 preview、文件列表、`trust` 和 `security` 摘要。

`character_package.import` 将角色包导入 `characters` 目录：

```json
{
  "method": "character_package.import",
  "params": {
    "package_path": "packages/reimu.gensokyo-character",
    "locale": "zh_cn",
    "overwrite": false
  }
}
```

`character_package.export` 从已有角色 YAML 生成角色包：

```json
{
  "method": "character_package.export",
  "params": {
    "character_path": "characters/zh_cn/HakureiReimu.yaml",
    "output_path": "packages/reimu.gensokyo-character",
    "package_id": "HakureiReimu",
    "author": "GensokyoAI",
    "license": "Apache-2.0",
    "source": "https://example.com/packages/reimu",
    "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
    "external_links": [{"label": "发布页", "url": "https://example.com/packages/reimu", "purpose": "source"}],
    "repository": {"id": "touhou/reimu", "url": "https://example.com/index.json"},
    "signature": {"algorithm": "ed25519", "value": "base64-like-signature-value"},
    "assets": [],
    "overwrite": false
  }
}
```

角色包 API 返回字段：

- `ok`：是否没有 error 级诊断。
- `format` / `schema_version`：角色包格式和 schema version。
- `manifest`：包 ID、名称、版本、作者、许可证、来源、外部链接、签名、checksum、角色入口、资源列表等摘要。
- `preview`：复用角色 YAML 校验预览结构。
- `files`：包内文件路径和大小。
- `trust`：信任元数据摘要，包括作者、来源、许可证、签名、checksum 是否声明，以及外部链接数量。
- `security`：安全摘要，包括外部链接是否均为 `https`、checksum 是否有效、是否存在未声明文件、签名校验级别和声明资源数量。
- `diagnostics` / `error_count` / `warning_count`：结构化诊断信息。
- `imported` / `target_path`：导入结果字段。

生态字段诊断规则：

- `author`、`license`、`source`、`signature`、`checksums` 缺失为 warning，便于客户端导入前展示信任提示。
- `source`、`author_url`、`license_url`、`external_links[].url`、`repository.url`、`repository.homepage`、`repository.download_url` 仅允许 `https` URL；非 `https` 为 error。
- `signature` 当前只做字段格式校验，支持识别 `ed25519`、`rsa-pss-sha256` 和 `minisign`，不做真实加密验签；返回的 `security.signature_verification` 固定为 `format_only`。
- `checksums.sha256` 会对包内文件执行 SHA-256 校验；哈希格式错误、目标缺失或内容不匹配为 error。
- `assets` 中声明的资源必须存在；包内除 `manifest.yaml`、`character` 和 `assets` 声明外的额外文件会产生 `character_package.security.undeclared_file` warning。

## 资源控制

Runtime 资源控制由配置段 `resource_control` 控制。当前 Runtime gate 覆盖入口级与深层执行侧：

- `runtime`：Runtime 高成本入口总并发。
- `agent_message`：当前 Runtime 会话消息并发。
- `stream`：流式消息并发。
- `provider`：ModelClient / Provider 调用链总并发。
- `model`：模型调用并发，覆盖 chat、chat_stream、embeddings 和 image_generation。
- `tool`：ToolExecutor 内置工具与外部工具执行并发。
- `web_search`：`web_search` 工具执行并发。
- `image_generation`：图片生成执行并发。
- `dependency_install`：可选依赖安装并发。

`runtime.info.resource_control` 会返回当前配置摘要和 gate 快照。深层 Provider / 工具调用限流与入口级 gate 使用同一套 `resource.limit_exceeded` 错误结构，错误 details 会包含 `resource`、`reason`、`max_concurrent`、`queue_size`、`active`、`waiting` 和 `action`，便于客户端展示恢复建议。

## 会话消息编辑 API

`session.messages` 分页返回指定会话的可编辑历史消息。网络调用必须传 `agent_id` 和 `session_id`，可传 `limit`（1-500）与 `cursor`：

```json
{
  "method": "session.messages",
  "params": {"agent_id": "agent-1", "session_id": "session-1", "limit": 100, "cursor": null}
}
```

响应包含 `session`、`session_id`、`revision`、当前页 `messages` / `message_count`、`total_message_count`、`has_more` 和 `next_cursor`。每条消息包含稳定 `message_id` 与消息自身 `revision`，并保留 `reasoning_content`、`tool_calls`、`tool_call_id` 和未知扩展字段。

`session.replace_messages` 用于提交编辑后的完整消息数组，可实现编辑、删除、插入任意历史消息：

```json
{
  "method": "session.replace_messages",
  "params": {
    "agent_id": "agent-1",
    "session_id": "session-1",
    "expected_revision": 4,
    "messages": [
      {"role": "user", "content": "改写后的用户消息"},
      {"role": "assistant", "content": "插入或编辑后的助手消息"}
    ]
  }
}
```

校验规则：

- `messages` 必须是数组。
- 每条消息必须是对象，且包含字符串或结构化 content-parts 数组 `content`。
- `role` 仅允许 `system`、`user`、`assistant`、`tool`。
- Runtime 会全量替换目标会话消息，更新 `message_count` / `total_turns`，并同步当前会话工作记忆缓存。

`session.regenerate_from` 从指定消息索引附近重新生成后续助手回复：Runtime 会从 `message_index` 向前找到最近一条 `user` 消息，保留该用户消息之前的历史，将该用户消息重新发送给 Agent，然后返回更新后的完整消息列表。

```json
{
  "method": "session.regenerate_from",
  "params": {
    "agent_id": "agent-1",
    "session_id": "session-1",
    "expected_revision": 4,
    "message_index": 6,
    "system_contexts": ["可选临时系统上下文"]
  }
}
```

响应额外包含：

- `regenerated`：是否完成重生成。
- `from_index`：前端传入的索引。
- `user_message_index`：实际用于重生成的用户消息索引。
- `content`：本次新生成的助手回复。

前端推荐流程：分页读取 `session.messages` 并保存返回的会话 `revision`；编辑、重生成、回滚或发送时提交 `expected_revision`。发生 `session.revision_conflict` 后重新读取权威状态，不要静默覆盖。发送超时后使用原 `idempotency_key` 重试。

自带控制台 CLI 提供同等历史编辑入口：

```text
/history
/history export session_history.json
/history import session_history.json
/history delete 3
/history insert 2 assistant 插入一条助手消息
/history regen 6
```

标签命令形式等价：

```text
<history>import session_history.json</history>
<history>regen 6</history>
```

CLI 的 `/history import`、`/history delete`、`/history insert` 和 `/history regen` 会复用会话管理层的全量替换与持久化能力，保持当前工作记忆、会话消息和 `total_turns` 同步。

## 会话导出与 schema version

`session.export` 返回机器可读会话包，包含：

- `format`：当前为 `gensokyoai.session.export`。
- `version`：保留的兼容字段。
- `schema_version`：导出包 schema version。
- `session_schema_version`：会话文件 schema version。
- `memory_schema_version`：记忆 topic store schema version。
- `session` / `messages` / `message_count`：会话元信息与消息内容。
- `runtime`：导出时 Runtime 的基本路径和启动状态。

## 事件订阅

SSE `/events` 会推送 Runtime 事件。事件字段会经过敏感信息清洗，`api_key`、`authorization`、`token`、`password` 等字段会替换为 `[redacted]`。

租户事件带稳定 `event_id`、单调 `sequence`、`user_id`、`agent_id` 和 `recorded_at`。SSE 必须传 `agent_id`，可通过 `Last-Event-ID` 或 `after_sequence` 恢复；WS `runtime.subscribe` 使用相同的 `after_sequence` / `replay_limit`。事件日志默认每个 Agent 保留最近 10,000 条，单次最多回放 1,000 条；游标早于保留窗口时客户端应重新读取权威 session 状态。

## 记忆管理 API

`memory.list` 列出当前会话语义记忆：

```json
{
  "method": "memory.list",
  "params": {"topic_name": "偏好", "limit": 50, "offset": 0}
}
```

返回包含 `items`、`total`、`limit`、`offset`。每条记忆包含 `id`、`content`、`importance`、`topic`、`topic_name`、`tags`、`memory_type`、`timestamp`。

`memory.search` 搜索当前会话语义记忆：

```json
{
  "method": "memory.search",
  "params": {"query": "喝茶", "top_k": 5, "threshold": 0.7, "include_embedding": true}
}
```

返回每条结果的 `score`、`keyword_score`、可选 `embedding_score`、`matched_by` 与 `diagnostics`。当 embedding provider 未配置、不可用或调用失败时，Runtime 会自动降级到关键词 / 话题检索，并在 `diagnostics.embedding_fallback` 和 `diagnostics.embedding_error` 中说明原因。

`memory.get`、`memory.update`、`memory.delete` 分别按 `memory_id` 读取、更新和删除当前会话语义记忆。`memory.update` 支持更新 `content`、`importance`、`tags`。

`memory.graph` 返回当前会话话题图：

```json
{
  "nodes": [{"id": "topic-1", "name": "偏好", "recall_weight": 0.8}],
  "edges": [],
  "topic_count": 1,
  "edge_count": 0
}
```

## 方法元数据

机器可读方法元数据由代码中的 RPC registry 生成，包含：

- `method`
- `handler`
- `legacy`
- `namespace`
- `deprecated`
- `replacement`
- `remove_after`
- `remote_admin`
- `contract_scope`
- `params_schema` / `result_schema`
- `result_schema_complete`
