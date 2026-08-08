# GensokyoAI Versioning Guide

This document defines GensokyoAI version number format, where version numbers are written, Runtime protocol compatibility judgment, schema version increment rules, and changelog naming rules.

## 1. Overall Principles

GensokyoAI uses a hybrid strategy of "calendar release version + independent compatibility version":

- Public release versions use calendar versioning, making it easy for ordinary users to judge new vs. old.
- Runtime protocol version uses independent semantic versioning; client compatibility mainly depends on protocol major version.
- The first official release Runtime protocol version is `1.0.0`, not synchronized with calendar versions.
- Persistent schema versions continue to use integers for easy migration logic judgment.
- Changelog version numbers are consistent with package version, but filenames and titles carry the `v` prefix.

## 2. Calendar Version Format

The public version format is:

```text
vYYYY.M.D.N
```

Meaning:

- `YYYY`: release year.
- `M`: release month, no leading zero.
- `D`: release date, no leading zero.
- `N`: release count for the day, starting from `0`.

Examples:

- `v2026.5.11.0`: first release on May 11, 2026.
- `v2026.5.11.1`: second release on May 11, 2026.
- `v2026.5.12.0`: first release on May 12, 2026.

## 3. Where to Use `v` and Where Not

| Location | With `v` | Example | Note |
| --- | --- | --- | --- |
| Python package version | No | `2026.7.14.0` | Written in [`pyproject.toml`](../pyproject.toml) |
| Git tag | Yes | `v2026.7.14.0` | Release tag |
| Changelog filename | Yes | `docs/en/changelog/v2026.7.14.0.md` | Public release notes |
| Changelog title | Yes | `# GensokyoAI v2026.7.14.0 Changelog` | For ordinary users |
| UI display | Recommended | `v2026.7.14.0` | Easier for users to recognize |
| Runtime protocol version | No | `2.0.0` | Independent protocol version for Runtime API; JSON field value has no `v` |

Note: the Python package version in [`pyproject.toml`](../pyproject.toml) must follow PEP 440, so it cannot be `v2026.7.14.0`; it should be `2026.7.14.0`.

## 4. Project / Package Version

The project / package version is written in [`pyproject.toml`](../pyproject.toml):

```toml
[project]
version = "2026.7.14.0"
```

Uses:

- Released package version.
- For ordinary users to identify the current GensokyoAI version.
- Source of changelog version.
- pip / uv / wheel build and publish.
- Runtime exposes to clients, logs, and issue reports via `runtime.info.package_version`.

Release requirements:

- Changelog file version must match the version in [`pyproject.toml`](../pyproject.toml).
- Git tag must equal `v` + package version.

Runtime reading rules:

- When installed, prefer reading version from Python package metadata.
- When running from source, fall back to reading `project.version` from the project root [`pyproject.toml`](../pyproject.toml).
- If both are unavailable, return `0+unknown`.

Example:

```text
pyproject.toml: version = "2026.7.14.0"
Git tag: v2026.7.14.0
Changelog: docs/en/changelog/v2026.7.14.0.md
```

## 5. Runtime Protocol Version

The Runtime protocol version is written in [`rpc.py`](../GensokyoAI/runtime/rpc.py):

```python
RUNTIME_PROTOCOL_VERSION = "2.0.0"
RUNTIME_PROTOCOL_MAJOR_VERSION = 2
```

Rules:

- `RUNTIME_PROTOCOL_VERSION` uses independent semantic versioning without `v`.
- The first official release Runtime protocol version is `1.0.0`; subsequent compatible additions of fields or methods within the same major version increment minor or patch version.
- `RUNTIME_PROTOCOL_MAJOR_VERSION` uses an integer.
- When judging compatibility, clients should prioritize `protocol_major_version`.
- `protocol_version` represents the protocol release batch, not the compatibility level alone.

### When to Modify Runtime Protocol Version

Modify `RUNTIME_PROTOCOL_VERSION` when:

