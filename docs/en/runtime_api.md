# Runtime API Contract

This document describes the stable JSON RPC contract exposed by the GensokyoAI Runtime to frontends, desktop clients, CLIs, and third-party integrations.

## Versioning and Compatibility

- Current package version: `2026.7.30.0`
- Current protocol version: `2.1.0`
- Current protocol major version: `2`
- Compatibility policy: within the same major version, new fields and methods may be added; removing fields, changing semantics, or changing error structures requires a breaking change.
- Clients should call `runtime.info` first, then decide available features based on `protocol_version`, `capabilities`, `methods`, `legacy_methods`, and `method_specs`.
- The method list in this document should be taken as a guide; the authoritative source is `runtime.info.methods` and `runtime.info.method_specs`. Examples will list the complete non-legacy methods from the current `GensokyoAI.runtime.rpc.RPC_METHOD_SPECS` as much as possible to avoid clients misunderstanding due to subset display.

## Remote Multi-User Resource Model

HTTP, WebSocket, and SSE use `user_id -> agent_id -> session_id -> message_id`. The JWT `sub` claim is the stable `user_id`; clients cannot submit or override it. Equal Agent or session IDs owned by different users are fully isolated.

- `agent.init` and `world.init` accept an optional `agent_id`; the server generates one when omitted (returned in the result). `agent.list` only returns the caller's Agents, and `agent.delete` requires `admin`.
- Network Agent/session RPCs require `agent_id`. Conversation-context operations also require `session_id` and never rely on a process-global current session.
- Session writes require the last observed `expected_revision`. Conflicts return `session.revision_conflict`.
- Message sends also require a 1-128 character `idempotency_key`; retries must reuse it.
- `world.*` multi-character orchestration methods are isolated per `user_id -> agent_id` tenant the same way; `world.send_message` / `world.send_message_stream` require an `idempotency_key` (sharing the `operations.json` ledger with Agent messages, keyed by the World session id). Worlds have no session revision concept, so writes do not require `expected_revision` (the World turn lock already serializes turns).
- Before calling a Provider, the Runtime persists a message operation in `operations.json`. Clients can query `pending`, `succeeded`, `failed`, or `cancelled` through `message.status` with `agent_id`, `session_id`, and `idempotency_key`.
- The trusted local JSON Lines bridge retains single-user implicit-current-session compatibility.

For remote authentication, use a TLS reverse proxy/OIDC gateway to issue short-lived HS256 JWTs. Configure `GENSOKYOAI_RUNTIME_JWT_SECRET` (at least 32 characters), plus optional strict `GENSOKYOAI_RUNTIME_JWT_ISSUER` and `GENSOKYOAI_RUNTIME_JWT_AUDIENCE`. JWTs require `sub` and `exp`, and may carry `read`, `chat`, or `admin` roles. The legacy shared `GENSOKYOAI_RUNTIME_TOKEN` maps to one `runtime-admin` identity and does not provide multi-user identity.

HTTP/WS disables remote administration methods such as `runtime.shutdown`, `dependency.install`, and `character_package.import` / `export` by default. Enable them only on a trusted management network with `GENSOKYOAI_RUNTIME_ALLOW_REMOTE_ADMIN=true` or `--allow-remote-admin`. `GET /info` exposes the effective list through `active_transport.disabled_methods`.

The file backend writes to `runtime_data/users/<user-hash>/agents/<agent-hash>/` and supports a single-node remote multi-user deployment. `BlobStore` is the object-storage replacement boundary. Multi-node deployments still need PostgreSQL for session/Agent metadata and shared object storage for blobs; the file backend does not claim multi-node consistency.

## Discovery Interface

`runtime.info` returns runtime metadata:

