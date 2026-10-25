"""agt share's background runs: the watcher, the session and save entries, and the catch-up when an MCP server starts.

Every background run is a sync from your saved settings that never waits for
the lock: when another run holds it, this one leaves the work to the next.
It skips the whole run when nothing moved: the signature of the folders
(nanosecond mtimes, sizes and inodes of every entry, following linked skill
folders, plus your settings file) is taken holding the lock before planning,
and saved only after a run with no edit still in progress and nothing that
failed. agt's own writes then change the signature once more, so the next
run plans again, finds nothing to do, and saves it.

- `share watch --once` (the LaunchAgent, every 60 s): a run, then the
  heartbeat (`state/share/heartbeat.json`), then a macOS notification for new
  urgent items (conflicts, deletes waiting, a frozen folder, a failing write),
  at most once an hour, unless `notify = false`.
- `share session` (SessionStart in Claude, and in Codex when you turn it on):
  restarts a stopped watcher, catches up for up to 3 s (the rest goes on in
  the background), and prints one line when there's something to say: what
  arrived in this folder since your last session there, and what waits for
  you. Nothing at all otherwise.
- `share saved` (PostToolUse on Write|Edit|MultiEdit in Claude): a save inside
  a shared folder starts a background run, so it spreads within seconds.
- An MCP server start (`tools.startup`) refreshes the launcher when agt moved
  and starts a background run when something moved.

None of them runs inside agt's workers, writes to stdout except the session's
line, or fails the session that runs it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
from typing import Any

from agents_router import fsutil, synclog
from agents_router.config import load_config
from agents_router.share import apply, attention, engine, providers, scope, settings, state, triggers
from agents_router.share import plan as planner

STALE_AFTER = 600  # a watcher that hasn't run for 10 minutes is stopped
NOTIFY_EVERY = 3600
SESSION_BUDGET = 3.0
URGENT = ("conflict", "delete", "frozen", "failing")
ARRIVED = ("copy", "link", "update", "relink", "rename", "rename-folder", "rename-link")
TRIGGERS = ("command", "watcher", "session", "save", "mcp-start")


def signature_path() -> Path:
    return state.share_dir() / "signature.json"


def heartbeat_path() -> Path:
    return scope.share_root() / "heartbeat.json"


def good_settings_path() -> Path:
    return scope.share_root() / "settings.good.toml"


# What moved ------------------------------------------------------------------------


def signature(included: list[providers.Provider], extra: list[Path] | None = None) -> str:
    """A digest of every shared folder's entries, the skill folders they link to, and `extra` files."""

    digest = hashlib.sha256()
    seen: set[str] = set()

    def add(path: Path | str) -> None:
        digest.update(f"{path}\0{fsutil.stat_signature(Path(path))}\n".encode("utf-8", "surrogateescape"))

    for path in extra or []:
        add(path)
    for provider in included:
        if provider.cli == "codex":
            add(provider.folder / "config.toml")  # skills it disables are left out
        for kind, locations in sorted(provider.locations.items()):
            for location in locations:
                add(location.path)
                try:
                    entries = sorted(location.path.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    add(entry)
                    real = os.path.realpath(entry)
                    if real != str(entry):
                        add(real)
                    if kind != "skills" or real in seen or not os.path.isdir(real):
                        continue
                    seen.add(real)
                    for folder, dirs, files in os.walk(real):
                        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
                        for name in [*dirs, *sorted(files)]:
                            if not name.startswith("."):
                                add(os.path.join(folder, name))
    return digest.hexdigest()


def saved_signature() -> str | None:
    data = fsutil.read_json(signature_path(), {})
    return data.get("signature") if isinstance(data, dict) else None


def _save_signature(value: str) -> None:
    fsutil.make_private_dir(signature_path().parent)
    fsutil.write_json(signature_path(), {"signature": value, "time": time.time()})


def _load() -> settings.Settings | None:
    """Your share settings; a bad value keeps the last good ones (and is logged once)."""

    try:
        saved = settings.load()
    except settings.SettingsError as exc:
        synclog.refused(str(exc), None)
        try:
            text = good_settings_path().read_text(encoding="utf-8")
            return settings.parse(tomllib.loads(text).get("share", {}), good_settings_path())
        except (OSError, ValueError, settings.SettingsError):
            return None
    rendered = settings.render(saved)
    try:
        current = good_settings_path().read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != rendered and saved.enabled:
        fsutil.make_private_dir(good_settings_path().parent)
        fsutil.write_text(good_settings_path(), rendered)
    return saved


def included_folders(saved: settings.Settings) -> list[providers.Provider]:
    from agents_router.share import commands

    return commands._folders(load_config(), None, False, saved)[1]


def watched_files(saved: settings.Settings, included: list[providers.Provider]) -> list[Path]:
    """Files whose change starts a run besides the folders: your settings (and a repo's), and the instruction files."""

    from agents_router.share import instructions

    current = scope.current()
    with scope.use(scope.USER):
        files = [settings.path()]
    if current.project:
        return [*files, current.settings_file]
    return [*files, *(instructions.signature_files(included) if saved.instructions != "off" else [])]


def repo_scopes(saved: settings.Settings) -> list[scope.Scope]:
    """The listed repos (and their worktrees) a background run covers; one in the middle of a merge waits."""

    if not saved.enabled:
        return []
    return [target for target in scope.scopes(worktrees_join=saved.worktrees == "auto") if not scope.busy(target.root)]


def stale(saved: settings.Settings, included: list[providers.Provider] | None = None) -> bool:
    """Whether anything moved since the last background sync: in your user folders or in a listed repo."""

    included = included_folders(saved) if included is None else included
    if signature(included, watched_files(saved, included)) != saved_signature():
        return True
    for target in repo_scopes(saved):
        with scope.use(target):
            inner = settings.load()
            mine = included_folders(inner)
            if signature(mine, watched_files(inner, mine)) != saved_signature():
                return True
    return False


# One background run ------------------------------------------------------------------


def background_sync(trigger: str, *, if_stale: bool = True) -> tuple[str, list[str]]:
    """A sync of your user folders and every listed repo that never waits for a lock. Returns what happened and what it wrote."""

    if apply.in_worker():
        return "inside an agt worker", []
    saved = _load()
    if saved is None:
        return "the share settings can't be read", []
    if not saved.enabled:
        return "sharing is off", []
    outcome, written = _scope_sync(saved, trigger, if_stale)
    for target in repo_scopes(saved):
        with scope.use(target):
            try:
                inner_outcome, inner_written = _scope_sync(settings.load(), trigger, if_stale)
            except settings.SettingsError as exc:
                inner_outcome, inner_written = f"stopped: {exc}", []
        if inner_outcome != "nothing moved":
            outcome += f"; {target.name}: {inner_outcome}"
        written += [name for name in inner_written if name not in written]
    return outcome, written


def _scope_sync(saved: settings.Settings, trigger: str, if_stale: bool) -> tuple[str, list[str]]:
    """One scope's background sync (your user folders, or the repo `scope.use()` set)."""

    from agents_router.share import commands

    included = included_folders(saved)
    if not included:
        return "no folder shares", []
    extra = watched_files(saved, included)
    if if_stale and signature(included, extra) == saved_signature():
        return "nothing moved", []
    args = argparse.Namespace(json=False, dry_run=False)
    try:
        with apply.lock([provider.folder for provider in included], timeout=0):
            moved = signature(included, extra)
            if if_stale and moved == saved_signature():
                return "nothing moved", []
            current = state.load()
            replayed = apply.replay(current)
            plan = commands._plan(args, current, saved)
            if any(item.decision.reason == "saved less than 3 s ago" for item in plan.waiting):
                time.sleep(planner.FRESH_SECONDS)
                plan = commands._plan(args, current, saved)
            result = apply.apply(plan, current, command="agt share sync", trigger=trigger)
            result.replayed = replayed
            commands._after_renames(args, saved, current, result)
            commands._remember(plan, current, result)
            settled = not result.failed and not any(
                item.decision.action == engine.WAIT and item.decision.reason == "saved less than 3 s ago"
                for item in plan.waiting)
            if settled:
                _save_signature(moved)
    except apply.Busy:
        return "another run had the folders", []
    except state.StateError as exc:
        return f"stopped: {exc}", []
    written = sorted({outcome.name for outcome in result.written})
    if result.failed:
        return f"wrote {len(result.written)}, {len(result.failed)} failed", written
    return (f"wrote {len(result.written)}" if result.written else "nothing to write"), written


def sync_command(args: argparse.Namespace) -> int:
    """`agt share sync --if-stale / --quiet / --trigger`: one background run."""

    trigger = args.trigger if args.trigger in TRIGGERS else "command"
    try:
        outcome, written = background_sync(trigger, if_stale=args.if_stale)
    except Exception as exc:  # a background run reports, never raises
        print(f"agt share sync ({trigger}): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"agt share sync: {outcome}" + (f" ({', '.join(written)})" if written else ""))
    return 0


# The watcher -------------------------------------------------------------------------


def run_once() -> int:
    """The LaunchAgent's run: a background sync, the heartbeat, then notifications."""

    try:
        outcome, _ = background_sync("watcher")
    except Exception as exc:
        outcome = f"error: {type(exc).__name__}: {exc}"
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} agt share watch: {outcome}", file=sys.stderr)
    fsutil.make_private_dir(heartbeat_path().parent)
    fsutil.write_json(heartbeat_path(), {"time": time.time(), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                         "result": outcome, "pid": os.getpid()})
    try:
        saved = _load()
        if saved is not None and saved.enabled:
            notify(saved)
    except Exception as exc:
        print(f"agt share watch: notification failed: {exc}", file=sys.stderr)
    return 0


def health() -> dict[str, Any]:
    """The watcher as files show it: installed, its last run, whether it stopped, and the launcher."""

    installed = triggers.plist_path().exists()
    beat = fsutil.read_json(heartbeat_path(), {}) or {}
    last = beat.get("time") if isinstance(beat, dict) else None
    age = time.time() - last if isinstance(last, (int, float)) else None
    if age is None and installed:
        since = time.time() - triggers.plist_path().stat().st_mtime
        stopped = since > STALE_AFTER
    else:
        stopped = installed and age is not None and age > STALE_AFTER
    launcher = triggers.launcher_problem() if installed or triggers.launcher_path().exists() else None
    return {"installed": installed, "last_run_seconds": round(age) if age is not None else None,
            "last_result": beat.get("result") if isinstance(beat, dict) else None, "stopped": stopped,
            "launcher_problem": launcher}


def _ago(seconds: float) -> str:
    return f"{int(seconds)} s ago" if seconds < 120 else f"{int(seconds // 60)} min ago" if seconds < 7200 \
        else f"{int(seconds // 3600)} h ago"


def health_lines(saved: settings.Settings, *, check_loaded: bool = True) -> list[str]:
    """The watcher, the launcher and the hook entries, for `status` and `doctor`."""

    if not saved.enabled:
        return []
    found = health()
    lines = []
    if not saved.watcher:
        lines.append("Watcher: off (`watcher = false`); sessions and saves still catch up.")
    elif triggers.watcher_supported():
        lines.append(f"Watcher: none: {triggers.watcher_supported()}.")
    elif not found["installed"]:
        lines.append("Watcher: not installed. An agent can do it: `agt doctor --fix`.")
    elif found["stopped"]:
        when = f"its last run was {_ago(found['last_run_seconds'])}" if found["last_run_seconds"] is not None \
            else "it hasn't run yet"
        lines.append(f"Watcher stopped: {when}. An agent can do it: `agt doctor --fix`.")
    else:
        running = "" if not check_loaded or triggers.loaded() else " (launchd doesn't list it; `agt doctor --fix` loads it)"
        last = f"last run {_ago(found['last_run_seconds'])}: {found['last_result']}" if found["last_run_seconds"] is not None \
            else "no run yet"
        lines.append(f"Watcher: on, {last}{running}.")
    if found["launcher_problem"]:
        lines.append(f"Launcher: {found['launcher_problem']}. An agent can do it: `agt doctor --fix`.")
    return lines


def fix(saved: settings.Settings) -> list[str]:
    """`agt doctor --fix` for sharing: put back a damaged state, rewrite the launcher, reinstall the watcher and entries."""

    lines = []
    try:
        state.load()
    except state.StateError:
        previous = state.path().with_name(state.path().name + ".prev")
        if previous.exists():
            state.save(state.load(previous))
            lines.append(f"Put back agt share's state from {previous.name}.")
        else:
            lines.append("agt share's state is damaged and there's no earlier copy; `agt share enable` starts it again.")
    if saved.enabled:
        triggers.write_launcher()
        lines += triggers.refresh(saved, included_folders(saved))
    return lines


# Notifications -------------------------------------------------------------------------


def _summary(entries: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
    words = {"conflict": ("conflict", "conflicts"), "delete": ("item waiting to be deleted", "items waiting to be deleted"),
             "frozen": ("frozen folder", "frozen folders"), "failing": ("write that keeps failing", "writes that keep failing")}
    parts = [f"{count} {words[kind][count != 1]}" for kind, count in counts.items() if kind in words]
    return "agt share needs you: " + ", ".join(parts) + ". `agt share status` shows them."


def notify(saved: settings.Settings) -> bool:
    """A macOS notification for urgent items agt hasn't told you about, at most once an hour. Returns whether it posted."""

    command = triggers._notifier()
    if not saved.notify or not command:
        return False
    urgent = [entry for entry in attention.load() if entry.get("kind") in URGENT]
    location = scope.share_root() / "notified.json"
    data = fsutil.read_json(location, {}) or {}
    told = set(data.get("ids") or [])
    ids = sorted(entry["id"] for entry in urgent)
    new = [entry for entry in urgent if entry["id"] not in told]
    if not new or time.time() - (data.get("time") or 0) < NOTIFY_EVERY:
        if not new and ids != sorted(told):
            fsutil.write_json(location, {"ids": ids, "time": data.get("time") or 0})  # forget what's resolved
        return False
    script = f"display notification {json.dumps(_summary(urgent), ensure_ascii=False)} with title \"agt share\""
    try:
        subprocess.run([*command, "-e", script], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    fsutil.make_private_dir(location.parent)
    fsutil.write_json(location, {"ids": ids, "time": time.time()})
    return True


# Starting a background run -----------------------------------------------------------


def spawn(args: list[str]) -> int | None:
    """Start `agt <args>` in the background through the launcher. Returns its PID (None when recorded for a test)."""

    record = os.environ.get("AGT_SPAWN_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(args) + "\n")
        return None
    launcher = triggers.launcher_path()
    if launcher.exists() and os.name != "nt":
        argv, env = ["/bin/sh", str(launcher), *args], None
    else:
        package = str(Path(__file__).resolve().parents[2])
        argv = [fsutil.python_executable(), "-m", "agents_router", *args]
        env = {**os.environ, "PYTHONPATH": package}
    fsutil.make_private_dir(triggers.log_path().parent)
    return fsutil.spawn_detached(argv, triggers.log_path(), env)


def _wait(pid: int | None, seconds: float) -> bool:
    """Whether the background run finished within `seconds`."""

    if pid is None:
        return True
    process = next((process for process in fsutil._SPAWNED if process.pid == pid), None)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process is None or process.poll() is not None:
            return True
        time.sleep(0.05)
    return False


# The hook entries -------------------------------------------------------------------


def _newest_run() -> str | None:
    return next((entry.get("run") for entry in synclog.entries() if entry.get("type") == "sync"), None)


def _arrived(label: str, mark: dict[str, Any]) -> list[str]:
    """What sync runs since `mark` (the newest run at your last session here) wrote into this folder."""

    names: list[str] = []
    for entry in synclog.entries():
        if entry.get("type") != "sync":
            continue
        if entry.get("run") == mark.get("run") or (synclog.stamp(entry) or 0) < (mark.get("time") or 0) - 1:
            break
        for change in entry.get("changes") or []:
            if change.get("ok") and label in (change.get("labels") or []) and change.get("action") in ARRIVED:
                name = str(change.get("item", "")).partition("/")[2]
                if name and name not in names:
                    names.append(name)
    return names


SHOWN_AGAIN = 86400  # a waiting item shows in the first session after it appears, then at most once a day


def attention_lines(limit: int = 3, *, remember: bool = True) -> list[str]:
    """What waits for you, one line each with the command that fixes it.

    For a session (`remember`): only items not shown in the last day, which are then marked shown. Without it
    (agt's MCP server instructions, which nobody sees as a message), every waiting item.
    """

    try:
        if not settings.load().enabled:
            return []
    except settings.SettingsError:
        return []
    location = scope.share_root() / "shown.json"
    shown = fsutil.read_json(location, {}) or {}
    now = time.time()
    waiting = [entry for entry in attention.load() if entry.get("who") in ("choice", "action")
               and (not remember or now - (shown.get(entry.get("id")) or 0) >= SHOWN_AGAIN)]
    lines = [f"agt share: {entry['message']}" + (f"; `{entry['fix'][0]}`" if entry.get("fix") else "") + "."
             for entry in waiting[:limit]]
    if len(waiting) > limit:
        lines.append(f"agt share: {len(waiting) - limit} more; `agt share status` lists them.")
    if remember and waiting:
        current = {entry.get("id") for entry in attention.load()}
        kept = {key: value for key, value in shown.items() if key in current}  # resolved items are forgotten
        kept.update({entry["id"]: now for entry in waiting})
        fsutil.make_private_dir(location.parent)
        fsutil.write_json(location, kept)
    return lines


def session(label: str | None, stdin_text: str, event: str = "SessionStart") -> str | None:
    """The session entry's output: one JSON line when there's something to say, else None."""

    if apply.in_worker():
        return None
    try:
        saved = settings.load()
    except settings.SettingsError:
        return None
    if not saved.enabled:
        return None
    lines = []
    found = health()
    if saved.watcher and found["installed"] and found["stopped"] and not triggers.restart_watcher():
        lines.append("agt share's background watcher stopped and couldn't restart; `agt doctor --fix` reinstalls it.")
    included = included_folders(saved)
    caught_up = True
    if included and stale(saved, included):
        pid = spawn(["share", "sync", "--if-stale", "--quiet", "--trigger", "session"])
        caught_up = _wait(pid, SESSION_BUDGET)
    if label:
        marks_path = scope.share_root() / "sessions.json"
        marks = fsutil.read_json(marks_path, {}) or {}
        last = marks.get(label)
        if isinstance(last, dict):
            names = _arrived(label, last)
            if names:
                shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
                lines.append(f"agt share brought {len(names)} item{'s' if len(names) != 1 else ''} from your other CLIs "
                             f"since your last session here: {shown}. Anything that arrived after this session started "
                             "loads in the next one.")
        marks[label] = {"run": _newest_run(), "time": time.time()}
        fsutil.make_private_dir(marks_path.parent)
        fsutil.write_json(marks_path, marks)
    if not caught_up:
        lines.append("agt share: still catching up; anything new appears in your next session.")
    lines += _unlisted_repo(stdin_text)
    lines += attention_lines()
    if not lines:
        return None
    text = "\n".join(lines)
    return json.dumps({"systemMessage": text, "hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                      ensure_ascii=False)


def _unlisted_repo(stdin_text: str) -> list[str]:
    """In a repo that has its own agents or skills but isn't shared: one line, the first time only."""

    try:
        cwd = json.loads(stdin_text or "{}").get("cwd")
    except (ValueError, AttributeError):
        return []
    if not isinstance(cwd, str) or not cwd:
        return []
    top = scope.toplevel(Path(cwd))
    if top is None or scope.scope_of(top) is not None or not scope.has_items(top):
        return []
    location = scope.share_root() / "hinted.json"
    hinted = fsutil.read_json(location, []) or []
    real = os.path.realpath(top)
    if real in hinted:
        return []
    fsutil.make_private_dir(location.parent)
    fsutil.write_json(location, [*hinted, real])
    return [f"agt share: {scope._short(top)} has its own agents or skills, and this repo isn't shared: `agt share repo add .` "
            "shares them between your CLIs."]


def _inside(path: str, folders: list[str]) -> bool:
    return any(path == folder or path.startswith(folder.rstrip(os.sep) + os.sep) for folder in folders)


def saved_hook(stdin_text: str) -> bool:
    """The save entry: start a background run when the saved file is in a shared folder. Returns whether it did."""

    if apply.in_worker():
        return False
    try:
        data = json.loads(stdin_text or "{}")
        target = (data.get("tool_input") or {}).get("file_path")
    except (ValueError, AttributeError):
        return False
    if not isinstance(target, str) or not target:
        return False
    try:
        saved = settings.load()
    except settings.SettingsError:
        return False
    if not saved.enabled or not saved.push_on_save:
        return False
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else os.getcwd()
    real = os.path.realpath(os.path.join(cwd, os.path.expanduser(target)))
    home = Path.home()
    folders = [os.path.realpath(location.path) for folder in saved.folders
               for kind, locations in providers.locations(folder.cli, folder.path, home).items() if kind in saved.kinds
               for location in locations]
    if not _inside(real, folders):
        return False
    spawn(["share", "sync", "--if-stale", "--quiet", "--trigger", "save"])
    return True


def on_mcp_start() -> None:
    """When an MCP server starts: refresh the launcher if agt moved, and catch up in the background if something moved."""

    if apply.in_worker():
        return
    try:
        saved = settings.load()
    except settings.SettingsError:
        return
    if not saved.enabled:
        return
    triggers.refresh_launcher()
    included = included_folders(saved)
    if included and stale(saved, included):
        spawn(["share", "sync", "--if-stale", "--quiet", "--trigger", "mcp-start"])


def badge() -> str:
    """`share:2!` when something waits for you, else nothing: for a status line, so it reads files only."""

    try:
        saved = settings.load()
    except settings.SettingsError:
        return "share:!"
    if not saved.enabled:
        return ""
    count = len([entry for entry in attention.load() if entry.get("who") in ("choice", "action")])
    count += 1 if saved.watcher and health()["stopped"] else 0
    return f"share:{count}!" if count else ""