- New Runtime RPC methods are added.
- New `runtime.info` capabilities are added.
- New meaningful response fields are added for clients.
- Documented request / response structures in the Runtime API change.

Modify `RUNTIME_PROTOCOL_MAJOR_VERSION` when:

- Public RPC methods are removed.
- Public response fields are removed.
- Field semantics change causing old clients to misjudge.
- Error structures change causing old clients to fail to handle stably.
- Existing client compatibility is broken.

### Breaking Changes

Breaking changes should be recorded in `RUNTIME_BREAKING_CHANGES` in [`rpc.py`](../GensokyoAI/runtime/rpc.py) and synchronized into the changelog "Removed or Breaking Changes" section.

## 6. Schema Version

Persistent schema versions are written in [`schema_versions.py`](../GensokyoAI/core/schema_versions.py):

```python
CONFIG_SCHEMA_VERSION = 1
SESSION_SCHEMA_VERSION = 2
MEMORY_SCHEMA_VERSION = 2
SESSION_EXPORT_SCHEMA_VERSION = 1
CHARACTER_PACKAGE_SCHEMA_VERSION = 1
```

Schema versions use integers, not date versions.

Reason: schema versions are mainly used by migration logic; integers better express:

```text
v1 -> v2 -> v3
```

Rather than:

```text
2026.5.11.0 -> 2026.5.13.0
```

### When to Increment Schema Version

| Schema | Increment Condition |
| --- | --- |
| config schema | Incompatible change to configuration file structure, or need to migrate old config |
| session schema | Incompatible session file changes; v2 adds session revision and stable message identity |
| memory schema | Incompatible change to memory topic store structure |
| session export schema | Incompatible change to session export package structure |
| character package schema | Formal definition or incompatible change to character package format |

Each schema version increment must be synchronized with:

1. Adding migration branches in [`migrations.py`](../GensokyoAI/core/migrations.py).
2. Explaining impact in the changelog "Data Migration and Upgrade Notes" section.
3. Updating related schema field descriptions in [`runtime_api.md`](runtime_api.md).
4. Adding regression tests from old format to new format.

## 7. Deprecated Fields and Methods Lifecycle

When Runtime API, config fields, response fields, or RPC methods need to be replaced, prefer a "deprecate first, remove later" compatibility flow rather than direct deletion.

### 1. Scope

This lifecycle applies to:

- Runtime RPC methods, e.g. future migration from old methods to clearer namespaced methods.
- Public response fields such as `runtime.info`, session, memory, config validation.
- Config fields and character config fields.
- Public capabilities in changelog that need to prompt clients, scripts, or ordinary users to adjust usage.

Does not apply to:

- Private Python functions, private classes, internal temporary variables.
- Internal implementation details not documented as stable.
- Test helpers.

### 2. Lifecycle Stages

| Stage | Runtime Behavior | Documentation and Changelog Requirements |
| --- | --- | --- |
| active | Normally supported | Normally recorded in API docs |
| deprecated | Continues to be supported, but migration is explicitly recommended | Written into `deprecated_methods` or `deprecated_fields`; changelog placed in Deprecated |
| removal_pending | Still compatible, but removal is declared | Changelog writes both Deprecated and Compatibility, explaining alternatives |
| removed | No longer supported | Changelog placed in Removed; breaking Runtime API changes are also recorded as breaking changes |

### 3. Minimum Compatibility Retention Rules

- Public RPC methods or public response fields marked deprecated are retained by default at least until the next official release.
- If removal breaks existing clients, `RUNTIME_PROTOCOL_MAJOR_VERSION` must be incremented.
- If only alternative fields or methods are added, `RUNTIME_PROTOCOL_MAJOR_VERSION` should not be incremented, but `RUNTIME_PROTOCOL_VERSION` should be updated.
- If config fields are deprecated, the config diagnostics should first return warnings, then consider upgrading to error or removal in a future schema version.
- If persistent data fields are deprecated, they should not be directly discarded; they should be handled through schema version and migration logic.

### 4. Runtime Declaration Rules