```json
{
  "name": "GensokyoAI Runtime",
  "package_version": "2026.7.30.0",
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

`method_specs[].params_schema` is generated for the active call context. HTTP/WS returns `contract_scope: "network"` and includes the additional required `agent_id`, `session_id`, `expected_revision`, and `idempotency_key` fields imposed by remote resource routing. `result_schema_complete` is `true` only for explicitly modeled response shapes; clients must not treat a broad dictionary schema as a complete contract when it is `false`.

Non-legacy methods grouped by current namespace:

- `runtime.info`, `runtime.health`, `runtime.ready`, `runtime.shutdown`
- `config.validate`
- `character.validate`, `character.list`
- `character_package.validate`, `character_package.preview`, `character_package.import`, `character_package.export`
- `agent.init`, `agent.list`, `agent.delete`, `agent.send_message`, `agent.send_message_stream`
- `message.status`
- `model.list`, `model.info`
- `session.create`, `session.list`, `session.current`, `session.resume`, `session.delete`, `session.export`, `session.rename`, `session.messages`, `session.replace_messages`, `session.regenerate_from`, `session.rollback`
- `dependency.status`, `dependency.install`
- `external_tool.status`
- `initiative_timer.current`, `initiative_timer.update`, `initiative_timer.cancel`, `initiative_timer.trigger` (`initiative_timer.hesitation` / `initiative_timer.hesitation.set` are deprecated legacy)
- `memory.list`, `memory.search`, `memory.get`, `memory.update`, `memory.delete`, `memory.graph`
- `media.list`, `media.delete`
- `scene.current`, `scene.list`, `scene.get`, `scene.switch`, `scene.graph`

Legacy compatibility methods remain available but are deprecated: `init`, `send_message`, `send_message_stream`, `list_characters`, `create_session`, `list_sessions`, `current_session`, `resume_session`, `delete_session`, `export_session`, `rename_session`, `rollback_session`, `shutdown`, `dependency_status`, `install_dependencies`, `external_tool_status`. New clients should migrate to namespaced methods according to `method_specs[].replacement`.

## Runtime Versioning and Migration Diagnostics

`runtime.info` exposes package version, Runtime version, and schema version summary:

- `package_version`: current GensokyoAI package / project version; read from installed package metadata, falling back to `pyproject.toml` when running from source.
- `protocol_version` / `protocol_major_version`: Runtime RPC protocol version.
- `schema_versions.config`: configuration schema version.
- `schema_versions.session`: session file schema version.
- `schema_versions.memory`: memory topic store schema version.
- `schema_versions.session_export`: session export package schema version.
- `schema_versions.character_package`: character package schema version; current `.gensokyo-character` format is `1`.
- `deprecated_methods`: deprecated RPC methods and their replacements.
- `deprecated_fields`: deprecated fields; currently empty array.
- `compatibility_notes`: compatibility notes; currently includes the note that legacy non-namespaced RPC methods remain compatible but should be migrated to namespaced methods.

`runtime.info.migration_diagnostics` returns recent migration summary:

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

Migration diagnostic field descriptions:

- `source`: migration source, e.g. `session` or `memory.topic_store`.
- `status`: migration status; currently produces `migrated` and `failed`; `skipped` is a reserved count.
- `from_schema_version` / `to_schema_version`: schema version before and after migration; old unversioned formats are `null`.
- `format`: target format name after migration.
- `path`: path of the migrated file.
- `backup_path`: pre-migration backup path; automatic memory schema 1→2 migration creates a `.bak` before rewriting. On failure, keep both source and backup, then repair or roll back according to diagnostics.
- `message`: human-readable summary.
- `diagnostics`: structured diagnostics list; includes stable `code`, `severity`, `message`, and repair suggestions on failure.
- `migrated_at`: migration diagnostic record time.

## RPC Request Format

HTTP `/rpc` and WebSocket ordinary RPC use the same request format:

```json
{
  "id": "request-1",
  "method": "runtime.health",
  "params": {}
}
```

- `id`: client-defined request identifier, can be string or number.
- `method`: method name; use namespaced new method names.
- `params`: object; pass `{}` or omit when there are no parameters.

## Success Response Format

```json
{
  "id": "request-1",
  "ok": true,
  "result": {}
}
```

## Error Response Format

```json
{
  "id": "request-1",
  "ok": false,
  "error": {
    "code": "method_not_found",
    "error_code": "method_not_found",
    "message": "Requested Runtime RPC method does not exist.",
    "technical_message": "Unknown method: bad.method",
    "user_message": "Requested Runtime RPC method does not exist.",
    "recoverable": true,
    "action_hint": "Please use a method listed in runtime.info.methods or legacy_methods.",
    "details": {}
  }
}
```

Clients should branch on `code` or `error_code`; do not parse natural language `message`.

When resource control is triggered, `resource.limit_exceeded` is returned:

```json
{
  "ok": false,
  "error": {
    "code": "resource.limit_exceeded",
    "error_code": "resource.limit_exceeded",
    "message": "Runtime is currently busy, please retry later.",
    "recoverable": true,
    "action_hint": "Please retry later, or increase the corresponding concurrency / queue configuration in resource_control.",
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

## WebSocket Streaming Frames

RuntimeService currently provides two streaming consumption forms:

- `iter_message_stream()`: async iterator that produces Runtime events immediately as Agent streaming chunks progress; WebSocket `/ws` `agent.send_message_stream` uses this form to push frame by frame.
- `send_message_stream()`: aggregated form that collects complete `events` and returns them at once; JSON Lines RPC and HTTP `POST /rpc` use this form to maintain one-request-one-response compatibility.

WebSocket client sends:

```json
{
  "id": "stream-1",
  "method": "agent.send_message_stream",
  "params": {
    "agent_id": "agent-1",
    "session_id": "session-1",
    "expected_revision": 4,
    "idempotency_key": "send-018f...",
    "message": "Hello"
  }
}
```

The server first returns a start confirmation frame containing the assigned `stream_id` and `generation_id`:

```json
{
  "id": "stream-1",
  "ok": true,
  "result": {"stream_id": "...", "generation_id": "..."}
}
```

Then event frames are returned as generation progresses:

```json
{
  "id": "stream-1",
  "ok": true,
  "stream_id": "...",
  "generation_id": "...",
  "event": {"type": "content", "index": 0, "content": "...", "generation_id": "..."}
}
```

End frame:

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
      "pending_summary": "I was just thinking about that again...",
      "reason": "Character wants to add something later",
      "user_modified": false,
      "editable_fields": ["due_at", "delay_seconds", "pending_summary"]
    }
  }
}
```

`reasoning_content` is public by default in protocol `2.0.0`. Reasoning-only chunks use `type: "reasoning"`; answer chunks use `type: "content"`. Both carry the same `generation_id`, and clients must not merge reasoning into `content`.

RPC method failures use the top-level `ok: false` envelope and top-level `error` object. Protocol `2.0.0` no longer returns nested `ok: true`, `result.ok: false` failures.

Non-loopback HTTP/WebSocket binding requires either a JWT secret or a shared Runtime token. Browser access also requires an exact scheme/host/port entry through `--allowed-origin`; CORS `OPTIONS` preflight is supported.

Cancellation semantics:

- Clients can send `runtime.cancel_stream` via WebSocket with parameter `{"stream_id": "..."}`; the Runtime will cancel the corresponding streaming task and attempt to send a `cancelled` event frame.
- If the WebSocket connection is directly disconnected, the Runtime will cancel stream tasks still running on that connection and clean up event subscriptions created by that connection.
- When SSE `/events` clients disconnect or close the response, the Runtime will close the corresponding event subscription; repeatedly closing client connections does not require clients to call additional RPCs.
- If an HTTP `/rpc` request is cancelled by the client, the underlying request coroutine will converge as the connection is cancelled; methods involving Runtime resource gates should still rely on server-side `finally` paths to release resources.
- Stream tasks, event subscriptions, event queues, shutdown lifecycle, and resource states are isolated between multiple Runtime HTTP app / service instances.

The generation token stream itself cannot resume across WebSocket connections. Replay applies to the Runtime event log consumed through `/events` or `runtime.subscribe`. After a disconnect, query the original operation:

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

- `pending`: processing is still active; do not resend with another key.
- `succeeded`: consume `result`, then refresh authoritative state through `session.messages`.
- `failed` / `cancelled`: inspect the structured `error`. An unconfirmed request left by a restart converges to `message.operation_outcome_unknown`; reread the session before sending again with a new key.
- Reusing a key for a different message returns `message.idempotency_conflict` without calling the Provider.

`GET /health` only reports process liveness. `GET /ready` and `runtime.ready` report whether new work is accepted. During drain, readiness returns HTTP `503`, existing operations are allowed to settle up to the configured timeout, and new work fails with `runtime.draining`.

## World Multi-Character Orchestration API

The `world.*` methods drive GensokyoWorld multi-character orchestration: one model performs an entire play, and the Director decides who speaks each turn, when to switch, and when to hand the mic back to the user. A single-character Agent and a World are mutually exclusive within one RuntimeService instance (`world.init` and `agent.init` reject each other with `world.agent_mode_active` / `world.world_mode_active`); a multi-tenant process can host Agents and Worlds side by side per `user_id -> agent_id`.

- `world.init`: assembles (or resumes) a World. Params: `agent_id` (optional tenant slot; generated when omitted), `config_path` (optional, `admin` only over the network), `session_id` (optional; resumes that archive when provided), `start` (default `true`). Returns the same snapshot as `world.state` plus `resume_diagnostics` (roster/session differences found during resume).
- `world.start`: opening beat (idempotent).
- `world.send_message`: params `message`, `idempotency_key`; returns `{world_id, session_id, turns, waiting_for_user, generation_id, idempotent_replay}` where `turns[]` are `{actor_id, actor_name, scene_id, content}` — the speeches of this automatic performance segment.
- `world.send_message_stream`: aggregate form identical to `world.send_message` plus the full `events` list; WebSocket uses incremental frames (see below).
- `world.state`: World snapshot (`world_id`, `session_id`, `protagonist`, `current_actor_id`, `waiting_for_user`, `stage`, `roster`, `transcript_counts`, `started`, `resume_diagnostics`).
- `world.roster`: cast list with stage positions (`actor_id`, `name`, `scene_id`, `is_current`).
- `world.transcript`: shared transcript (public layer only; no Director reasoning or private character content). Params `scene_id` (optional, defaults to the user's current scene), `limit` (1-500).
- `world.move`: param `scene_id`; moves the user to the given scene and broadcasts a public transition event.
- `world.session.create` / `world.session.list` / `world.session.resume` / `world.session.delete` / `world.session.export`: World session management; `world_id` defaults to the current World. The active running session is refused for deletion (`world.session_active`).
- `world.shutdown`: saves the session, then shuts down every Actor and the World event bus.

Role permissions: `world.state` / `world.roster` / `world.transcript` / `world.session.list` / `world.session.export` require `read`; all other `world.*` methods require `chat`.

WebSocket `world.send_message_stream` uses the same ack/task/cancel mechanics as `agent.send_message_stream`: an acknowledgement frame with `stream_id` and `generation_id`, then per-event frames, then a done frame (`result` matches `world.send_message` plus `events`). The event frame sequence:

```json
{"type": "world.actor.started", "actor_id": "marisa", "actor_name": "Marisa Kirisame", "scene_id": "magic_forest"}
{"type": "world.actor.chunk", "actor_id": "marisa", "content": "..."}
{"type": "world.actor.completed", "actor_id": "marisa", "actor_name": "Marisa Kirisame", "scene_id": "magic_forest", "content": "..."}
{"type": "world.waiting_user"}
{"type": "world.finish", "world_id": "gensokyo", "session_id": "...", "turns": [{"actor_id": "marisa", "actor_name": "Marisa Kirisame", "scene_id": "magic_forest", "content": "..."}], "waiting_for_user": true, "generation_id": "...", "idempotent_replay": false}
```

World runtime events (`world.started`, `world.shutdown`, `world.actor.started` / `chunk` / `completed`, `world.director.decision`, `world.scene.moved`, `world.waiting_user`) can be subscribed through the `world` event category and are included in `runtime_observable`; the subscription bus is chosen automatically for World vs Agent mode. Cancellation and disconnect semantics match `agent.send_message_stream`: on disconnect or `runtime.cancel_stream`, the operation ledger converges to `cancelled` instead of staying `pending` forever.

## Initiative Timer API

The initiative timer allows the AI to decide after each reply whether to store a brief summary of something it wants to say later and set a trigger time. If the user sends a new message before the trigger, or the frontend cancels the timer, the Runtime directly discards the old stored summary; when the time is reached, it does not re-judge whether to speak, but regenerates the actual proactive message to the user based on the still-valid `pending_summary`, current context, and pre-speech internal thinking.

Whether to speak up is decided by the speaking-drive threshold model (since 2026-07-30): the ThinkEngine scores four mood dimensions (expression drive / emotional charge / relational need / situational relevance) and produces a candidate intent; when `total_drive` exceeds `initiative_timer.drive_threshold` (default `0.6`), a proactive message is scheduled, otherwise the character stays silent—no accumulator, no hesitation re-judgment chain, no forced fallback; when the AI does not want to speak, it does not speak.

The old `initiative_timer.fallback_on_no_schedule` forced fallback chain, the `hesitation_*` re-judgment chain, and the `drive_*` accumulator settings have all been removed (legacy config keys only produce a migration warning and no longer take effect).

Both `agent.send_message` return results and `agent.send_message_stream` `finish` events include an `initiative_timer` field; when there is no current timer, they return `null`.

`initiative_timer.current` gets the current timer:

```json
{"method": "initiative_timer.current", "params": {}}
```

`initiative_timer.update` modifies the current timer, including trigger time or stored summary:

```json
{
  "method": "initiative_timer.update",
  "params": {
    "timer_id": "abcd1234",
    "delay_seconds": 180,
    "pending_summary": "I changed what I want to say later."
  }
}
```

Field rules:

- `timer_id` is optional; if provided it must match the current timer.
- `delay_seconds` and `due_at` are mutually exclusive.
- `pending_summary` is editable only when `initiative_timer.allow_frontend_edit_summary` is `true`.
- After editing, `user_modified` becomes `true` and `generation` is refreshed; old async tasks automatically become invalid.
- `enabled` is an in-process runtime switch independent of the timer fields (never persisted): `false` immediately discards any pending plan and fully disables initiative speaking for that Agent (subsequent scheduling short-circuits at the `config.enabled` check — no more drive-evaluation calls or initiative messages), `true` re-enables it; the response echoes the current `enabled` value. Typical use: integrations without a proactive-message delivery channel (e.g. a QQ bot) call `{"method": "initiative_timer.update", "params": {"enabled": false}}` once after `agent.init` (the network path also requires `agent_id` and `session_id`).

`initiative_timer.cancel` cancels and discards the stored summary:

```json
{"method": "initiative_timer.cancel", "params": {"timer_id": "abcd1234", "reason": "user_cancelled"}}
```

`initiative_timer.trigger` immediately triggers the current stored summary and returns the triggered summary and final generated result:

```json
{"method": "initiative_timer.trigger", "params": {"timer_id": "abcd1234"}}
```

`initiative_timer.hesitation` / `initiative_timer.hesitation.set` are deprecated (`legacy`, `remove_after: "3.0.0"`): the hesitation chain is retired with the speaking-drive threshold model. Both methods remain callable for compatibility but return a fixed retirement payload (`{"enabled": false, "deprecated": true, "remove_after": "3.0.0", ...}`); set calls are ignored (`ignored: true`) and no longer touch Agent state or config files.

Subscribable events include: `initiative_timer.created`, `initiative_timer.updated`, `initiative_timer.cancelled`, `initiative_timer.triggered`, `initiative_timer.discarded`. Event payloads contain `timer_id`, `generation`, `status`, `source`, `due_at`, `delay_seconds`, `reason`, and optional `pending_summary`. `source: "ai"` means the model actively set it, and `source: "drive"` means scheduled via the drive model. `initiative_timer.triggered` only means the timer was effectively triggered; the actual proactive message sent is still exposed through `message.sent` / proactive message events with `content`.

Related configuration section:

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
  drive_threshold: 0.6  # speaking-drive threshold (§7.3): speak when total_drive exceeds it
```

