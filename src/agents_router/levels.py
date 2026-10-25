"""Where agt's settings come from: one `settings.toml` per level, like Claude Code's settings.

    user       ~/.agt/settings.toml                  your settings for every workspace
    workspace  <workspace>/.agt/settings.toml        committed: the team's settings for a repo
    local      <workspace>/.agt/settings.local.toml  yours for that repo; never committed

Later levels win, key by key. `$AGT_CONFIG_DIR` moves the user file; with only
`AGT_HOME` set it's `<AGT_HOME>/settings.toml`, so `agt --home` isolates it too.

The workspace is found from a folder without running git:

- inside a checkout: the nearest folder with `.agt/` between it and the
  checkout's root, both included, else the root. It never looks above the
  root, so a worktree, submodule or nested clone never uses an outer repo's;
- outside git: the nearest folder with `.agt/` at or above it, else the folder;
- your home folder is never a workspace (its `.agt/` is the user level).

A linked worktree without its own `settings.local.toml` reads the main
checkout's, so your account picks follow you.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tomllib
from typing import Any, Callable

from agents_router import fsutil

LEVELS = ("user", "workspace", "local")
HEADER_RE = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\-\"' ]+?)\s*\]\]?\s*(?:#.*)?$")  # not a nested array's line


class SettingsError(ValueError):
    """A settings file can't be used; the message names the file, and the line when agt can tell."""


@dataclass
class Workspace:
    root: Path
    checkout: Path | None = None  # the git checkout it's in
    main: Path | None = None  # for a linked worktree: the main checkout

    @property
    def folder(self) -> Path:
        return self.root / ".agt"

    @property
    def settings(self) -> Path:
        return self.folder / "settings.toml"

    @property
    def local(self) -> Path:
        return self.folder / "settings.local.toml"

    def local_fallback(self) -> Path | None:
        """The main checkout's `settings.local.toml`, for a worktree that has none of its own."""

        if self.main is None or self.checkout is None:
            return None
        return self.main / self.root.relative_to(self.checkout) / ".agt" / "settings.local.toml"


@dataclass
class Layer:
    level: str
    path: Path
    text: str
    data: dict[str, Any]


def _main_checkout(checkout: Path) -> Path | None:
    """For a linked worktree (a `.git` file whose folder has `commondir`), the main checkout."""

    marker = checkout / ".git"
    if not marker.is_file():
        return None
    try:
        line = next((line for line in marker.read_text(encoding="utf-8").splitlines() if line.startswith("gitdir:")), "")
        gitdir = (checkout / line.partition(":")[2].strip()).resolve()
        common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except (OSError, ValueError):
        return None  # a submodule has no commondir
    return common.parent if common.name == ".git" else None


def find_workspace(cwd: Path | str | None = None) -> Workspace | None:
    """The workspace `cwd` is in, or None in your home folder."""

    start = Path(cwd or os.getcwd()).resolve()
    home = Path.home().resolve()
    checkout = next((folder for folder in (start, *start.parents) if os.path.lexists(folder / ".git")), None)
    chain = []  # the folders that may hold the workspace's .agt/
    for folder in (start, *start.parents):
        if checkout is None and folder == home:
            break
        chain.append(folder)
        if folder == checkout:
            break
    root = next((folder for folder in chain if folder != home and (folder / ".agt").is_dir()), checkout or start)
    if root == home:
        return None
    return Workspace(root, checkout, _main_checkout(checkout) if checkout is not None else None)


def read(level: str, path: Path) -> Layer | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SettingsError(f"{path} can't be read ({exc})") from None
    try:
        return Layer(level, path, text, tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path}: {exc}") from None


def layers(user: Path, workspace: Workspace | None) -> list[Layer]:
    """The settings files that exist, lowest level first."""

    paths = [("user", user)]
    if workspace is not None:
        local = workspace.local
        if not local.exists():
            local = workspace.local_fallback() or local
        paths += [("workspace", workspace.settings), ("local", local)]
    found, seen = [], set()
    for level, path in paths:
        if path in seen:  # $AGT_CONFIG_DIR may point inside a workspace
            continue
        seen.add(path)
        layer = read(level, path)
        if layer is not None:
            found.append(layer)
    return found


def _header(line: str) -> str | None:
    match = HEADER_RE.match(line)
    return re.sub(r"\s*\.\s*", ".", match.group(1)).replace('"', "").replace("'", "") if match else None


def line_of(text: str, table: str | None, key: str) -> int | None:
    """The line that sets `key` in `table` (None: before any table), or the `[[table.key]]` header."""

    current = None
    pattern = re.compile(rf"^\s*[\"']?{re.escape(key)}[\"']?\s*=")
    for number, line in enumerate(text.splitlines(), 1):
        header = _header(line)
        if header is not None:
            current = header
            if header == (f"{table}.{key}" if table else key):
                return number
        elif current == table and pattern.match(line):
            return number
    return None


def where(layer: Layer, table: str | None, key: str | None) -> str:
    """`<file>:<line>` for a key, or just the file when the line can't be found."""

    line = line_of(layer.text, table, key) if key else None
    return f"{layer.path}:{line}" if line else str(layer.path)


def split(text: str, ours: Callable[[str], bool]) -> tuple[list[str], list[str]]:
    """The lines outside the tables whose header `ours` picks, and the lines inside them."""

    kept: list[str] = []
    picked: list[str] = []
    inside = False
    for line in text.splitlines():
        header = _header(line)
        if header is not None:
            inside = ours(header)
        (picked if inside else kept).append(line)
    return kept, picked


def write(location: Path, text: str) -> Path:
    tomllib.loads(text)  # never write a file agt can't read back
    fsutil.make_private_dir(location.parent)
    fsutil.write_text(location, text)
    return location


def replace_tables(location: Path, ours: Callable[[str], bool], rendered: str) -> Path:
    """Rewrite the tables whose header `ours` picks with `rendered`, keeping every other line."""

    try:
        text = location.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    kept = split(text, ours)[0]
    while kept and not kept[-1].strip():
        kept.pop()
    if not kept:
        kept = ["# agt settings. See docs/configuration.md."]
    return write(location, "\n".join(kept) + "\n" + ("\n" + rendered if rendered else ""))
