# GensokyoAI Changelog 模板

> 定位：这个 changelog 面向普通用户、客户端开发者和集成方公开发布。
>
> 写法原则：先讲用户能感知到的变化，再补充开发者需要的兼容、迁移和协议信息。普通用户应该能看懂“升级后有什么变化、会不会影响旧数据、需要不需要手动处理”。

## 版本号规则

版本号格式、哪些地方带 `v`、哪些地方不带 `v`、Runtime 协议版本和 schema version 的区别，统一见 [`versioning.md`](versioning.md)。

简要规则：

- 对外版本和 changelog 使用 `vYYYY.M.D.N`；当前正式 release 为 `v2026.7.30.0`，首个正式 release 是 `v2026.5.13.0`。
- [`pyproject.toml`](../pyproject.toml) 中的 package version 不带 `v`，当前为 `2026.7.30.0`。
- Runtime protocol version 使用独立语义版本且不带 `v`，当前开发分支为 `2.0.0`、major 为 `2`；客户端兼容性优先看 `protocol_major_version`。
- schema version 继续使用整数，例如 `1`、`2`、`3`。

## 使用方式

每次发布新版本时，复制下面的版本模板，放到本文件顶部，并把未使用的小节删除或写成“无”。

建议每条变更都尽量包含：

- 发生了什么变化。
- 对普通用户有什么影响。
- 是否需要手动操作。
- 是否影响旧配置、旧会话、旧记忆、角色文件或客户端集成。
- 是否改变 Runtime methods、capabilities、返回字段、错误结构或 schema version。
- 是否涉及 deprecated、removal_pending、removed 或 breaking changes；如果涉及，必须给出替代方案和迁移方式。

---

## 当前开发分支兼容性摘要

- Runtime Protocol `2.0.0` 集中执行一次不兼容修正：网络资源改为 `user_id -> agent_id -> session_id`，会话写入要求 `expected_revision`，消息发送要求 `idempotency_key`。
- 新增 JWT/RBAC、租户存储隔离、稳定 `message_id`、分页、事件持久化回放、媒体 multipart 上传和角色包远程上传信任门槛。
- 消息幂等升级为 Provider 调用前持久化的操作账本，新增 `message.status` 断线状态查询；生成 token 流明确不支持跨连接续传，客户端通过操作状态和权威会话恢复。
- 新增 readiness、优雅 drain 和默认关闭的 HTTP/WS 远程管理面；网络 `method_specs` 会声明真实资源必填字段及结果 schema 完整度。
- Session schema 从 `1` 升到 `2`；旧文件自动补 `revision`、`message_id` 和消息 revision，并保留未知扩展字段。
- 默认文件后端定位为单节点远程多用户；多节点生产部署仍要求 PostgreSQL 和共享对象存储。
- Runtime Protocol `2.1.0` 新增 `world.*` 多角色编排方法（14 个）与 capability `world.orchestration`；World 会话使用独立的 `world session schema v1`；`world.send_*` 复用消息幂等账本。协议 major 保持 `2`，客户端无迁移负担。
- 主动定时器强制兜底链（`fallback_on_no_schedule` 等 4 键）已移除，旧键仅警告迁移；Runtime 事件脱敏占位符统一为 `[REDACTED]`。
- 对话欲按 2026-07-30 用户定稿重构：主动发言改由 ThinkEngine 四维心情打分 + `initiative_timer.drive_threshold`（默认 0.6）阈值判定，无累积器；`hesitation_*` 配置键与 `drive_*` 累积键退役（仅警告迁移）；`initiative_timer.hesitation` / `initiative_timer.hesitation.set` 转为 legacy/deprecated（`remove_after: "3.0.0"`，仍可从 `legacy_methods` 发现，调用返回退役载荷）；定时器事件 payload 移除 `hesitation_*` 字段。ActionPlanner 的 INITIATIVE_SPEAK 动作 content 语义改为「待表达意图摘要」，执行时统一走说话前思考 + 即时生成管线，预写话术不再直接发送；一切主动开口统一经 INITIATIVE_SPEAK 动作出口，SPEAK 仅服务回复用户。
- World 模式的所有存储统一收进 `sessions/world/<world_id>/` 命名空间：World 存档（`<session>.json`）、actor 私有会话（`<角色名>/`）、语义记忆（`memory/<角色名>/`）同树管理；此前分散在 `sessions/worlds/`、`sessions/<角色名>/`、`sessions/memory/world_<id>/` 三处。单角色模式路径不受影响。

---

## 版本模板

