#!/usr/bin/env python3
"""将 default.yaml 的新增/重命名字段同步到本地配置文件中。

功能：
- 补全目标文件中缺失的键（递归处理嵌套 dict）
- 支持 --rename 将旧键值迁移到新键名下
- 检测目标中已不存在于 source 的废弃键
- --prune 可自动移除废弃键
- 已设置的值不做任何修改
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def _split_path(path: str) -> list[str]:
    """将 'a.b.c' 拆为 ['a', 'b', 'c']。"""
    return [p for p in path.split(".") if p]


def _get_nested(data: dict, path: list[str]) -> object:
    """读取嵌套值，路径不存在则返回 sentinel。"""
    current: object = data
    for key in path:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return _MISSING
    return current


def _set_nested(data: dict, path: list[str], value: object) -> None:
    """写入嵌套值，缺失的中间 dict 自动创建。"""
    current = data
    for key in path[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[path[-1]] = value


def _del_nested(data: dict, path: list[str]) -> bool:
    """删除嵌套值，返回是否成功删除。"""
    if not path:
        return False
    current = data
    for key in path[:-1]:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    if isinstance(current, dict) and path[-1] in current:
        del current[path[-1]]
        # 清理空的中间 dict
        return True
    return False


class _MissingSentinel:
    """标记嵌套路径不存在。"""

    def __repr__(self):
        return "<MISSING>"


_MISSING = _MissingSentinel()


def apply_renames(target: dict, renames: list[tuple[str, str]]) -> list[str]:
    """将 target 中旧键的值迁移到新键名下，删除旧键。

    返回实际执行了迁移的 rename 描述列表。
    """
    applied: list[str] = []
    for old_raw, new_raw in renames:
        old_path = _split_path(old_raw)
        new_path = _split_path(new_raw)
        if not old_path or not new_path:
            continue
        value = _get_nested(target, old_path)
        if value is _MISSING:
            continue
        # 如果新位置已有值且和旧值不同，跳过避免覆盖用户设置
        existing = _get_nested(target, new_path)
        if existing is not _MISSING and existing != value:
            continue
        _set_nested(target, new_path, value)
        _del_nested(target, old_path)
        applied.append(f"{old_raw} -> {new_raw}")
    return applied


def deep_merge(source: dict, target: dict) -> dict:
    """递归补全 target 中缺失的 source 键，保留 target 已有值。"""
    result = dict(target)
    for key, value in source.items():
        if key not in result:
            result[key] = value
        elif isinstance(value, dict) and isinstance(result[key], dict):
            result[key] = deep_merge(value, result[key])
    return result


def collect_added(source: dict, target: dict, prefix: str = "") -> list[str]:
    """收集 source 中有但 target 中缺失的键路径。"""
    added: list[str] = []
    for key, value in source.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in target:
            added.append(path)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            added.extend(collect_added(value, target[key], path))
    return added


def collect_obsolete(source: dict, target: dict, prefix: str = "") -> list[str]:
    """收集 target 中有但 source 中不存在的键路径（可能是废弃/改名键）。"""
    obsolete: list[str] = []
    for key, value in target.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in source:
            obsolete.append(path)
        elif isinstance(value, dict) and key in source and isinstance(source[key], dict):
            obsolete.extend(collect_obsolete(source[key], value, path))
    return obsolete


def prune_obsolete(target: dict, obsolete_paths: list[str]) -> int:
    """从 target 中移除废弃键路径，返回移除数量。"""
    count = 0
    for path_str in obsolete_paths:
        path = _split_path(path_str)
        if _del_nested(target, path):
            count += 1
    return count


def sync_config(
    source_path: Path,
    target_path: Path,
    *,
    renames: list[tuple[str, str]] | None = None,
    dry_run: bool = False,
    backup: bool = True,
    prune: bool = False,
) -> int:
    """将 source 的格式同步到 target。

    返回变更总数（新增 + 重命名 + 移除），-1 表示错误。
    """
    if not source_path.exists():
        print(f"错误: 源文件不存在 {source_path}")
        return -1
    if not target_path.exists():
        print(f"错误: 目标文件不存在 {target_path}")
        return -1

    with open(source_path, encoding="utf-8") as f:
        source = yaml.safe_load(f)
    with open(target_path, encoding="utf-8") as f:
        target = yaml.safe_load(f)

    if not isinstance(source, dict) or not isinstance(target, dict):
        print("错误: 两个文件都必须包含顶层 YAML mapping")
        return -1

    renames = renames or []

    # ---- 1. 应用重命名 ----
    rename_applied = apply_renames(target, renames)
    if rename_applied:
        print(f"重命名迁移 ({len(rename_applied)}):")
        for entry in rename_applied:
            print(f"  ~ {entry}")

    # ---- 2. 检测新增键 ----
    added = collect_added(source, target)

    # ---- 3. 检测废弃键 ----
    obsolete = collect_obsolete(source, target)

    # ---- 汇总 ----
    total_changes = len(rename_applied) + len(added) + (len(obsolete) if prune else 0)

    if added:
        print(f"\n新增键 ({len(added)}):")
        for path in added:
            print(f"  + {path}")

    if obsolete:
        if prune:
            print(f"\n废弃键 ({len(obsolete)}) — 将被移除:")
        else:
            print(
                f"\n[!] 废弃键 ({len(obsolete)}) — 在 default 中已不存在，建议确认是否需要 --rename 或 --prune:"
            )
        for path in obsolete:
            print(f"  - {path}")

    if total_changes == 0 and not obsolete:
        print("已是最新格式，无需同步。")
        return 0
    elif total_changes == 0 and obsolete:
        print("\n提示: 使用 --prune 可自动移除以上废弃键。")
        return 0

    if dry_run:
        print(f"\n[dry-run] 未修改目标文件。变更数: {total_changes}")
        return total_changes

    # ---- 4. 执行写入 ----
    if backup:
        backup_path = target_path.with_suffix(target_path.suffix + ".bak")
        backup_path.write_bytes(target_path.read_bytes())
        print(f"\n已备份至 {backup_path}")

    # 先合并新增
    merged = deep_merge(source, target)

    # 再清理废弃
    if prune and obsolete:
        removed = prune_obsolete(merged, obsolete)
        print(f"已移除 {removed} 个废弃键")

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.dump(
            merged,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            indent=2,
            width=120,
        )

    print(f"同步完成: {len(added)} 新增, {len(rename_applied)} 重命名", end="")
    if prune:
        print(f", {len(obsolete)} 移除", end="")
    print(f" -> {target_path}")
    return total_changes


def main() -> None:
    parser = argparse.ArgumentParser(description="将配置模板的格式变更同步到本地配置文件")
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path("tmp/template-conf.yaml"),
        help="源文件路径（默认: tmp/template-conf.yaml）",
    )
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=Path("config/local.yaml"),
        help="目标文件路径（默认: config/local.yaml）",
    )
    parser.add_argument(
        "-r",
        "--rename",
        action="append",
        dest="renames",
        default=[],
        metavar="OLD:NEW",
        help="将旧键值迁移到新键名，格式: 旧.键.路径:新.键.路径（可多次指定）",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="自动移除在 source 中已不存在的废弃键",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅显示差异，不修改目标文件",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="不备份目标文件",
    )
    args = parser.parse_args()

    # 解析 rename 参数
    parsed_renames: list[tuple[str, str]] = []
    for entry in args.renames:
        if ":" not in entry:
            print(f"警告: 无效的 --rename 格式 '{entry}'，应使用 '旧键:新键'")
            continue
        old, new = entry.split(":", 1)
        parsed_renames.append((old.strip(), new.strip()))

    result = sync_config(
        args.source.resolve(),
        args.target.resolve(),
        renames=parsed_renames,
        dry_run=args.dry_run,
        backup=not args.no_backup,
        prune=args.prune,
    )
    sys.exit(0 if result >= 0 else 1)


if __name__ == "__main__":
    main()
