# GensokyoAI 版本管理说明

本文档定义 GensokyoAI 的版本号格式、版本号写入位置、Runtime 协议兼容判断、schema version 递增规则和 changelog 命名规则。

## 一、总体原则

GensokyoAI 采用“日期发布版本 + 独立兼容版本”的混合策略：

- 对外 release 版本使用日期版本号，方便普通用户判断新旧。
- Runtime protocol version 使用独立语义版本；客户端兼容性主要看 protocol major version。
- 首个正式 release 的 Runtime protocol version 为 `1.0.0`，不随日期版本同步。
- 持久化 schema version 继续使用整数，方便迁移逻辑判断。
- changelog 版本号与 package version 一致，但文件名和标题带 `v` 前缀。

## 二、日期版本号格式

对外版本号格式为：

```text
vYYYY.M.D.N
```

含义：

- `YYYY`：发布年份。
- `M`：发布月份，不补零。
- `D`：发布日期，不补零。
- `N`：当天第几次发布，从 `0` 开始。

示例：

- `v2026.5.11.0`：2026 年 5 月 11 日第 1 次发布。
- `v2026.5.11.1`：2026 年 5 月 11 日第 2 次发布。
- `v2026.5.12.0`：2026 年 5 月 12 日第 1 次发布。

## 三、哪些地方带 `v`，哪些地方不带 `v`

| 位置 | 是否带 `v` | 示例 | 说明 |
| --- | --- | --- | --- |
| Python package version | 不带 | `2026.7.14.0` | 写入 [`pyproject.toml`](../pyproject.toml) |
| Git tag | 带 | `v2026.7.14.0` | 发布 tag |
| changelog 文件名 | 带 | `docs/changelog/v2026.7.14.0.md` | 对外更新日志 |
| changelog 标题 | 带 | `# GensokyoAI v2026.7.14.0 更新日志` | 普通用户阅读 |
| UI 展示版本 | 建议带 | `v2026.7.14.0` | 用户更容易识别 |
| Runtime protocol version | 不带 | `2.0.0` | Runtime API 独立协议版本，JSON 字段值不带 `v` |

注意：[`pyproject.toml`](../pyproject.toml) 中的 Python 包版本应遵循 PEP 440，不能写 `v2026.7.14.0`，应写 `2026.7.14.0`。

## 四、项目 / 包版本

项目 / 包版本写在 [`pyproject.toml`](../pyproject.toml)：

```toml
[project]
version = "2026.7.14.0"
```

用途：

- 发布包版本。
- 普通用户识别当前 GensokyoAI 版本。
- changelog 版本来源。
- pip / uv / wheel 构建和发布。
- Runtime 通过 `runtime.info.package_version` 暴露给客户端、日志和故障报告。

发布时要求：

- changelog 文件版本必须与 [`pyproject.toml`](../pyproject.toml) 的版本一致。
- Git tag 必须等于 `v` + package version。

Runtime 读取规则：

- 已安装运行时，优先从 Python package metadata 读取版本。
- 源码运行时，回退读取项目根目录 [`pyproject.toml`](../pyproject.toml) 的 `project.version`。
- 如果两者都不可用，返回 `0+unknown`。

示例：

```text
pyproject.toml: version = "2026.7.14.0"
Git tag: v2026.7.14.0
Changelog: docs/changelog/v2026.7.14.0.md
```

## 五、Runtime 协议版本

Runtime 协议版本写在 [`rpc.py`](../GensokyoAI/runtime/rpc.py)：

```python
RUNTIME_PROTOCOL_VERSION = "2.0.0"
RUNTIME_PROTOCOL_MAJOR_VERSION = 2
```

规则：

- `RUNTIME_PROTOCOL_VERSION` 使用独立语义版本，不带 `v`。
- 首个正式 release 的 Runtime protocol version 为 `1.0.0`；后续同一主版本内新增兼容字段或方法时递增次版本或修订版本。
- `RUNTIME_PROTOCOL_MAJOR_VERSION` 使用整数。
- 客户端判断是否兼容时，优先看 `protocol_major_version`。
- `protocol_version` 表示协议发布批次，不单独代表兼容等级。

### 什么时候修改 Runtime protocol version

需要修改 `RUNTIME_PROTOCOL_VERSION` 的情况：

