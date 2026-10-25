"""What starts agt share without you: the launcher, the macOS LaunchAgent, and agt's entries in the CLIs' hooks.

The launcher `<agt home>/bin/agt-run` is a /bin/sh script that records the
Python and the package folder agt runs from, and where agt was installed from.
The LaunchAgent and every hook entry call it by absolute path, never bare
`agt` or `python3`, so they work with launchd's minimal PATH and keep working
when agt is reinstalled elsewhere: `share enable`, `agt setup`, `agt update`
and an MCP server start rewrite it when the install moved. When the recorded
Python is gone it says so instead of failing silently: the session entry
prints one line with the reinstall command, the watcher posts a notification
at most once a day, and the save entry stays quiet.

The LaunchAgent (`io.github.watcharagorn.agents-router.share`, macOS only)
runs `agt-run share watch --once --quiet` every 60 s and when a top-level
agents or skills folder changes. launchctl is called through `AGT_LAUNCHCTL`
(a JSON list) when it's set, which is how the tests fake it; the real one is
never used for an agt home in a temp folder.

Each Claude folder that shares gets a SessionStart entry (`share session`)
and, unless `push_on_save = false`, a PostToolUse entry on
Write|Edit|MultiEdit (`share saved`) in its settings.json. Codex gets the
session entry in its hooks.json only with `codex_session = true`. agt finds its
own entries by the launcher's name, never moves anyone else's, and backs the
file up before changing it. `share disable` removes all of it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from agents_router import fsutil
from agents_router.config import home as agt_home, state_dir
from agents_router.share import providers, settings as share_settings
from agents_router.share.providers import BINARY

LABEL = "io.github.watcharagorn.agents-router.share"
LAUNCHER = "agt-run"
SESSION_MATCHER = "startup|resume|compact"
SAVE_MATCHER = "Write|Edit|MultiEdit"
HOOK_TIMEOUT = 10
SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
# An entry agt wrote, whichever agt home it came from: `<...>/bin/agt-run' share session|saved`.
OURS = re.compile(r"/bin/agt-run'? share (session|saved)\b")


class TriggerError(RuntimeError):
    """A hooks file agt can't read; it's left as it is."""


def launcher_path() -> Path:
    return agt_home().expanduser().absolute() / "bin" / LAUNCHER


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def share_state() -> Path:
    return state_dir() / "share"


def log_path() -> Path:
    return share_state() / "launchd.log"


def record_path() -> Path:
    return share_state() / "triggers.json"


def _in_temp(path: Path) -> bool:
    real = os.path.realpath(path)
    temps = {os.path.realpath(tempfile.gettempdir()), "/tmp", "/private/tmp", "/private/var/folders", "/var/folders"}
    return any(real == temp or real.startswith(temp.rstrip(os.sep) + os.sep) for temp in temps)


# The launcher ------------------------------------------------------------------------


def install_record() -> dict[str, str]:
    """The Python and package folder running now, and the agt home and settings folder the launcher pins.

    The settings folder is always pinned: with AGT_HOME set and no AGT_CONFIG_DIR, agt would look for your
    settings in the agt home instead of where they are now.
    """

    import agents_router
    from agents_router.config import user_settings_path
    from agents_router.setup import install_source

    return {"python": fsutil.python_executable(), "package": str(Path(agents_router.__file__).resolve().parent.parent),
            "home": str(agt_home().expanduser().absolute()), "source": install_source(),
            "config_dir": str(user_settings_path().parent.expanduser().absolute())}


def _notifier() -> list[str] | None:
    fake = os.environ.get("AGT_OSASCRIPT")
    if fake:
        return list(json.loads(fake))
    if sys.platform != "darwin" or _in_temp(agt_home()):
        return None
    return ["/usr/bin/osascript"]


def launcher_text(record: dict[str, str]) -> str:
    q = shlex.quote
    message = (f"agt share is stopped: its Python at {record['python']} is gone. "
               f"Reinstall with `uv tool install --reinstall --managed-python {record['source']}`.")
    session = json.dumps({"systemMessage": message,
                          "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message}})
    notifier = _notifier()
    script = f"display notification {json.dumps(message, ensure_ascii=False)} with title \"agt share\""
    notify = " ".join(q(part) for part in notifier) + f" -e {q(script)} >/dev/null 2>&1" if notifier else ":"
    config = f"AGT_CONFIG_DIR={q(record['config_dir'])}; export AGT_CONFIG_DIR\n" if record.get("config_dir") else ""
    return f"""#!/bin/sh