```markdown
# GensokyoAI vYYYY.M.D.N 更新日志

发布日期：YYYY-MM-DD

## 一句话总结

用一两句话说明这个版本最重要的变化，尽量让普通用户能快速判断是否需要升级。

## 新增功能

- 新增：说明新增能力。
  - 对用户的影响：说明用户能做什么新事情。
  - 是否需要操作：说明是否需要改配置、重启、重新安装依赖。

## 行为变化

- 变化：说明已有功能的行为变化。
  - 对用户的影响：说明升级后体验有什么不同。
  - 兼容性：说明旧用法是否还能继续用；如果只是 warning 收紧为 error，需要写明受影响配置。
  - 客户端影响：说明 Runtime 返回字段、错误码、capabilities 或方法列表是否变化。

## 修复问题

- 修复：说明修复的问题。
  - 受影响场景：说明哪些用户可能遇到过。
  - 升级建议：说明是否建议相关用户升级。

## 已废弃但仍兼容

- 废弃：说明不推荐继续使用的配置、字段、RPC 方法或文件格式。
  - 废弃对象：写完整路径或方法名，例如 `runtime.info.old_field` 或 `legacy_method`。
  - 生效版本：写不带 `v` 的版本号，例如 `2026.5.11.0`。
  - 替代方案：说明应该改用什么；没有替代方案时说明原因。
  - 预计移除：说明 `remove_after`，无法确定时写“暂未确定”。
  - Runtime 声明：说明是否已写入 `runtime.info.deprecated_methods` 或 `runtime.info.deprecated_fields`。

## 已移除或破坏性变化

- 移除：说明不再支持的能力。
  - 影响范围：说明会影响哪些用户或客户端。
  - 破坏性级别：说明是否需要递增 `RUNTIME_PROTOCOL_MAJOR_VERSION` 或 schema version。
  - 迁移方式：说明如何改配置、改调用方式或迁移数据。
  - Runtime 声明：说明是否已写入 `runtime.info.breaking_changes`。

## 数据迁移与升级提醒

- 数据迁移：说明本次是否会迁移会话、记忆、配置或角色包。
  - 自动迁移：说明程序会自动处理什么。
  - 备份位置：说明备份文件或导出包在哪里。
  - 失败处理：说明如果迁移失败，用户应该看哪里、怎么恢复。

## 安装与依赖变化

- 依赖变化：说明 Python 版本、pip / uv 安装、Provider SDK、Ollama 或系统依赖是否变化。
  - 普通用户操作：说明是否需要重新安装依赖。
  - Windows 用户提醒：如涉及脚本、路径或权限问题，在这里说明。

## Runtime / 客户端兼容性

- Runtime 协议版本：YYYY.M.D.N
- Runtime protocol major：N
- 支持的客户端：说明推荐客户端版本或最低兼容版本。
- methods 变化：列出新增、废弃或移除的方法；没有则写“无”。
- capabilities 变化：列出新增、废弃或移除的能力；没有则写“无”。
- 返回字段变化：列出 `runtime.info`、配置诊断、会话、记忆等公开返回字段变化；没有则写“无”。
- 废弃 methods / fields：列出废弃方法或字段，并说明替代方案；没有则写“无”。
- compatibility notes：列出客户端需要关注的兼容事项；没有则写“无”。
- breaking changes：列出破坏性变化；没有则写“无”。

## Schema 版本

| 类型 | 版本 | 说明 |
| --- | --- | --- |
| config schema | 1 | 配置文件格式版本 |
| session schema | 2 | 会话文件格式版本；v2 含会话 revision 与消息身份 |
| memory schema | 1 | 记忆存储格式版本 |
| session export schema | 1 | 会话导出包格式版本 |
| character package schema | 待定 | 角色包格式版本 |

## 已知问题

- 问题：说明当前版本仍存在的已知限制。
  - 临时方案：说明用户可以怎么绕过。
  - 后续计划：说明后续会在哪个方向处理。

## 面向开发者的补充说明

- 代码层变化：简要说明重要模块或 API 变化。
- 测试结果：说明关键测试是否通过；建议列出实际执行的测试命令。
- 文档更新：列出需要同步阅读的文档。
- 发布前检查：确认 [`versioning.md`](versioning.md) 的发布前检查清单已完成。
```

---

## 当前项目版本记录

正式发布记录从 `v2026.5.13.0` 开始。6.x 与 `v2026.7.4.0` 文件是未发布开发快照或候选记录，不应视作正式版本：

- [`v2026.7.30.0.md`](changelog/v2026.7.30.0.md)：正式发布，多角色 GensokyoWorld 完整落地与 Runtime 健壮性记录。
- [`v2026.7.14.0.md`](changelog/v2026.7.14.0.md)：自首个正式基线 `v2026.5.13.0` 以来的累计正式发布。
- [`v2026.5.13.0.md`](changelog/v2026.5.13.0.md)：首个正式 release，首个公开 Alpha 基线。
- [`v2026.6.21.0.md`](changelog/v2026.6.21.0.md)：未发布开发快照，HTTP/WebSocket 入口迁移、DDG 搜索与主动定时器记录。
- [`v2026.6.22.0.md`](changelog/v2026.6.22.0.md)：未发布开发快照，安全与角色开场记录。
- [`v2026.6.23.0.md`](changelog/v2026.6.23.0.md)：未发布开发快照，主动定时器后台化与角色资料记录。
- [`v2026.6.25.0.md`](changelog/v2026.6.25.0.md)：未发布候选记录。
- [`v2026.6.25.1.md`](changelog/v2026.6.25.1.md)：未发布开发快照，性能优化记录。
- [`v2026.7.4.0.md`](changelog/v2026.7.4.0.md)：未发布候选记录，场景系统记录。
