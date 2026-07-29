# GensokyoAI Changelog Template

> Positioning: this changelog is publicly released for ordinary users, client developers, and integrators.
>
> Writing principle: explain user-perceivable changes first, then supplement developer compatibility, migration, and protocol information. Ordinary users should be able to understand "what changes after upgrading, whether old data is affected, and whether manual action is needed."

## Version Number Rules

For version format, where to use `v` and where not, and the difference between Runtime protocol version and schema version, see [`versioning.md`](versioning.md).

Brief rules:

- Public versions and changelog use `vYYYY.M.D.N`; the current official release is `v2026.7.30.0`, and the first official release was `v2026.5.13.0`.
- Package version in [`pyproject.toml`](../pyproject.toml) has no `v`; the current version is `2026.7.30.0`.
- Runtime protocol version uses independent semantic versioning without `v`; the current development branch uses `2.0.0` with major `2`; client compatibility prioritizes `protocol_major_version`.
- Schema versions continue to use integers, e.g. `1`, `2`, `3`.

## Usage

Each time a new version is released, copy the version template below, place it at the top of this file, and delete or write "None" for unused sections.

Each change entry should ideally include:

- What changed.
- Impact on ordinary users.
- Whether manual action is needed.
- Whether old config, old sessions, old memory, character files, or client integration are affected.
- Whether Runtime methods, capabilities, response fields, error structures, or schema versions changed.
- Whether deprecated, removal_pending, removed, or breaking changes are involved; if so, alternatives and migration methods must be given.

---

## Current Development Compatibility Summary

- Runtime Protocol `2.0.0` performs one concentrated incompatible revision: network resources use `user_id -> agent_id -> session_id`, writes require `expected_revision`, and sends require `idempotency_key`.
- Adds JWT/RBAC, tenant storage isolation, stable `message_id`, pagination, persistent event replay, multipart media upload, and a fail-closed remote character-package trust gate.
- Message idempotency now uses a durable operation ledger written before Provider execution, with `message.status` for disconnect recovery. Generation token streams explicitly do not resume across connections; clients recover from operation state and authoritative session reads.
- Adds readiness, graceful drain, and a remote administration surface disabled by default on HTTP/WS. Network `method_specs` now expose actual resource requirements and result-schema completeness.
- Session schema moves from `1` to `2`; legacy files gain revision/message identity while unknown extension fields are preserved.
- The file backend targets single-node remote multi-user service. Multi-node production still requires PostgreSQL and shared object storage.
- Runtime Protocol `2.1.0` adds the `world.*` multi-character orchestration methods (14) and the `world.orchestration` capability; World sessions use an independent `world session schema v1`; `world.send_*` reuses the message idempotency ledger. Protocol major stays `2` — no client migration needed.
- The forced initiative fallback chain (`fallback_on_no_schedule` and 3 related keys) is removed; legacy keys only warn on validation. Runtime event redaction is unified to the `[REDACTED]` placeholder.
- Speaking drive reworked per the 2026-07-30 final design: proactive speech is now decided by the ThinkEngine's four-dimension mood scoring plus the `initiative_timer.drive_threshold` (default 0.6) — no accumulator; `hesitation_*` and `drive_*` config keys are retired (warning-only migration); `initiative_timer.hesitation` / `initiative_timer.hesitation.set` become legacy/deprecated (`remove_after: "3.0.0"`, still discoverable via `legacy_methods`, returning a retirement payload); timer event payloads drop the `hesitation_*` fields.

---

## Version Template

