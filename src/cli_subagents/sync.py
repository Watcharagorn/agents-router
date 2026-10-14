"""Mirror shared setup entries from a primary config folder into an account folder.

Folders are linked (junction on Windows, symlink elsewhere) and files are
copied. Every entry the sync creates is recorded in a manifest inside the
account folder, so later runs can update or remove exactly those entries.

Safety rules, because the account folder is writable by the worker it serves:
* manifest keys must have the shape `<folder>/<name>` or `<file>`; anything else is ignored;
* nothing is touched unless its parent folder is a real folder inside the
  account folder (not a link that leads somewhere else);
* links are only ever unlinked, never followed for deletion;
* a copied folder is only deleted if it still matches what was copied.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any

from cli_subagents.fsutil import (
    is_link,
    link_target,
    make_dir_link,
    read_json,
    remove_link,
    same_path,
    sha256_file,
    tree_signature,
    write_json,
)

MANIFEST = ".cli-subagents-managed.json"


@dataclass(frozen=True)
class SyncPlan:
    folders: tuple[str, ...]  # folders whose entries are mirrored one by one
    files: tuple[str, ...]  # top-level files that are copied
    skip: frozenset[tuple[str, str]] = frozenset()  # (folder, name) the account owns
    overwrite: frozenset[str] = frozenset()  # managed files the CLI itself may rewrite


def new_report() -> dict[str, Any]:
    return {"linked": [], "copied": [], "removed": [], "conflicts": [], "warnings": []}


def _valid_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and not name.startswith(".") and "/" not in name and "\\" not in name and ":" not in name


def _valid_key(key: str, plan: SyncPlan) -> bool:
    if key in plan.files:
        return True
    folder, _, name = key.partition("/")
    return folder in plan.folders and _valid_name(name)


def _inside(target: Path, relative: str) -> bool:
    """True if `target/relative`'s parent is a real folder at the expected place."""

    root = target.resolve()
    parent = target / relative
    parent = parent.parent
    if parent != target and is_link(parent):
        return False
    try:
        return parent.resolve() == (root / Path(relative).parent).resolve() and parent.resolve().is_relative_to(root)
    except OSError:
        return False


def sync(primary: Path, target: Path, plan: SyncPlan, report: dict[str, Any]) -> None:
    if is_link(target):
        report["conflicts"].append("the account folder itself is a link; not syncing into it")
        return
    target.mkdir(parents=True, exist_ok=True)
    manifest_path = target / MANIFEST
    if is_link(manifest_path):
        report["conflicts"].append(f"{MANIFEST} is a link; not syncing")
        return
    manifest = read_json(manifest_path, {}) or {}
    raw_items = manifest.get("items") if isinstance(manifest, dict) else None
    items: dict[str, dict[str, Any]] = {
        key: value
        for key, value in (raw_items or {}).items()
        if isinstance(key, str) and isinstance(value, dict) and _valid_key(key, plan)
    }

    desired: dict[str, Path] = {}
    for folder in plan.folders:
        source_dir = primary / folder
        if not source_dir.is_dir():
            continue
        for entry in source_dir.iterdir():
            if not _valid_name(entry.name) or (folder, entry.name) in plan.skip:
                continue
            desired[f"{folder}/{entry.name}"] = entry
    for name in plan.files:
        if (primary / name).is_file():
            desired[name] = primary / name

    for relative in sorted(set(items) - set(desired)):
        info = items.pop(relative)
        try:
            if _remove(target, relative, info, report):
                report["removed"].append(relative)
        except OSError as exc:
            report["warnings"].append(f"could not remove {relative}: {exc}")

    for relative, source in sorted(desired.items()):
        if not _inside(target, relative):
            report["conflicts"].append(f"{relative}: its folder in the account is a link or outside it; left alone")
            continue
        try:
            if source.is_dir():
                _sync_dir(source, target / relative, relative, items, report)
            else:
                _sync_file(source, target / relative, relative, items, report, relative in plan.overwrite)
        except OSError as exc:
            report["warnings"].append(f"could not sync {relative}: {exc}")

    write_json(manifest_path, {"version": 1, "primary": str(primary), "items": items})


def _remove(target: Path, relative: str, info: dict[str, Any], report: dict[str, Any]) -> bool:
    if not _inside(target, relative):
        report["conflicts"].append(f"{relative}: its folder in the account is a link or outside it; left alone")
        return False
    destination = target / relative
    kind = info.get("kind")
    if is_link(destination):
        remove_link(destination)
        return True
    if not destination.exists():
        return True
    if kind == "copy" and destination.is_file():
        if sha256_file(destination) == info.get("sha256"):
            destination.unlink()
            return True
        report["conflicts"].append(f"{relative}: edited in the account folder, left in place")
        return False
    if kind == "copydir" and destination.is_dir():
        if tree_signature(destination) == info.get("signature"):
            shutil.rmtree(destination)
            return True
        report["conflicts"].append(f"{relative}: changed in the account folder, left in place")
        return False
    report["conflicts"].append(f"{relative}: unexpected entry, left in place")
    return False


def _sync_dir(source: Path, destination: Path, relative: str, items: dict, report: dict) -> None:
    info = items.get(relative)
    if is_link(destination):
        current = link_target(destination)
        if current and same_path(current, source):
            items[relative] = {"kind": "link", "source": str(source)}
            return
        if not info:
            report["conflicts"].append(f"{relative}: account has its own link, left in place")
            return
        remove_link(destination)
    elif destination.exists():
        if info and info.get("kind") == "copydir" and destination.is_dir():
            if tree_signature(destination) != info.get("signature"):
                report["conflicts"].append(f"{relative}: changed in the account folder, left in place")
                return
            signature = tree_signature(source)
            if signature != info.get("source_signature"):
                shutil.rmtree(destination)
                shutil.copytree(source, destination)
                items[relative] = {
                    "kind": "copydir",
                    "source": str(source),
                    "signature": tree_signature(destination),
                    "source_signature": signature,
                }
                report["copied"].append(relative)
            return
        report["conflicts"].append(f"{relative}: account has its own copy, left in place")
        return
    kind = make_dir_link(source, destination)
    if kind == "copy":
        items[relative] = {
            "kind": "copydir",
            "source": str(source),
            "signature": tree_signature(destination),
            "source_signature": tree_signature(source),
        }
        report["copied"].append(relative)
    else:
        items[relative] = {"kind": "link", "source": str(source)}
        report["linked"].append(relative)


def _sync_file(source: Path, destination: Path, relative: str, items: dict, report: dict, overwrite: bool) -> None:
    info = items.get(relative)
    if is_link(destination):
        report["conflicts"].append(f"{relative}: is a link in the account folder, left in place")
        return
    wanted = sha256_file(source)
    if destination.exists():
        current = sha256_file(destination)
        if not info and current != wanted:
            report["conflicts"].append(f"{relative}: account has its own version, left in place")
            return
        if info and current not in (wanted, info.get("sha256")) and not overwrite:
            report["conflicts"].append(f"{relative}: edited in the account folder, left in place")
            return
    if not destination.exists() or sha256_file(destination) != wanted:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.cli-subagents.tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
        report["copied"].append(relative)
    items[relative] = {"kind": "copy", "source": str(source), "sha256": wanted}