`allow_frontend_edit_summary` is the currently recommended field name; the old config `allow_frontend_edit_message` is still read as a compatibility alias, but clients and config files are recommended to gradually migrate to the new field name. The old `fallback_*` forced fallback, `hesitation_*` hesitation-chain, and `drive_*` accumulator config keys have been removed; legacy keys in existing configs only receive a migration warning and no longer take effect.

The built-in console CLI also provides corresponding interactive entry points:

```text
/timer
/timer update delay 120
/timer update due 2026-06-07T21:30:00+08:00
/timer summary Remind the user to continue the previous topic later
/timer cancel
/timer trigger
```

Equivalent tag command form:

```text
<timer>summary Remind the user to continue the previous topic later</timer>
<timer>trigger</timer>
```

CLI commands reuse the same Agent timer capability, without bypassing Runtime / Agent state, invalidation, and trigger semantics. `/timer hesitation on|off` immediately updates the current Agent state and writes back to the config file.

## Configuration Validation API

`config.validate` validates configuration files, inline configuration, and Runtime overrides without initializing the Agent:

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

Return fields:

- `ok`: whether there are no error-level diagnostics.
- `source`: `inline` or `file`.
- `config_path`: config path in file mode.
- `diagnostics`: each item contains `code`, `path`, `severity`, `message`, and optional `suggestion`.
- `error_count` / `warning_count`: number of errors and warnings.