- 新增 Runtime RPC 方法。
- 新增 `runtime.info` capability。
- 新增对客户端有意义的返回字段。
- 修改 Runtime API 文档中公开的请求 / 响应结构。

需要修改 `RUNTIME_PROTOCOL_MAJOR_VERSION` 的情况：

- 删除公开 RPC 方法。
- 删除公开返回字段。
- 修改字段语义导致旧客户端误判。
- 修改错误结构导致旧客户端无法稳定处理。
- 破坏现有客户端兼容性。

### breaking changes

破坏性变化应记录在 [`rpc.py`](../GensokyoAI/runtime/rpc.py) 的 `RUNTIME_BREAKING_CHANGES` 中，并同步写入 changelog 的“已移除或破坏性变化”小节。

## 六、schema version

持久化 schema version 写在 [`schema_versions.py`](../GensokyoAI/core/schema_versions.py)：

```python
CONFIG_SCHEMA_VERSION = 1
SESSION_SCHEMA_VERSION = 2
MEMORY_SCHEMA_VERSION = 2
SESSION_EXPORT_SCHEMA_VERSION = 1
CHARACTER_PACKAGE_SCHEMA_VERSION = 1
```

schema version 使用整数，不使用日期版本。

原因：schema version 主要给迁移逻辑使用，整数更适合表达：

```text
v1 -> v2 -> v3
```

而不是：

```text
2026.5.11.0 -> 2026.5.13.0
```

### 什么时候递增 schema version

| schema | 递增条件 |
| --- | --- |
| config schema | 配置文件结构出现不兼容变化，或需要迁移旧配置 |
| session schema | 会话文件结构出现不兼容变化；v2 增加会话 revision 与稳定消息身份 |
| memory schema | 记忆 topic store 结构出现不兼容变化 |
| session export schema | 会话导出包结构出现不兼容变化 |
| character package schema | 角色包格式正式定义或出现不兼容变化 |

每次递增 schema version 时必须同步：

1. 在 [`migrations.py`](../GensokyoAI/core/migrations.py) 中补迁移分支。
2. 在 changelog 的“数据迁移与升级提醒”中说明影响。
3. 在 [`runtime_api.md`](runtime_api.md) 中更新相关 schema 字段说明。
4. 补充旧格式到新格式的回归测试。

## 七、废弃字段和废弃方法生命周期

当 Runtime API、配置字段、返回字段或 RPC 方法需要被替换时，优先采用“先废弃、再移除”的兼容流程，而不是直接删除。

### 1. 适用范围

本生命周期适用于：

- Runtime RPC 方法，例如未来从旧方法迁移到更清晰的命名空间方法。
- `runtime.info`、session、memory、config validation 等公开返回字段。
- 配置字段和角色配置字段。
- changelog 中需要提示客户端、脚本或普通用户调整用法的公开能力。

不适用于：

- 私有 Python 函数、私有类、内部临时变量。
- 未在文档中承诺稳定的内部实现细节。
- 测试辅助对象。

### 2. 生命周期阶段

| 阶段 | Runtime 行为 | 文档和 changelog 要求 |
| --- | --- | --- |
| active | 正常支持 | 正常记录在 API 文档中 |
| deprecated | 继续支持，但明确推荐迁移 | 写入 `deprecated_methods` 或 `deprecated_fields`，changelog 放入 Deprecated |
| removal_pending | 仍兼容，但声明将移除 | changelog 同时写入 Deprecated 和 Compatibility，说明替代方案 |
| removed | 不再支持 | changelog 放入 Removed，破坏 Runtime API 时同步记录 breaking changes |

### 3. 最低兼容保留规则

- 被标记为 deprecated 的公开 RPC 方法或公开返回字段，默认至少保留到下一个正式发布版本。
- 如果移除会破坏现有客户端，必须递增 `RUNTIME_PROTOCOL_MAJOR_VERSION`。
- 如果只是新增替代字段或替代方法，不应递增 `RUNTIME_PROTOCOL_MAJOR_VERSION`，但应更新 `RUNTIME_PROTOCOL_VERSION`。
- 配置字段如果被废弃，应先由配置诊断返回 warning，再考虑在未来 schema version 中升级为 error 或移除。
- 持久化数据字段如果被废弃，不应直接丢弃；应通过 schema version 和迁移逻辑处理。