When declaring deprecation information externally, Runtime should prefer structured fields:

- RPC method deprecation information is written in [`deprecated_rpc_methods()`](../GensokyoAI/runtime/rpc.py) or its data source.
- Runtime protocol metadata exposes `deprecated_methods` through [`runtime_protocol_metadata()`](../GensokyoAI/runtime/rpc.py).
- Non-method field deprecation information is exposed through `runtime.info.deprecated_fields`.
- Breaking removals are exposed through `breaking_changes` and synchronized to the changelog.

Recommended field structure:

```json
{
  "name": "runtime.old_method",
  "since": "2026.5.11.0",
  "remove_after": "2026.5.12.0",
  "replacement": "runtime.new_method",
  "reason": "Use namespaced Runtime method names."
}
```

Field descriptions:

- `name`: deprecated method or field name.
- `since`: version when deprecation started, without `v`.
- `remove_after`: planned earliest removal version, without `v`; can be `null` if undetermined.
- `replacement`: alternative method or field; can be `null` if no alternative.
- `reason`: human-readable reason for users or client developers.

### 5. Changelog Writing

Each release containing deprecation or removal must explain:

- Which field or method is deprecated / removed.
- Effective version.
- Recommended alternative.
- Whether it affects ordinary users, client developers, or existing data.
- If data structure changes are involved, whether automatic migration is triggered and where to view migration diagnostics.

Example:

```markdown
## Deprecated

- `runtime.old_method` is deprecated; please use `runtime.new_method`. The old method will be removed in a future version.

## Removed

- Removed `runtime.legacy_method`. Clients still relying on it need to upgrade to `runtime.new_method`.
```

### 6. Deprecated Field Recording Specification

When public fields are deprecated, a structured record must be maintained instead of only verbally explaining in documentation. The record fields are recommended to be consistent with deprecated RPC methods:

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

Rules:

- `name` must be the complete field path in external documentation, e.g. `runtime.info.deprecated_fields`.
- `since` and `remove_after` use package / protocol versions without `v`; `remove_after` can be `null` if removal time cannot be determined.
- `replacement` can be `null` if there is no alternative, but `reason` must explain impact and handling.
- `status` uses `deprecated`, `removal_pending`, or `removed`.
- Runtime public field deprecation records should be exposed through `runtime.info.deprecated_fields`; ordinary config field deprecation still gives warnings through config diagnostics.

### 7. Runtime Metadata Synchronization Requirements

Each time Runtime public capabilities are modified, synchronize checks:

- Do `runtime.info.methods`, `legacy_methods`, and `method_specs` reflect the real method table in [`rpc.py`](../GensokyoAI/runtime/rpc.py)?
- Is `runtime.info.capabilities` consistent with public capabilities in [`runtime_api.md`](runtime_api.md)?
- Does `runtime.info.deprecated_methods` come from the unified RPC method table?
- Are `runtime.info.deprecated_fields` and `compatibility_notes` from maintainable constants or helpers, rather than scattered temporary literals?
- Is `runtime.info.breaking_changes` consistent with the changelog "Removed or Breaking Changes"?

### 8. Pre-Release Checks

If deprecation or removal exists before release, additionally check:

- [ ] Does [`runtime_api.md`](runtime_api.md) mark deprecated status and alternatives?
- [ ] Is [`rpc.py`](../GensokyoAI/runtime/rpc.py) synchronized with `deprecated_methods` or `breaking_changes`?
- [ ] Does `runtime.info.deprecated_fields` include public field deprecation information?
- [ ] Does `runtime.info.compatibility_notes` explain client compatibility concerns?
- [ ] Does the changelog contain Deprecated, Removed, or Compatibility sections?
- [ ] When removing public Runtime API, is `RUNTIME_PROTOCOL_MAJOR_VERSION` incremented?
- [ ] When persistent data changes are involved, is the corresponding schema version incremented and migration diagnostic tests added?

## 8. Changelog Rules