# Written by agt (Agents Router); agt rewrites it when it moves. It runs agt from the
# install below, so the background watcher and the CLI hook entries never depend on PATH.
PY={q(record['python'])}
PKG={q(record['package'])}
AGT_HOME={q(record['home'])}; export AGT_HOME
{config}PYTHONUTF8=1; export PYTHONUTF8
LOG="$AGT_HOME/state/share/launcher.log"
if [ ! -x "$PY" ] || [ ! -d "$PKG/agents_router" ]; then
  mkdir -p "$AGT_HOME/state/share" 2>/dev/null
  printf '%s %s\\n' "$(date '+%Y-%m-%dT%H:%M:%S')" {q(message)} >> "$LOG" 2>/dev/null
  case "$1 $2" in
    "share session") printf '%s\\n' {q(session)} ;;
    "share watch")
      MARK="$AGT_HOME/state/share/launcher-notified"
      if [ -z "$(find "$MARK" -mtime -1 2>/dev/null)" ]; then
        touch "$MARK" 2>/dev/null
        {notify}
      fi ;;
  esac
  exit 0
fi
case "$1 $2" in
  "share session"|"share saved")
    # A hook entry never fails the session that runs it.
    mkdir -p "$AGT_HOME/state/share" 2>/dev/null
    PYTHONPATH="$PKG" "$PY" -s -m agents_router "$@" 2>>"$LOG"
    exit 0 ;;
esac
PYTHONPATH="$PKG" exec "$PY" -s -m agents_router "$@"
"""


def write_launcher() -> tuple[Path, bool]:
    """(Re)write the launcher for the running install. Returns its path and whether it changed."""

    path = launcher_path()
    text = launcher_text(install_record())
    try:
        if path.read_text(encoding="utf-8") == text:
            return path, False
    except OSError:
        pass
    fsutil.make_private_dir(path.parent)
    fsutil.write_text(path, text)
    path.chmod(0o755)
    return path, True


def launcher_problem() -> str | None:
    """Why the launcher can't run agt, or None when it can."""

    path = launcher_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return f"{path} is missing"
    found = {key: shlex.split(value)[0] for key, value in re.findall(r"^(PY|PKG)=(.*)$", text, re.M) if value}
    python, package = found.get("PY"), found.get("PKG")
    if not python or not os.access(python, os.X_OK):
        return f"its Python ({python}) is gone"
    if not package or not (Path(package) / "agents_router").is_dir():
        return f"its package folder ({package}) is gone"
    return None


def refresh_launcher() -> bool:
    """Rewrite the launcher when the install it records isn't the running one (only when it exists). Returns whether it changed."""

    if not launcher_path().exists():
        return False
    return write_launcher()[1]


# The LaunchAgent -----------------------------------------------------------------------


def _launchctl_command() -> list[str] | None:
    fake = os.environ.get("AGT_LAUNCHCTL")
    if fake:
        return list(json.loads(fake))
    if sys.platform != "darwin" or _in_temp(agt_home()):
        return None  # never the real launchd for a throwaway agt home
    return ["/bin/launchctl"]


def _launchctl(*args: str) -> subprocess.CompletedProcess | None:
    command = _launchctl_command()
    if command is None:
        return None
    try:
        return subprocess.run([*command, *args], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def _domain() -> str:
    return f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/0"


def watcher_supported() -> str | None:
    """Why there's no background watcher here, or None when there can be one."""

    if os.environ.get("AGT_LAUNCHCTL"):
        return None
    if sys.platform != "darwin":
        return "the background watcher needs macOS; sessions and MCP server starts catch up instead"
    if _in_temp(agt_home()):
        return "agt's home is in a temp folder, so the background watcher stays off"
    return None


def path_folders() -> list[str]:
    """PATH for the watcher: the folders of the CLIs and git found now, then the system folders."""

    found = []
    for binary in [*BINARY.values(), "git"]:
        located = shutil.which(binary)
        if located:
            found.append(str(Path(located).parent))
    return list(dict.fromkeys([*found, *SYSTEM_PATH]))


def watch_paths(included: list[providers.Provider]) -> list[str]:
    paths = []
    for provider in included:
        for locations in provider.locations.values():
            paths += [str(location.path) for location in locations if location.path.is_dir()]
    return sorted(dict.fromkeys(paths))


def plist_data(included: list[providers.Provider], record: dict[str, str]) -> dict[str, Any]:
    environment = {"PATH": os.pathsep.join(path_folders()), "AGT_HOME": record["home"], "PYTHONUTF8": "1"}
    if record.get("config_dir"):
        environment["AGT_CONFIG_DIR"] = record["config_dir"]
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", str(launcher_path()), "share", "watch", "--once", "--quiet"],
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "StartInterval": 60,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "WatchPaths": watch_paths(included),
        "WorkingDirectory": record["home"],
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
    }


