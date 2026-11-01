"""What every command that writes agt's settings shares: who is asking, which file, backup, lock and log.

`agt config set|unset` and `agt roles config` both write through these, so
the rules stay one: an agent needs the user's words for a change that loosens
safety (exit 3 without), every write keeps a backup first, two writers never
interleave, and the change goes to agt's log.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Iterator

from agents_router import config, fsutil, hosts, levels
from agents_router.share import gitexclude

NEEDS_WORDS = 3


def agent() -> bool:
    """An agent runs this command (agt sees CLAUDECODE, CODEX_THREAD_ID and the like)."""

    return hosts.detect_host() != "unknown"


def terminal() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def who(words: str | None) -> dict[str, Any]:
    if agent():
        return {"by": "agent", "host": hosts.detect_host(), "user_said": words}
    return {"by": "terminal" if terminal() else "command", "user_said": words}


def files(workspace: levels.Workspace | None) -> dict[str, Path]:
    found = {"user": config.user_settings_path()}
    if workspace is not None:
        found.update(workspace=workspace.settings, local=workspace.local)
    return found


def read_text(location: Path) -> str:
    try:
        return location.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def backup(location: Path, level: str) -> Path | None:
    if not location.exists():
        return None
    folder = config.state_dir() / "backups" / "settings"
    fsutil.make_private_dir(folder)
    target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{level}.toml"
    shutil.copy2(location, target)
    return target


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """One settings writer at a time; re-read the file inside the block."""

    with fsutil.file_lock(config.state_dir() / "locks" / "settings.lock", timeout=30.0):
        yield


def exclude_local(location: Path) -> str | None:
    """Keep a new `.agt/settings.local.toml` out of git: one line in the repo's info/exclude."""

    found = gitexclude.repo_of(location)
    if not found:
        return None
    top, exclude = found
    line = gitexclude.entry(top, location)
    try:
        text = exclude.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if not line or line in text.splitlines():
        return None
    exclude.parent.mkdir(parents=True, exist_ok=True)
    body = text + ("" if not text or text.endswith("\n") else "\n") + "# agt: your own settings for this workspace\n" + line + "\n"
    fsutil.write_preserving(exclude, body.encode("utf-8"), new=not exclude.exists())
    return str(exclude)