The changelog template is at [`changelog.md`](changelog.md); official release records are recommended to be placed in the `docs/changelog/` directory.

Recommended structure:

```text
docs/
  changelog.md
  changelog/
    v2026.5.11.0.md
    v2026.5.11.1.md
```

Rules:

- [`changelog.md`](changelog.md) is the template and writing guide.
- Each official release creates an independent version file.
- Filenames use Git tag form with `v`.
- Changelog titles also carry `v`.
- Changelog content targets ordinary users; explain user-perceivable changes first, then supplement developer and client compatibility information.

## 9. Pre-Release Checklist

Before each release, check in order:

- [ ] Is the package version in [`pyproject.toml`](../pyproject.toml) correct?
- [ ] Can `runtime.info.package_version` return the current package version?
- [ ] Does [`rpc.py`](../GensokyoAI/runtime/rpc.py) need to update the Runtime protocol version?
- [ ] Have breaking Runtime API changes occurred; if so, is `RUNTIME_PROTOCOL_MAJOR_VERSION` incremented and `breaking_changes` recorded?
- [ ] Are Runtime methods, capabilities, or response fields added or removed; if so, is [`runtime_api.md`](runtime_api.md) updated and consistency tests added?
- [ ] Are new deprecated methods / fields added; if so, are `since`, `remove_after`, `replacement`, `reason`, and changelog explanations added?
- [ ] Does [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) need to update schema version?
- [ ] When schema version is incremented, is migration logic, backup / recovery instructions, and tests added?
- [ ] Is the corresponding changelog file created?
- [ ] Does the changelog explain additions, fixes, behavior changes, deprecations, breaking changes, dependency changes, data migration, Runtime / schema versions, and test results?
- [ ] If deprecated or removed items exist, are Runtime declarations and migration instructions supplemented according to the deprecated fields and methods lifecycle?
- [ ] Do [`runtime_api.md`](runtime_api.md), [`user_guide.md`](user_guide.md), and README need to synchronize public API / user entry changes?
- [ ] Have key tests passed, and are test scopes recorded in the changelog "Supplementary Notes for Developers"?
- [ ] Is the Git tag consistent with the package version?

## 10. Recommended Version Matrix

| Type | Format | Example | Written Location |
| --- | --- | --- | --- |
| package version | `YYYY.M.D.N` | `2026.7.14.0` | [`pyproject.toml`](../pyproject.toml) |
| Git tag | `vYYYY.M.D.N` | `v2026.7.14.0` | Git tag |
| changelog | `vYYYY.M.D.N` | [`docs/en/changelog/v2026.7.14.0.md`](changelog/v2026.7.14.0.md) | [`docs/en/changelog`](changelog) |
| Runtime protocol version | independent semantic version | `2.0.0` | [`rpc.py`](../GensokyoAI/runtime/rpc.py) |
| Runtime protocol major | integer | `2` | [`rpc.py`](../GensokyoAI/runtime/rpc.py) |
| config schema | integer | `1` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |
| session schema | integer | `2` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |
| memory schema | integer | `2` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |
| session export schema | integer | `1` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |
| character package schema | integer or `None` | `1` | [`schema_versions.py`](../GensokyoAI/core/schema_versions.py) |

## 11. Current Status

Current Alpha prerelease preparation status:

- Current package version is in [`pyproject.toml`](../pyproject.toml) and is `2026.8.8.0`.
- Current Runtime protocol version is in [`rpc.py`](../GensokyoAI/runtime/rpc.py) and is `2.2.0`, with major `2`.
- Current schema versions are in [`schema_versions.py`](../GensokyoAI/core/schema_versions.py): session and memory are `2`, world session is `1`, and all other public schemas are `1`.
- Release changelog is [`docs/en/changelog/v2026.8.8.0.md`](changelog/v2026.8.8.0.md).
- The corresponding Git tag is `v2026.8.8.0`; release preparation does not create the tag.
- The channel is a GitHub Alpha prerelease with wheel and sdist only; this release does not publish to PyPI or bundle the desktop client.
