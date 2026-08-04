"""Locate read-only resources shipped with source checkouts and installed packages."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_CHECKOUT_ROOT = _PACKAGE_ROOT.parent
_PACKAGED_RESOURCES_ROOT = _PACKAGE_ROOT / "_resources"


def bundled_resource_path(*parts: str) -> Path:
    """Return a shipped resource path for an installed wheel or source checkout."""

    packaged = _PACKAGED_RESOURCES_ROOT.joinpath(*parts)
    if packaged.exists():
        return packaged
    checkout = _CHECKOUT_ROOT.joinpath(*parts)
    if checkout.exists():
        return checkout
    return packaged


def resolve_resource_path(root_dir: Path | None, *parts: str) -> Path:
    """Prefer an operator-owned resource and fall back to the shipped version."""

    local = (root_dir or Path.cwd()).joinpath(*parts)
    if local.exists():
        return local
    return bundled_resource_path(*parts)


def resource_directories(root_dir: Path | None, name: str) -> Iterator[Path]:
    """Yield existing operator and bundled directories, in override order."""

    candidates = [
        (root_dir or Path.cwd()) / name,
        bundled_resource_path(name),
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not candidate.is_dir():
            continue
        seen.add(resolved)
        yield candidate


def find_character_resource(
    name: str,
    root_dir: Path | None = None,
    *,
    allow_absolute: bool = False,
) -> Path:
    """Resolve an operator character path or a trusted bundled character."""

    root = (root_dir or Path.cwd()).resolve()
    requested = Path(name).expanduser()
    if requested.is_absolute():
        if allow_absolute and requested.is_file():
            return requested
        raise FileNotFoundError(f"Character file not found: {name}")

    direct = (root / requested).resolve()
    if direct.is_relative_to(root) and direct.is_file():
        return direct

    relative = requested
    if relative.parts and relative.parts[0].lower() == "characters":
        relative = Path(*relative.parts[1:])
    if not relative.parts or ".." in relative.parts:
        raise FileNotFoundError(f"Character file not found: {name}")

    if relative.suffix.lower() in {".yaml", ".yml"}:
        relative_candidates = [relative]
    elif len(relative.parts) > 1:
        relative_candidates = [relative.with_suffix(".yaml"), relative.with_suffix(".yml")]
    else:
        relative_candidates = [
            relative.with_suffix(".yaml"),
            relative.with_suffix(".yml"),
            Path("zh_cn") / relative.with_suffix(".yaml"),
            Path("zh_cn") / relative.with_suffix(".yml"),
        ]

    searched: list[Path] = []
    for directory in resource_directories(root, "characters"):
        for candidate_name in relative_candidates:
            candidate = directory / candidate_name
            searched.append(candidate)
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(
        f"Character file not found: {name}; searched: {', '.join(map(str, searched))}"
    )


def logical_character_path(path: Path, root_dir: Path | None = None) -> str:
    """Return a stable path clients can send back for local or bundled characters."""

    root = (root_dir or Path.cwd()).resolve()
    if path.resolve().is_relative_to(root):
        return str(path.resolve().relative_to(root))
    for directory in resource_directories(root, "characters"):
        if path.resolve().is_relative_to(directory.resolve()):
            return str(Path("characters") / path.resolve().relative_to(directory.resolve()))
    return str(path)
