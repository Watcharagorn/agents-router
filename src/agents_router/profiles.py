"""Your Claude Code profiles: the config folders on this machine that agt can run jobs on with no setup.

A profile is a Claude Code folder you already use (`~/.claude`, `~/.claude-work`, a folder
listed in `[[share.folders]]`) that has been logged in. agt runs jobs there with the folder's
own setup and never links anything into it; agt share keeps the folders alike.

Only two kinds of facts are read, never credentials:
* `oauthAccount` fields of the folder's `.claude.json` (`~/.claude.json` for `~/.claude`):
  the account and organization ids (kept only as short hashes) and the plan type;
* `claude auth status --json`, which makes no model request. Its answer is cached in
  `state/claude-profiles.json` until the folder's `.claude.json` changes or ten minutes pass.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

AUTH_TTL_SECONDS = 600
CLAUDE_MARKERS = ("settings.json", ".claude.json", "projects", "CLAUDE.md", "plugins")


@dataclass
class Profile:
    name: str
    home: Path
    login: str | None = None  # short hash of oauthAccount.accountUuid: folders with one login share it
    org: str | None = None  # short hash of oauthAccount.organizationUuid
    org_type: str | None = None  # oauthAccount.organizationType, e.g. claude_team or claude_max
    logged_in: bool | None = None  # None: unknown (no claude binary, or the check failed)


def canonical(path: Path | str) -> Path:
    """The folder's real path, so symlinks and spelling differences name one folder."""

    path = Path(path).expanduser()
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def same_folder(left: Path | str | None, right: Path | str | None) -> bool:
    if left is None or right is None:
        return False
    return os.path.normcase(str(canonical(left))) == os.path.normcase(str(canonical(right)))


def default_folder() -> Path:
    return Path.home() / ".claude"


def is_default(folder: Path | str) -> bool:
    """Whether this is Claude Code's default folder, which runs without CLAUDE_CONFIG_DIR."""

    return same_folder(folder, default_folder())


def _hash(value: Any) -> str | None:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12] if value else None


def account_info(folder: Path | str) -> dict[str, str | None]:
    """Login metadata from `.claude.json`: hashed ids and the organization type. Nothing else is kept."""

    folder = Path(folder).expanduser()
    paths = [folder / ".claude.json"]
    if is_default(folder):
        paths.insert(0, Path.home() / ".claude.json")
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        account = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(account, dict) and account.get("accountUuid"):
            org_type = account.get("organizationType")
            return {"login": _hash(account.get("accountUuid")), "org": _hash(account.get("organizationUuid")),
                    "org_type": str(org_type) if isinstance(org_type, str) else None}
    return {"login": None, "org": None, "org_type": None}


def _state_file(folder: Path) -> Path | None:
    for path in ([Path.home() / ".claude.json"] if is_default(folder) else []) + [folder / ".claude.json"]:
        if path.is_file():
            return path
    return None


def _cache_path() -> Path:
    from agents_router.config import state_dir

    return state_dir() / "claude-profiles.json"


def forget(folder: Path | str | None = None) -> None:
    """Drop cached login checks (after a login, a logout or an auth failure). No folder: all of them."""

    from agents_router.fsutil import file_lock, read_json, write_json

    path = _cache_path()
    if not path.exists():
        return
    with file_lock(path.with_suffix(".lock"), timeout=10):
        data = read_json(path, {}) or {}
        if folder is None:
            data = {}
        else:
            data.pop(os.path.normcase(str(canonical(folder))), None)
        write_json(path, data)


def logged_in(folder: Path, check) -> bool | None:
    """Cached `claude auth status` for a folder. `check(folder)` runs the real one."""

    from agents_router.fsutil import file_lock, read_json, write_json

    key = os.path.normcase(str(canonical(folder)))
    state = _state_file(folder)
    try:
        stamp = state.stat().st_mtime if state else None
    except OSError:
        stamp = None
    path = _cache_path()
    cached = (read_json(path, {}) or {}).get(key) if path.exists() else None
    now = time.time()
    if isinstance(cached, dict) and cached.get("stamp") == stamp and now - float(cached.get("checked") or 0) < AUTH_TTL_SECONDS:
        return cached.get("logged_in")
    try:
        result = check(folder)
    except Exception:
        result = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.with_suffix(".lock"), timeout=10):
            data = read_json(path, {}) or {}
            data[key] = {"logged_in": result, "stamp": stamp, "checked": now}
            write_json(path, data)
    except OSError:
        pass
    return result


def candidate_folders(extra: list[Path] | None = None) -> list[Path]:
    """`~/.claude`, every `~/.claude-*` folder Claude Code has used, and any extra folders, deduplicated."""

    home = Path.home()
    folders = [home / ".claude"]
    try:
        entries = sorted(home.iterdir())
    except OSError:
        entries = []
    folders += [entry for entry in entries if entry.name.startswith(".claude-") and entry.is_dir()
                and any((entry / marker).exists() for marker in CLAUDE_MARKERS)]
    folders += [Path(folder).expanduser() for folder in extra or []]
    seen: list[Path] = []
    for folder in folders:
        if folder.is_dir() and not any(same_folder(folder, other) for other in seen):
            seen.append(folder)
    return seen


def label(folder: Path) -> str:
    """`claude` for ~/.claude, `claude-ntb` for ~/.claude-ntb: the same labels agt share uses."""

    name = folder.name.lstrip(".")
    return name if name.startswith("claude") else f"claude-{name}" if name else "claude"


def discover(*, extra: list[Path] | None = None, skip: list[Path] | None = None, check=None) -> list[Profile]:
    """Claude folders that have a login, named after the folder.

    A folder counts once `.claude.json` holds an `oauthAccount` (it was logged in at some point);
    `check(folder)` then says whether it still is. Folders in `skip` (agt's own account
    folders, registered accounts, `ignore`) are left out.
    """

    found: list[Profile] = []
    for folder in candidate_folders(extra):
        if any(same_folder(folder, other) for other in skip or []):
            continue
        info = account_info(folder)
        if not info["login"]:
            continue
        state = logged_in(folder, check) if check else None
        found.append(Profile(name=label(folder), home=folder, logged_in=state, **info))
    names = [profile.name for profile in found]
    for profile in found:
        if names.count(profile.name) > 1:  # two folders with one label (a symlink, an extra path): keep both apart
            profile.name = f"{profile.name}-{_hash(str(canonical(profile.home)))[:4]}"
    return found