Provider field matrix distinguishes two types of compatibility diagnostics:

- `config.provider.field_discouraged`: fields usually unnecessary or only suitable for custom gateway scenarios, kept as warning.
- `config.provider.field_unsupported` / `config.provider.api_path_unsupported` / `config.provider.web_search_unsupported`: fields or capabilities explicitly unsupported by the current provider, returned as error.

## Character Validation API

`character.validate` can validate character files, character names, or inline character data:

```json
{
  "method": "character.validate",
  "params": {
    "character_data": {
      "name": "Reimu Hakurei",
      "system_prompt": "You are Reimu Hakurei.",
      "greeting": "Hello.",
      "example_dialogue": [{"user": "Hello", "assistant": "Hello there."}]
    }
  }
}
```

Return fields:

- `ok`: whether there are no error-level diagnostics.
- `source`: `inline` or `file`.
- `character_path`: character path in file mode.
- `preview`: preview of name, persona length, example count, and metadata.
- `diagnostics` / `error_count` / `warning_count`: structured diagnostic information.

`character.list` entries also include `ok`, `preview`, and `diagnostics`, making it easy for clients to display broken character files in the list.

## Media and Character Package Uploads

Remote media uses `POST /media?agent_id=...` with a `multipart/form-data` file field named `file`. The response contains a stable `media_id`, MIME type, size, SHA-256, and ownership. Reference it from a message content-parts array with `{"type":"media","media_id":"..."}`. Images are mapped to provider-neutral multimodal input; other stored MIME types are currently rejected as model input. `media.list` and `media.delete` enforce the same user/Agent ownership. The file backend implements the replaceable `BlobStore` boundary.