### 4. Runtime 声明规则

Runtime 对外声明废弃信息时，应优先使用结构化字段：

- RPC 方法废弃信息写入 [`deprecated_rpc_methods()`](../GensokyoAI/runtime/rpc.py) 或其数据来源。
- Runtime 协议元数据通过 [`runtime_protocol_metadata()`](../GensokyoAI/runtime/rpc.py) 暴露 `deprecated_methods`。
- 非方法类字段废弃信息通过 `runtime.info.deprecated_fields` 暴露。
- 破坏性移除通过 `breaking_changes` 暴露，并同步 changelog。

推荐字段结构：

```json
{
  "name": "runtime.old_method",
  "since": "2026.5.11.0",
  "remove_after": "2026.5.12.0",
  "replacement": "runtime.new_method",
  "reason": "Use namespaced Runtime method names."
}
```

字段说明：

- `name`：废弃方法或字段名。
- `since`：开始废弃的版本，不带 `v`。
- `remove_after`：计划最早移除版本，不带 `v`；无法确定时可为 `null`。
- `replacement`：替代方法或字段；没有替代方案时可为 `null`。
- `reason`：用户或客户端开发者可读原因。

### 5. changelog 写法

每次发布如果包含废弃或移除，changelog 必须说明：

- 哪个字段或方法被 deprecated / removed。
- 从哪个版本开始生效。
- 推荐替代方案。
- 是否影响普通用户、客户端开发者或已有数据。
- 如果涉及数据结构变化，是否会触发自动迁移，以及迁移诊断在哪里查看。

示例：

```markdown
## Deprecated

- `runtime.old_method` 已废弃，请改用 `runtime.new_method`。旧方法将在后续版本中移除。

## Removed

- 移除了 `runtime.legacy_method`。如客户端仍依赖该方法，需要升级到 `runtime.new_method`。
```

### 6. 废弃字段记录规范

公开字段废弃时必须维护一条结构化记录，避免只在文档里口头说明。记录字段建议与废弃 RPC 方法保持一致：

```json
{
  "name": "runtime.info.old_field",
  "since": "2026.5.11.0",
  "remove_after": "2026.5.12.0",
  "replacement": "runtime.info.new_field",
  "reason": "Use the namespaced field for clearer client integration.",
  "status": "deprecated"
}
```

规则：

- `name` 必须是对外文档中的完整字段路径，例如 `runtime.info.deprecated_fields`。
- `since` 和 `remove_after` 使用不带 `v` 的 package / protocol 版本；不能确定移除时间时 `remove_after` 可为 `null`。
- `replacement` 没有替代方案时可为 `null`，但 `reason` 必须说明影响和处理方式。
- `status` 使用 `deprecated`、`removal_pending` 或 `removed`。
- Runtime 公开字段废弃记录应通过 `runtime.info.deprecated_fields` 暴露；普通配置字段废弃仍由配置诊断给 warning。

### 7. Runtime metadata 同步要求

每次修改 Runtime 公开能力时，需要同步检查：

- `runtime.info.methods`、`legacy_methods`、`method_specs` 是否能反映 [`rpc.py`](../GensokyoAI/runtime/rpc.py) 中的真实方法表。
- `runtime.info.capabilities` 是否与 [`runtime_api.md`](runtime_api.md) 中公开能力一致。
- `runtime.info.deprecated_methods` 是否来自统一 RPC 方法表。
- `runtime.info.deprecated_fields` 和 `compatibility_notes` 是否来自可维护的常量或 helper，而不是散落的临时字面量。
- `runtime.info.breaking_changes` 是否与 changelog 的“已移除或破坏性变化”一致。

### 8. 发布前检查

发布前如果存在废弃或移除，需要额外检查：

- [ ] [`runtime_api.md`](runtime_api.md) 是否标注废弃状态和替代方案。
- [ ] [`rpc.py`](../GensokyoAI/runtime/rpc.py) 是否同步 `deprecated_methods` 或 `breaking_changes`。
- [ ] `runtime.info.deprecated_fields` 是否包含公开字段废弃信息。
- [ ] `runtime.info.compatibility_notes` 是否说明客户端需要关注的兼容事项。
- [ ] changelog 是否包含 Deprecated、Removed 或 Compatibility 小节。
- [ ] 移除公开 Runtime API 时是否递增 `RUNTIME_PROTOCOL_MAJOR_VERSION`。
- [ ] 涉及持久化数据变化时是否递增对应 schema version，并补迁移诊断测试。

