"""Where agt share works: your user folders, or one repo's own project folders.

A repo you list (`agt share repo add <path>`) is a scope of its own: its
`.claude/agents`, `.codex/agents`, `.agents/` and `.kiro/` folders share the
repo's agents and skills the way your user folders share yours, with the
same planner and engine. Its worktrees are scopes too, and join by
themselves while the repo is listed.

Each scope keeps its own state, journal, backups and attention list
(`state/share/projects/<key>/` for a repo), so runs never mix. The share code
reads the current scope from a context variable (`use()`), so the same
functions serve every scope. Picks for a repo's items go in its
`.agt/settings.toml`, under `[share.targets]`.

The list is `state/share/projects.json`. agt refuses your home folder, a
folder that holds a CLI's config folder, and anything that isn't a git
checkout. During a merge, rebase or cherry-pick, a repo is left alone.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib
from typing import Iterator

from agents_router import fsutil
from agents_router.config import state_dir

# The project folders of each CLI: (its folder, {kind: [(path, writable)]}), relative to the repo root.
PROJECT = {
    "claude": (".claude", {"agents": [(".claude/agents", True)], "skills": [(".claude/skills", True)]}),
    "codex": (".codex", {"agents": [(".codex/agents", True)], "skills": [(".agents/skills", True), (".codex/skills", False)]}),
    "antigravity": (".agents", {"agents": [(".agents/agents", True)], "skills": [(".agents/skills", True)]}),
    "kiro": (".kiro", {"agents": [(".kiro/agents", True)], "skills": [(".kiro/skills", True)]}),
}
BUSY_MARKERS = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD")


@dataclass(frozen=True)
class Scope:
    root: Path | None = None  # None: your user folders
    main: Path | None = None  # a worktree's main checkout (the listed repo)

    @property
    def project(self) -> bool:
        return self.root is not None

    @property
    def key(self) -> str:
        if self.root is None:
            return ""
        real = os.path.realpath(self.root)
        return f"{re.sub(r'[^A-Za-z0-9._-]', '-', Path(real).name)[:40]}-{hashlib.sha256(real.encode()).hexdigest()[:10]}"

    @property
    def name(self) -> str:
        return "your user folders" if self.root is None else _short(self.root)

    @property
    def settings_file(self) -> Path | None:
        """Where this repo's picks live: its own `.agt/settings.toml` (a worktree has its copy of the repo's)."""

        return None if self.root is None else self.root / ".agt" / "settings.toml"


USER = Scope()
_CURRENT: contextvars.ContextVar[Scope] = contextvars.ContextVar("agt_share_scope", default=USER)


def current() -> Scope:
    return _CURRENT.get()


@contextlib.contextmanager
def use(scope: Scope) -> Iterator[Scope]:
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


def share_root() -> Path:
    return state_dir() / "share"


def folder() -> Path:
    """This scope's state folder: `state/share`, or `state/share/projects/<key>` for a repo."""

    scope = current()
    return share_root() if not scope.project else share_root() / "projects" / scope.key