def loaded() -> bool:
    result = _launchctl("print", f"{_domain()}/{LABEL}")
    return result is not None and result.returncode == 0


def install_watcher(included: list[providers.Provider]) -> tuple[bool, str]:
    """Write the LaunchAgent and load it (again when it changed). Returns whether it runs, and a line saying so."""

    why = watcher_supported()
    if why:
        return False, why
    data = plistlib.dumps(plist_data(included, install_record()))
    path = plist_path()
    try:
        changed = path.read_bytes() != data
    except OSError:
        changed = True
    if changed:
        fsutil.make_private_dir(path.parent)
        fsutil.make_private_dir(share_state())
        fsutil.write_private(path, data)
    running = loaded()
    if changed or not running:
        if running:
            _launchctl("bootout", f"{_domain()}/{LABEL}")
        result = _launchctl("bootstrap", _domain(), str(path))
        if result is None or result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() if result else "launchctl isn't available"
            return False, f"the background watcher didn't start: launchctl bootstrap failed ({detail})"
    return True, "the background watcher runs every minute and when an agents or skills folder changes"


def restart_watcher() -> bool:
    """Load the LaunchAgent again (a stopped watcher). Returns whether launchctl took it."""

    if watcher_supported() or not plist_path().exists():
        return False
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    result = _launchctl("bootstrap", _domain(), str(plist_path()))
    return result is not None and result.returncode == 0


def remove_watcher() -> bool:
    """Unload and delete the LaunchAgent. Returns whether there was one."""

    path = plist_path()
    had = path.exists()
    if had or (not watcher_supported() and loaded()):
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    if had:
        path.unlink(missing_ok=True)
    return had


# Hook entries ------------------------------------------------------------------------


def hook_command(sub: str, label: str | None = None) -> str:
    """The shell command of an agt entry: it runs the launcher, and does nothing once the launcher is gone."""

    launcher = shlex.quote(str(launcher_path()))
    extra = f" --label {shlex.quote(label)}" if label else ""
    return f"[ -x {launcher} ] && exec {launcher} share {sub}{extra} || exit 0"


def _ours(handler: Any) -> bool:
    return isinstance(handler, dict) and isinstance(handler.get("command"), str) and bool(OURS.search(handler["command"]))


def _without_ours(hooks: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            result[event] = groups
            continue
        kept = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                handlers = [handler for handler in group["hooks"] if not _ours(handler)]
                if not handlers and group["hooks"]:
                    continue  # a group that held only agt's entry goes with it
                group = {**group, "hooks": handlers}
            kept.append(group)
        if kept or not groups:
            result[event] = kept
    return result


def _with_ours(hooks: dict[str, Any], wanted: dict[str, list[tuple[str, str]]]) -> dict[str, Any]:
    """Your entries as they are, then agt's at the end of each event (Codex keys trust by position)."""

    result = _without_ours(hooks)
    for event, entries in wanted.items():
        for matcher, command in entries:
            result.setdefault(event, []).append(
                {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]})
    return result


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ""
    except OSError as exc:
        raise TriggerError(f"can't read {path} ({exc}); it was left as it is") from None
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise TriggerError(f"{path} isn't valid JSON ({exc}); it was left as it is") from None
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise TriggerError(f"{path} doesn't have the expected shape; it was left as it is")
    return data, text


def _indent(text: str) -> int:
    match = re.search(r"^( +)\S", text, re.M)
    return len(match.group(1)) if match else 2


def _backup(path: Path, text: str, label: str) -> Path:
    folder = share_state() / "backups" / "hooks" / time.strftime("%Y%m%d-%H%M%S")
    fsutil.make_private_dir(folder)
    target = folder / f"{label}-{path.name}"
    fsutil.write_private(target, text.encode("utf-8"))
    return target