Remote character packages use the admin-only `POST /character-packages` endpoint and require remote administration to be explicitly enabled on the network transport. The upload is checked for zip path safety, size, manifest, YAML, and checksums before import. Current manifest signatures are format-checked rather than cryptographically verified, so remote import fails closed with `character_package.untrusted` unless an administrator explicitly supplies `allow_untrusted=true` after reviewing provenance.

## Character Package API

Character packages use the `.gensokyo-character` extension. They are essentially security-restricted zip archives; the root directory must contain `manifest.yaml`. The current format name is `gensokyoai.character.package` and the schema version is `1`. After the P3 ecosystem specification expansion, the manifest supports source, author homepage, license link, attribution, external links, repository index metadata, optional signature field, and `checksums.sha256`.

`character_package.validate` validates character package structure, manifest, internal path safety, file size, character YAML, resource paths, ecosystem fields, external link URL schemes, and checksum:

```json
{
  "method": "character_package.validate",
  "params": {"package_path": "packages/reimu.gensokyo-character"}
}
```

`character_package.preview` returns the same diagnostics, plus UI-oriented manifest summary, character preview, file list, `trust`, and `security` summaries.

`character_package.import` imports a character package into the `characters` directory:

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

`character_package.export` generates a character package from an existing character YAML:

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
    "external_links": [{"label": "Release page", "url": "https://example.com/packages/reimu", "purpose": "source"}],
    "repository": {"id": "touhou/reimu", "url": "https://example.com/index.json"},
    "signature": {"algorithm": "ed25519", "value": "base64-like-signature-value"},
    "assets": [],
    "overwrite": false
  }
}
```

Character package API return fields:

- `ok`: whether there are no error-level diagnostics.
- `format` / `schema_version`: character package format and schema version.
- `manifest`: summary of package ID, name, version, author, license, source, external links, signature, checksum, character entry, resource list, etc.
- `preview`: reuses character YAML validation preview structure.
- `files`: file paths and sizes inside the package.
- `trust`: trust metadata summary, including whether author, source, license, signature, and checksum are declared, and the number of external links.
- `security`: security summary, including whether all external links use `https`, whether checksum is valid, whether undeclared files exist, signature verification level, and declared resource count.
- `diagnostics` / `error_count` / `warning_count`: structured diagnostic information.
- `imported` / `target_path`: import result fields.

Ecosystem field diagnostic rules:

- Missing `author`, `license`, `source`, `signature`, or `checksums` produces warnings so clients can show trust prompts before import.
- `source`, `author_url`, `license_url`, `external_links[].url`, `repository.url`, `repository.homepage`, `repository.download_url` only allow `https` URLs; non-`https` is an error.
- `signature` currently only validates field format, supporting recognition of `ed25519`, `rsa-pss-sha256`, and `minisign`, without real cryptographic verification; returned `security.signature_verification` is always `format_only`.
- `checksums.sha256` performs SHA-256 verification of files inside the package; hash format errors, missing targets, or content mismatches are errors.
- Resources declared in `assets` must exist; extra files inside the package besides `manifest.yaml`, `character`, and `assets` declarations produce `character_package.security.undeclared_file` warnings.

## Resource Control

Runtime resource control is governed by the `resource_control` configuration section. Current Runtime gates cover entry-level and deep execution sides:

- `runtime`: total concurrency for high-cost Runtime entry points.
- `agent_message`: current Runtime session message concurrency.
- `stream`: streaming message concurrency.
- `provider`: total concurrency for ModelClient / Provider call chains.
- `model`: model call concurrency, covering chat, chat_stream, embeddings, and image_generation.
- `tool`: ToolExecutor built-in tool and external tool execution concurrency.
- `web_search`: `web_search` tool execution concurrency.
- `image_generation`: image generation execution concurrency.
- `dependency_install`: optional dependency installation concurrency.

`runtime.info.resource_control` returns the current configuration summary and gate snapshot. Deep Provider / tool call rate limiting and entry-level gates use the same `resource.limit_exceeded` error structure; error details include `resource`, `reason`, `max_concurrent`, `queue_size`, `active`, `waiting`, and `action`, making it easy for clients to display recovery suggestions.

## Session Message Editing API

`session.messages` returns a stable page of editable history. Network calls require `agent_id` and `session_id`, and accept `limit` (1-500) plus `cursor`:

```json
{
  "method": "session.messages",
  "params": {"agent_id": "agent-1", "session_id": "session-1", "limit": 100, "cursor": null}
}
```

The response contains `session`, `session_id`, session `revision`, page `messages` / `message_count`, `total_message_count`, `has_more`, and `next_cursor`. Every message has a stable `message_id` and its own `revision`, while preserving `reasoning_content`, tool fields, and unknown extensions.

`session.replace_messages` is used to submit the edited complete message array, enabling editing, deleting, or inserting any historical message:

```json
{
  "method": "session.replace_messages",
  "params": {
    "agent_id": "agent-1",
    "session_id": "session-1",
    "expected_revision": 4,
    "messages": [
      {"role": "user", "content": "Rewritten user message"},
      {"role": "assistant", "content": "Inserted or edited assistant message"}
    ]
  }
}
```

Validation rules:

- `messages` must be an array.
- Each message must be an object containing text or a structured content-parts array in `content`.
- `role` only allows `system`, `user`, `assistant`, `tool`.
- Runtime fully replaces target session messages, updates `message_count` / `total_turns`, and synchronizes the current session working memory cache.

`session.regenerate_from` regenerates subsequent assistant replies from near the specified message index: Runtime finds the most recent `user` message from `message_index` backward, preserves history before that user message, resends that user message to the Agent, and returns the updated complete message list.

```json
{
  "method": "session.regenerate_from",
  "params": {
    "agent_id": "agent-1",
    "session_id": "session-1",
    "expected_revision": 4,
    "message_index": 6,
    "system_contexts": ["Optional temporary system context"]
  }
}
```

The response additionally contains:

- `regenerated`: whether regeneration was completed.
- `from_index`: index passed by the frontend.
- `user_message_index`: actual user message index used for regeneration.
- `content`: the newly generated assistant reply.

Recommended frontend flow: page through `session.messages` and retain the returned session `revision`; submit it as `expected_revision` for edits, regeneration, rollback, and sends. On `session.revision_conflict`, reread authoritative state instead of overwriting it. Reuse the original `idempotency_key` after a send timeout.

The built-in console CLI provides equivalent history editing entry points:

```text
/history
/history export session_history.json
/history import session_history.json
/history delete 3
/history insert 2 assistant Insert an assistant message
/history regen 6
```

Equivalent tag command form:

```text
<history>import session_history.json</history>
<history>regen 6</history>
```

CLI `/history import`, `/history delete`, `/history insert`, and `/history regen` reuse the session management layer's full replacement and persistence capabilities, keeping current working memory, session messages, and `total_turns` synchronized.

## Session Export and Schema Version

`session.export` returns a machine-readable session package containing:

- `format`: currently `gensokyoai.session.export`.
- `version`: reserved compatibility field.
- `schema_version`: export package schema version.
- `session_schema_version`: session file schema version.
- `memory_schema_version`: memory topic store schema version.
- `session` / `messages` / `message_count`: session metadata and message content.
- `runtime`: basic path and startup state of the Runtime at export time.

## Event Subscription

Tenant events carry stable `event_id`, monotonic `sequence`, `user_id`, `agent_id`, and `recorded_at`. SSE requires `agent_id` and resumes through `Last-Event-ID` or `after_sequence`; WS `runtime.subscribe` accepts the same `after_sequence` / `replay_limit`. Each Agent retains the latest 10,000 events and replays at most 1,000 per request. When a cursor predates retention, reread authoritative session state.

SSE `/events` pushes Runtime events. Event fields are sanitized for sensitive information; fields such as `api_key`, `authorization`, `token`, `password` are replaced with `[redacted]`.

## Memory Management API

`memory.list` lists current session semantic memories:

```json
{
  "method": "memory.list",
  "params": {"topic_name": "Preferences", "limit": 50, "offset": 0}
}
```

Returns `items`, `total`, `limit`, `offset`. Each memory contains `id`, `content`, `importance`, `topic`, `topic_name`, `tags`, `memory_type`, `timestamp`.

`memory.search` searches current session semantic memories:

```json
{
  "method": "memory.search",
  "params": {"query": "tea", "top_k": 5, "threshold": 0.7, "include_embedding": true}
}
```

Returns `score`, `keyword_score`, optional `embedding_score`, `matched_by`, and `diagnostics` for each result. When the embedding provider is not configured, unavailable, or call fails, Runtime automatically falls back to keyword / topic retrieval, explaining the reason in `diagnostics.embedding_fallback` and `diagnostics.embedding_error`.

`memory.get`, `memory.update`, `memory.delete` respectively read, update, and delete current session semantic memories by `memory_id`. `memory.update` supports updating `content`, `importance`, `tags`.

`memory.graph` returns the current session topic graph:

```json
{
  "nodes": [{"id": "topic-1", "name": "Preferences", "recall_weight": 0.8}],
  "edges": [],
  "topic_count": 1,
  "edge_count": 0
}
```

## Method Metadata

Machine-readable method metadata is generated from the RPC registry in code and contains:

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