def _short(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


# The project folders -----------------------------------------------------------------


def project_folder(cli: str, root: Path) -> Path:
    return root / PROJECT[cli][0]


def project_locations(cli: str, root: Path) -> dict[str, list[tuple[Path, bool]]]:
    return {kind: [(root / path, writable) for path, writable in entries] for kind, entries in PROJECT[cli][1].items()}


def owner(folder: Path) -> Path | None:
    """The project root a CLI folder belongs to in the current scope, or None for a user folder."""

    scope = current()
    if scope.root is None:
        return None
    try:
        Path(os.path.abspath(folder)).relative_to(os.path.abspath(scope.root))
    except ValueError:
        return None
    return scope.root


def has_items(root: Path) -> bool:
    for cli in PROJECT:
        for kind, entries in project_locations(cli, root).items():
            for path, _ in entries:
                try:
                    if any(not entry.name.startswith(".") for entry in path.iterdir()):
                        return True
                except OSError:
                    continue
    return False


# Git ----------------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10, check=False,
                                stdin=subprocess.DEVNULL, **fsutil.no_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def toplevel(path: Path) -> Path | None:
    found = _git(path, "rev-parse", "--show-toplevel")
    return Path(found) if found else None


def busy(root: Path) -> str | None:
    """What git is in the middle of in this checkout (a merge, a rebase), or None."""

    found = _git(root, "rev-parse", "--absolute-git-dir")
    if not found:
        return None
    for marker in BUSY_MARKERS:  # all of them live in the worktree's own git folder
        if (Path(found) / marker).exists():
            return marker.lower().replace("_head", "").replace("_", " ").replace("-", " ")
    return None


def worktrees(root: Path) -> list[Path]:
    """The other worktrees of a checkout (not the checkout itself), as git lists them."""

    listed = _git(root, "worktree", "list", "--porcelain") or ""
    found = [Path(line[len("worktree "):]) for line in listed.splitlines() if line.startswith("worktree ")]
    real = os.path.realpath(root)
    return [path for path in found if os.path.realpath(path) != real and path.is_dir()]


def committed(root: Path, path: Path) -> bool:
    """Whether git has ever had this file (so it can come back with a checkout)."""

    try:
        relative = Path(os.path.abspath(path)).relative_to(os.path.abspath(root)).as_posix()
    except ValueError:
        return False
    return bool(_git(root, "log", "-1", "--format=%H", "--all", "--", relative))


def refuse(root: Path, user_folders: list[Path]) -> str | None:
    """Why a folder can't be a shared repo, or None. `user_folders` are your CLIs' own folders (`~/.claude` and the like)."""

    home = Path.home().resolve()
    real = Path(os.path.realpath(root))
    if real == home:
        return "your home folder holds your user folders; it can't be a repo too"
    if not real.is_dir():
        return f"no such folder: {root}"
    for folder in user_folders:
        mine = Path(os.path.realpath(folder))
        if real == mine or mine.is_relative_to(real):
            return f"{_short(real)} holds your CLI folder {_short(mine)}, which shares as a user folder"
    top = toplevel(real)
    if top is None:
        return f"{_short(real)} isn't in a git checkout"
    if Path(os.path.realpath(top)) != real:
        return f"{_short(real)} is inside the checkout {_short(top)}; add that instead"
    return None


# The list -----------------------------------------------------------------------------


def list_path() -> Path:
    return share_root() / "projects.json"


def listed() -> list[dict]:
    data = fsutil.read_json(list_path(), {}) or {}
    return [entry for entry in (data.get("repos") or []) if isinstance(entry, dict) and entry.get("root")]


def _save(repos: list[dict]) -> None:
    fsutil.make_private_dir(share_root())
    fsutil.write_json(list_path(), {"repos": repos})


def add(root: Path, how: str = "command") -> bool:
    """List a repo. Returns False when it already was."""

    real = os.path.realpath(root)
    repos = listed()
    if any(os.path.realpath(entry["root"]) == real for entry in repos):
        return False
    repos.append({"root": real, "added": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "how": how})
    _save(repos)
    return True


def remove(root: Path) -> bool:
    real = os.path.realpath(root)
    repos = listed()
    kept = [entry for entry in repos if os.path.realpath(entry["root"]) != real]
    if len(kept) == len(repos):
        return False
    _save(kept)
    return True


def scopes(*, worktrees_join: bool = True) -> list[Scope]:
    """Every listed repo that still exists, and their worktrees; a root that's gone is dropped from the list."""

    repos = listed()
    alive = [entry for entry in repos if Path(entry["root"]).is_dir()]
    if len(alive) != len(repos):
        _save(alive)
    found: list[Scope] = []
    for entry in alive:
        root = Path(entry["root"])
        found.append(Scope(root))
        if worktrees_join:
            found += [Scope(path, root) for path in worktrees(root)]
    return found


def scope_of(cwd: Path) -> Scope | None:
    """The listed repo (or one of its worktrees) that `cwd` is in."""

    real = os.path.realpath(cwd)
    for scope in sorted(scopes(), key=lambda scope: len(str(scope.root)), reverse=True):
        root = os.path.realpath(scope.root)
        if real == root or real.startswith(root + os.sep):
            return scope
    return None


# Repos your CLIs know ------------------------------------------------------------------


def known_projects(claude_folders: list[Path], codex_home: Path | None, user_folders: list[Path]) -> list[Path]:
    """Folders your CLIs keep project lists for: Claude's `.claude.json`, Codex's `[projects]`, Antigravity's list."""

    found: list[str] = []
    files = [Path.home() / ".claude.json", *(folder / ".claude.json" for folder in claude_folders)]
    for path in files:
        data = fsutil.read_json(path, {}) or {}
        projects = data.get("projects") if isinstance(data, dict) else None
        if isinstance(projects, dict):
            found += [key for key in projects if isinstance(key, str)]
    if codex_home is not None:
        try:
            data = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
            found += [key for key in (data.get("projects") or {}) if isinstance(key, str)]
        except (OSError, tomllib.TOMLDecodeError):
            pass
    gemini = fsutil.read_json(Path.home() / ".gemini" / "projects.json", {})
    if isinstance(gemini, dict):
        found += [key for key in (gemini.get("projects") or gemini) if isinstance(key, str) and key.startswith("/")]
    roots = []
    for key in dict.fromkeys(found):
        path = Path(key)
        if path.is_dir() and refuse(path, user_folders) is None and path not in roots:
            roots.append(path)
    return roots