```markdown
# GensokyoAI vYYYY.M.D.N Changelog

Release date: YYYY-MM-DD

## One-Sentence Summary

Use one or two sentences to explain the most important changes in this version, so ordinary users can quickly judge whether to upgrade.

## New Features

- New: explain the new capability.
  - User impact: explain what new things users can do.
  - Action needed: explain whether config changes, restart, or dependency reinstallation are needed.

## Behavior Changes

- Change: explain behavior changes of existing features.
  - User impact: explain how the experience differs after upgrade.
  - Compatibility: explain whether old usage still works; if a warning is tightened to an error, specify affected configs.
  - Client impact: explain whether Runtime response fields, error codes, capabilities, or method lists changed.

## Bug Fixes

- Fix: explain the fixed issue.
  - Affected scenarios: explain which users may have encountered it.
  - Upgrade recommendation: explain whether affected users are recommended to upgrade.

## Deprecated but Still Compatible

- Deprecated: explain config, fields, RPC methods, or file formats that are not recommended for continued use.
  - Deprecated object: write the full path or method name, e.g. `runtime.info.old_field` or `legacy_method`.
  - Effective version: write version without `v`, e.g. `2026.5.11.0`.
  - Alternative: explain what to use instead; if no alternative, explain why.
  - Planned removal: explain `remove_after`; write "undetermined" if unknown.
  - Runtime declaration: explain whether it has been written into `runtime.info.deprecated_methods` or `runtime.info.deprecated_fields`.

## Removed or Breaking Changes

- Removal: explain capabilities no longer supported.
  - Scope: explain which users or clients are affected.
  - Breaking level: explain whether `RUNTIME_PROTOCOL_MAJOR_VERSION` or schema version needs to be incremented.
  - Migration: explain how to change config, calling methods, or migrate data.
  - Runtime declaration: explain whether it has been written into `runtime.info.breaking_changes`.

## Data Migration and Upgrade Notes

- Data migration: explain whether this version will migrate sessions, memory, config, or character packages.
  - Automatic migration: explain what the program will handle automatically.
  - Backup location: explain where backup files or export packages are.
  - Failure handling: explain where users should look and how to recover if migration fails.

## Installation and Dependency Changes

- Dependency changes: explain whether Python version, pip / uv installation, Provider SDK, Ollama, or system dependencies changed.
  - Ordinary user action: explain whether dependencies need to be reinstalled.
  - Windows user reminder: explain script, path, or permission issues here if involved.

## Runtime / Client Compatibility

- Runtime protocol version: YYYY.M.D.N
- Runtime protocol major: N
- Supported clients: explain recommended client versions or minimum compatible versions.
- Method changes: list added, deprecated, or removed methods; write "None" if no changes.
- Capability changes: list added, deprecated, or removed capabilities; write "None" if no changes.
- Response field changes: list changes to public response fields such as `runtime.info`, config diagnostics, sessions, memory; write "None" if no changes.
- Deprecated methods / fields: list deprecated methods or fields and explain alternatives; write "None" if no changes.
- Compatibility notes: list compatibility concerns for clients; write "None" if no changes.
- Breaking changes: list breaking changes; write "None" if no changes.

## Schema Versions

| Type | Version | Note |
| --- | --- | --- |
| config schema | 1 | Config file format version |
| session schema | 2 | Session format; v2 includes session revision and message identity |
| memory schema | 1 | Memory storage format version |
| session export schema | 1 | Session export package format version |
| character package schema | TBD | Character package format version |

## Known Issues

- Issue: explain current known limitations in this version.
  - Workaround: explain how users can bypass it.
  - Future plan: explain which direction it will be handled in later.

## Supplementary Notes for Developers

- Code-level changes: briefly explain important module or API changes.
- Test results: explain whether key tests passed; it is recommended to list actually executed test commands.
- Documentation updates: list documents that need to be read in sync.
- Pre-release checks: confirm that the pre-release checklist in [`versioning.md`](versioning.md) has been completed.
```

---

## Current Project Version Records

Official release records start at `v2026.5.13.0`. The 6.x and `v2026.7.4.0` files are unpublished development snapshots or candidate notes and must not be treated as official versions:

- [`v2026.7.30.0.md`](changelog/v2026.7.30.0.md): official release covering the completed multi-character GensokyoWorld and Runtime robustness.
- [`v2026.7.14.0.md`](changelog/v2026.7.14.0.md): cumulative official release since the first official baseline `v2026.5.13.0`.
- [`v2026.5.13.0.md`](changelog/v2026.5.13.0.md): the first official release and first public Alpha baseline.
- [`v2026.6.21.0.md`](../changelog/v2026.6.21.0.md): unpublished development snapshot covering HTTP/WebSocket migration, DDG search, and initiative timers.
- [`v2026.6.22.0.md`](../changelog/v2026.6.22.0.md): unpublished development snapshot covering security and character openings.
- [`v2026.6.23.0.md`](../changelog/v2026.6.23.0.md): unpublished development snapshot covering background timers and character data.
- [`v2026.6.25.0.md`](../changelog/v2026.6.25.0.md): unpublished candidate note.
- [`v2026.6.25.1.md`](../changelog/v2026.6.25.1.md): unpublished performance-development snapshot.
- [`v2026.7.4.0.md`](../changelog/v2026.7.4.0.md): unpublished candidate note for the scene system.