## 八、changelog 规则

changelog 模板位于 [`changelog.md`](changelog.md)，正式发布记录建议放在 `docs/changelog/` 目录。

建议结构：

```text
docs/
  changelog.md
  changelog/
    v2026.5.11.0.md
    v2026.5.11.1.md
```

规则：

- [`changelog.md`](changelog.md) 是模板和写作说明。
- 每次正式发布创建一个独立版本文件。
- 文件名使用 Git tag 形式，带 `v`。
- changelog 标题也带 `v`。
- changelog 内容面向普通用户，先讲用户能感知的变化，再补充开发者和客户端兼容信息。

## 九、发布前检查清单

每次发布前按顺序检查：

- [ ] [`pyproject.toml`](../pyproject.toml) 中 package version 是否正确。
- [ ] `runtime.info.package_version` 是否能返回当前 package version。
- [ ] 是否需要更新 [`rpc.py`](../GensokyoAI/runtime/rpc.py) 中 Runtime protocol version。
- [ ] 是否发生破坏性 Runtime API 变化；如有，是否递增 `RUNTIME_PROTOCOL_MAJOR_VERSION` 并记录 `breaking_changes`。
- [ ] 是否新增或删除 Runtime methods、capabilities、返回字段；如有，是否更新 [`runtime_api.md`](runtime_api.md) 并补一致性测试。
- [ ] 是否新增 deprecated methods / fields；如有，是否补 `since`、`remove_after`、`replacement`、`reason` 和 changelog 说明。
- [ ] 是否需要更新 [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) 中 schema version。
- [ ] schema version 递增时，是否已补迁移逻辑、备份 / 恢复说明和测试。
- [ ] 是否创建对应 changelog 文件。
- [ ] changelog 是否说明新增、修复、行为变化、废弃、破坏性变化、安装依赖变化、数据迁移、Runtime / schema 版本和测试结果。
- [ ] 如存在 deprecated 或 removed 项，是否已按废弃字段和废弃方法生命周期补充 Runtime 声明与迁移说明。
- [ ] [`runtime_api.md`](runtime_api.md)、[`user_guide.md`](user_guide.md) 和 README 是否需要同步公开 API / 用户入口变化。
- [ ] 关键测试是否通过，并在 changelog 的“面向开发者的补充说明”中记录测试范围。
- [ ] Git tag 是否与 package version 一致。

## 十、推荐版本矩阵

| 类型 | 格式 | 示例 | 写入位置 |
| --- | --- | --- | --- |
| package version | `YYYY.M.D.N` | `2026.7.14.0` | [`pyproject.toml`](../pyproject.toml) |
| Git tag | `vYYYY.M.D.N` | `v2026.7.14.0` | Git tag |
| changelog | `vYYYY.M.D.N` | [`docs/changelog/v2026.7.14.0.md`](changelog/v2026.7.14.0.md) | [`docs/changelog`](changelog) |
| Runtime protocol version | 独立语义版本 | `2.0.0` | [`rpc.py`](../GensokyoAI/runtime/rpc.py) |
| Runtime protocol major | integer | `1` | [`rpc.py`](../GensokyoAI/runtime/rpc.py) |
| config schema | integer | `1` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |
| session schema | integer | `2` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |
| memory schema | integer | `2` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |
| session export schema | integer | `1` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |
| character package schema | integer 或 `None` | `1` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |

## 十一、当前状态

当前累计正式发布状态：

- package version 当前见 [`pyproject.toml`](../pyproject.toml)，应为 `2026.8.4.0`。
- Runtime protocol version 当前见 [`rpc.py`](../GensokyoAI/runtime/rpc.py)，应为 `2.2.0`，major 为 `2`。
- schema versions 当前见 [`schema_versions.py`](../GensokyoAI/core/schema_versions.py)：memory 为 `2`，session 为 `2`，其余公开 schema 为 `1`。
- release changelog 为 [`docs/changelog/v2026.8.4.0.md`](changelog/v2026.8.4.0.md)。
- 对应 Git tag 为 `v2026.8.4.0`；发布准备阶段不创建 tag。
