"""Point an account folder at the primary setup, so every login uses one shared pool.

Nothing is duplicated where the filesystem allows it:

* whole folders (agents, commands, ...) become one link (junction on Windows,
  symlink elsewhere) to the primary folder;
* folders whose top level the CLI also fills per account (skills, which holds
  `skills/synced`) get one link per entry instead;
* single files (CLAUDE.md) become hard links to the primary file, so an edit
  through any login changes the one shared file.

Copies are only a fallback (a hard link across drives, a folder link refused by
the OS). Every entry the sync creates is recorded in a manifest inside the
account folder so later runs can update or remove exactly those entries.

Safety rules, because the account folder is writable by the worker it serves:
* manifest keys must match the plan's shape; anything else is ignored;
* nothing is touched unless its parent is a real folder inside the account folder;
* links are only ever unlinked, never followed for deletion;
* copies are only deleted if they still match what was copied.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Any

from cli_subagents.fsutil import (
    copy_exclusive,
    is_link,
    link_target,
    make_dir_link,
    make_private_dir,
    read_json,
    remove_link,
    same_path,
    sha256_file,
    temp_sibling,
    tree_signature,
    write_json,
)

MANIFEST = ".cli-subagents-managed.json"


@dataclass(frozen=True)
class SyncPlan:
    whole: tuple[str, ...] = ()  # folders linked as a whole
    folders: tuple[str, ...] = ()  # folders mirrored entry by entry
    files: tuple[str, ...] = ()  # top-level files, hard-linked
    skip: frozenset[tuple[str, str]] = frozenset()  # (folder, name) the account owns


def new_report() -> dict[str, Any]:
    return {"linked": [], "copied": [], "removed": [], "conflicts": [], "warnings": []}


def _valid_name(name: str) -> bool:
    return (
        bool(name)
        and name not in (".", "..")
        and not name.startswith(".")
        and not any(char in name for char in "/\\:")
    )


def _valid_key(key: str, plan: SyncPlan) -> bool:
    if key in plan.files or key in plan.whole:
        return True
    folder, _, name = key.partition("/")
    return (folder in plan.folders or folder in plan.whole) and _valid_name(name)


def _inside(target: Path, relative: str) -> bool:
    """True if `target/relative`'s parent is a real folder at the expected place."""

    root = target.resolve()
    parent = (target / relative).parent
    if parent != target and is_link(parent):
        return False
    try:
        resolved = parent.resolve()
        return resolved == (root / Path(relative).parent).resolve() and resolved.is_relative_to(root)
    except OSError:
        return False


def sync(primary: Path, target: Path, plan: SyncPlan, report: dict[str, Any]) -> None:
    if is_link(target):
        report["conflicts"].append("the account folder itself is a link; not syncing into it")
        return
    make_private_dir(target)  # the account folder holds a login
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
    per_entry = list(plan.folders)
    for folder in plan.whole:
        source = primary / folder
        if not source.is_dir():
            continue
        if _can_link_whole(target, folder, items, report):
            desired[folder] = source
        else:
            per_entry.append(folder)  # the account has its own content here; share entry by entry
    for folder in per_entry:
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

    for relative in sorted(set(items) - set(desired), key=len, reverse=True):
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
                _sync_file(source, target / relative, relative, items, report)
        except OSError as exc:
            report["warnings"].append(f"could not sync {relative}: {exc}")

    write_json(manifest_path, {"version": 2, "primary": str(primary), "items": items})


def _can_link_whole(target: Path, folder: str, items: dict, report: dict) -> bool:
    """Make room for a whole-folder link; False if the account keeps its own content there."""

    destination = target / folder
    if is_link(destination) or not destination.exists():
        return True
    if not destination.is_dir():
        report["conflicts"].append(f"{folder}: account has a file with this name, left in place")
        return False
    # Clear entries an earlier per-entry sync created, then take over the folder if it's empty.
    for key in sorted((key for key in items if key.startswith(folder + "/")), key=len, reverse=True):
        if _remove(target, key, items[key], report):
            items.pop(key)
    try:
        next(destination.iterdir())
    except StopIteration:
        destination.rmdir()
        return True
    return False


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
    if kind == "hardlink" and destination.is_file():
        # Unlinking one name of a hard link never touches the other names.
        destination.unlink()
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


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _sync_file(source: Path, destination: Path, relative: str, items: dict, report: dict) -> None:
    info = items.get(relative)
    if is_link(destination):
        report["conflicts"].append(f"{relative}: is a link in the account folder, left in place")
        return
    if destination.exists() and _same_file(source, destination):
        items[relative] = {"kind": "hardlink", "source": str(source)}
        return
    wanted = sha256_file(source)
    if destination.exists():
        current = sha256_file(destination)
        recorded = info.get("sha256") if info else None
        # A managed entry may be replaced if nobody changed it in the account folder:
        # an old hard link (the primary file was saved as a new file) or an unchanged copy.
        replaceable = info is not None and (info.get("kind") == "hardlink" or current in (wanted, recorded))
        if not replaceable and current != wanted:
            report["conflicts"].append(f"{relative}: account has its own version, left in place")
            return
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A fresh random name, created exclusively: nothing already there is followed or reused.
    temporary = temp_sibling(destination)
    try:
        try:
            os.link(source, temporary)
            kind = "hardlink"
        except OSError:
            copy_exclusive(source, temporary)  # different drive: fall back to a copy
            kind = "copy"
        os.replace(temporary, destination)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    items[relative] = {"kind": kind, "source": str(source), "sha256": wanted}
    report["linked" if kind == "hardlink" else "copied"].append(relative)