def set_entries(path: Path, wanted: dict[str, list[tuple[str, str]]], label: str) -> bool:
    """Make agt's entries in a hooks file exactly `wanted` (none to remove them). Returns whether the file changed.

    Every other key and entry stays as it is; the file is backed up first and written through a link to it.
    """

    data, text = _read_json(path)
    before = data.get("hooks") or {}
    after = _with_ours(before, wanted)
    if after == before:
        return False
    if not text and not wanted:
        return False
    updated = dict(data)
    if after:
        updated["hooks"] = after
    else:
        updated.pop("hooks", None)  # agt's were the only ones
    if text:
        _backup(path, text, label)
    if not updated and not wanted:
        path.unlink()  # the file held nothing but agt's entries (agt made it)
        return True
    rendered = json.dumps(updated, indent=_indent(text), ensure_ascii=False) + ("\n" if not text or text.endswith("\n") else "")
    if path.exists():
        fsutil.write_preserving(path, rendered.encode("utf-8"))
    else:
        fsutil.make_private_dir(path.parent)
        fsutil.write_text(path, rendered)
    return True


def has_entries(path: Path) -> bool:
    try:
        data, _ = _read_json(path)
    except TriggerError:
        return False
    return any(_ours(handler) for groups in (data.get("hooks") or {}).values() if isinstance(groups, list)
               for group in groups if isinstance(group, dict) for handler in group.get("hooks") or [])


def claude_entries(label: str, push_on_save: bool) -> dict[str, list[tuple[str, str]]]:
    wanted = {"SessionStart": [(SESSION_MATCHER, hook_command("session", label))]}
    if push_on_save:
        wanted["PostToolUse"] = [(SAVE_MATCHER, hook_command("saved"))]
    return wanted


def codex_entries(label: str) -> dict[str, list[tuple[str, str]]]:
    return {"SessionStart": [(SESSION_MATCHER, hook_command("session", label))]}


def _hooks_file(cli: str, folder: Path) -> Path | None:
    return folder / "settings.json" if cli == "claude" else folder / "hooks.json" if cli == "codex" else None


def _recorded() -> list[dict[str, str]]:
    data = fsutil.read_json(record_path(), {})
    return [entry for entry in (data.get("files") or []) if isinstance(entry, dict)] if isinstance(data, dict) else []


def _record(files: list[dict[str, str]]) -> None:
    fsutil.make_private_dir(share_state())
    fsutil.write_json(record_path(), {"files": files})


# All of it ---------------------------------------------------------------------------


def refresh(saved: share_settings.Settings, included: list[providers.Provider]) -> list[str]:
    """Make the launcher, the watcher and the hook entries match the settings. Returns one line per trigger.

    With sharing off it removes them all. A hooks file agt can't read is left as it is and named.
    """

    if not saved.enabled:
        return remove_all()
    lines = []
    write_launcher()
    if saved.watcher:
        running, line = install_watcher(included)
        lines.append(line[0].upper() + line[1:] + ".")
    elif remove_watcher():
        lines.append("The background watcher is off (`watcher = false`); it was removed.")
    files = []
    for provider in included:
        path = _hooks_file(provider.cli, provider.folder)
        if path is None or os.name == "nt":  # the entries are /bin/sh commands; Windows catches up at MCP start
            continue
        if provider.cli == "claude":
            wanted = claude_entries(provider.label, saved.push_on_save)
        elif saved.codex_session:
            wanted = codex_entries(provider.label)
        else:
            wanted = {}
        try:
            set_entries(path, wanted, provider.label)
        except TriggerError as exc:
            lines.append(f"{provider.label}: {exc}.")
            continue
        if wanted:
            files.append({"cli": provider.cli, "label": provider.label, "path": str(path)})
            what = "session start" + (" and saves in Claude Code" if "PostToolUse" in wanted else "")
            lines.append(f"{provider.label}: agt catches up at each {what} ({providers._short(path)}).")
    for entry in _recorded():  # folders that stopped sharing, or switches turned off
        if not any(entry.get("path") == kept["path"] for kept in files):
            try:
                set_entries(Path(entry["path"]), {}, entry.get("label") or "hooks")
            except (TriggerError, KeyError):
                pass
    _record(files)
    return lines


def remove_all() -> list[str]:
    """Take out the watcher and every hook entry agt wrote. The launcher stays (it's harmless)."""

    lines = []
    if remove_watcher():
        lines.append("Removed the background watcher.")
    for entry in _recorded():
        try:
            if set_entries(Path(entry["path"]), {}, entry.get("label") or "hooks"):
                lines.append(f"Removed agt's entries from {providers._short(Path(entry['path']))}.")
        except (TriggerError, KeyError) as exc:
            lines.append(f"Couldn't remove agt's entries: {exc}.")
    _record([])
    return lines
