"""agt's log: one JSON line for each thing agt changed, for tracing and audit.

Lines go to `<AGT_HOME>/state/log/<year>-<month>.jsonl`, appended with fsync,
never inside a repo or a CLI folder. Each has `time`, `type`, and the
`workspace` it happened in with its `family` (the main checkout's path, so a
repo's worktrees read as one); a change to your user settings has no
workspace. Values that can hold a secret are logged as a hash (`secret()`).
Months older than 12 go, and the oldest go first past 50 MB. `agt log`
reads it.

The types:

- `setting`: one key's old and new value, from `agt config` (who ran it, and
  the user's words when an agent passed them), a hand edit, or git (the commit
  that last changed the file, when the file matches it);
- `sync`: a share run that writes: what started it, each write, link, removal
  and backup, the copies kept, the conflicts, and how long it took;
- `turn-off`: a kind, CLI or sharing turned off, with the keep-or-remove answer
  and each copy; one that arrives by a hand edit is "kept, nobody to ask";
- `refused`: a settings value agt won't use, with its file, line and reason.

agt notices hand edits and commits when it next reads a file on a path that
writes anyway (a real sync, `agt config set`, a job start), by the file's
hash. The first time it sees a file it only remembers it. `status`, dry runs
and `config show` never write here.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib
from typing import Any, Iterator

from agents_router import fsutil, levels
from agents_router.config import state_dir

KEEP_MONTHS = 12
MAX_BYTES = 50 * 1024 * 1024
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})\.jsonl$")
AGE_RE = re.compile(r"^(\d+)\s*([mhdw])$")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
SECRET_RE = re.compile(r"(^|_)(env|headers?|token|secret|password|api_?key)s?($|_)", re.IGNORECASE)
KEEP_REFUSED = 50


def folder() -> Path:
    return state_dir() / "log"


def secret(value: Any) -> str:
    """A value that can hold a secret, as the log keeps it."""

    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def where(workspace: levels.Workspace | None) -> dict[str, str | None]:
    if workspace is None:
        return {"workspace": None, "family": None}
    return {"workspace": str(workspace.root), "family": workspace.family}


def _months() -> list[Path]:
    try:
        return sorted(path for path in folder().iterdir() if MONTH_RE.match(path.name))
    except FileNotFoundError:
        return []


def _prune(now: float) -> None:
    current = time.localtime(now)
    oldest = current.tm_year * 12 + current.tm_mon - 1 - (KEEP_MONTHS - 1)
    months = _months()
    for path in months:
        year, month = (int(part) for part in MONTH_RE.match(path.name).groups())
        if year * 12 + month - 1 < oldest:
            path.unlink(missing_ok=True)
    months = _months()
    sizes = {path: path.stat().st_size for path in months}
    while len(months) > 1 and sum(sizes[path] for path in months) > MAX_BYTES:
        months.pop(0).unlink(missing_ok=True)


def append(entry: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Add one line to this month's file, with fsync, and return it."""

    now = time.time() if now is None else now
    record = {"time": time.strftime(TIME_FORMAT, time.localtime(now)), **entry}
    fsutil.make_private_dir(folder())
    location = folder() / f"{time.strftime('%Y-%m', time.localtime(now))}.jsonl"
    handle = os.open(location, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(handle, (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        os.fsync(handle)
    finally:
        os.close(handle)
    _prune(now)
    return record


def entries() -> Iterator[dict[str, Any]]:
    """Every line, newest first. A line that isn't JSON is skipped."""

    for path in reversed(_months()):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


# What agt has seen of each settings file -----------------------------------------


def _seen_path() -> Path:
    return folder() / "seen.json"


def _seen() -> dict[str, Any]:
    data = fsutil.read_json(_seen_path(), {})
    return data if isinstance(data, dict) else {}


def _keep_seen(data: dict[str, Any]) -> None:
    fsutil.make_private_dir(folder())
    fsutil.write_json(_seen_path(), data)


def _secret_name(name: str) -> bool:
    return any(SECRET_RE.search(part) for part in name.split("."))


def _scrub(value: Any) -> Any:
    """A list's tables (`[[...]]` entries) with the values that can hold a secret hashed."""

    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: secret(item) if _secret_name(str(key)) else _scrub(item) for key, item in value.items()}
    return value


def _flat(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flat(value, name + "."))
        else:
            flat[name] = secret(value) if _secret_name(name) else _scrub(value)
    return flat


def _snapshot(location: Path) -> tuple[str, dict[str, Any] | None] | None:
    try:
        raw = location.read_bytes()
    except FileNotFoundError:
        return None
    try:
        values = _flat(tomllib.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        values = None
    return hashlib.sha256(raw).hexdigest(), values


def _origin(location: Path) -> dict[str, Any]:
    """A hand edit, or git: the commit that last changed the file, when the file still matches it."""

    run = lambda *args: subprocess.run(["git", "-C", str(location.parent), *args], capture_output=True, text=True,
                                       timeout=10, check=False, **fsutil.no_window_kwargs())
    try:
        commit = run("log", "-1", "--format=%H", "--", location.name)
        if commit.returncode == 0 and commit.stdout.strip() and run("diff", "--quiet", "HEAD", "--", location.name).returncode == 0:
            return {"by": "git", "commit": commit.stdout.strip()}
    except (OSError, subprocess.SubprocessError):
        pass
    return {"by": "hand edit"}


def share_copies(kind: str | None = None, label: str | None = None) -> list[str]:
    """The copies agt made, as "<kind>/<name> in <label>", from share's state."""

    from agents_router.share import state

    try:
        items = state.load().items
    except (state.StateError, OSError, ValueError):
        return []
    return [f"{key} in {made}" for key, record in sorted(items.items()) if kind is None or key.startswith(kind + "/")
            for made in record.get("made") or [] if label is None or made == label]


def _turn_offs(old: dict[str, Any], new: dict[str, Any], origin: dict[str, Any]) -> None:
    """A kind, CLI or sharing turned off by a hand edit: nobody to ask, so every copy stays and stops updating."""

    found = []
    if old.get("share.enabled") is True and new.get("share.enabled") is not True:
        found.append(("sharing", share_copies()))
    else:
        for kind in sorted(set(old.get("share.kinds") or []) - set(new.get("share.kinds") or [])):
            found.append((f"kind {kind}", share_copies(kind=kind)))
        labels = lambda values: {entry.get("label") for entry in values.get("share.folders") or [] if isinstance(entry, dict)}
        for label in sorted(labels(old) - labels(new)):
            found.append((label, share_copies(label=label)))
    for what, copies in found:
        append({"type": "turn-off", **where(None), "what": what, "answer": "kept, nobody to ask", **origin,
                "copies": copies})


def notice_file(location: Path, level: str, workspace: levels.Workspace | None) -> None:
    """Log what changed in a settings file since agt last saw it, and remember it as it is now."""

    found = _snapshot(location)
    data = _seen()
    files = data.setdefault("files", {})
    before = files.get(str(location))
    if found is None and before is None:
        return
    if before is not None and (found is None or before.get("hash") != found[0]):
        old = before.get("values") or {}
        new = (found[1] or {}) if found else {}
        if found is None or found[1] is not None:  # a file that doesn't parse is refused, not diffed
            origin = _origin(location) if found else {"by": "hand edit"}
            for key in sorted(set(old) | set(new)):
                if old.get(key) != new.get(key):
                    append({"type": "setting", **where(workspace if level != "user" else None), "file": str(location),
                            "level": level, "key": key, "old": old.get(key), "new": new.get(key), **origin})
            if level == "user":
                _turn_offs(old, new, origin)
    if found is None:
        files.pop(str(location), None)
    else:
        files[str(location)] = {"hash": found[0], "values": found[1] or (before or {}).get("values") or {}}
    _keep_seen(data)


def notice(workspace: levels.Workspace | None, user: Path | None = None) -> None:
    """notice_file for your user settings and, in a workspace, its two files."""

    from agents_router.config import user_settings_path

    paths = [("user", user or user_settings_path())]
    if workspace is not None:
        paths += [("workspace", workspace.settings), ("local", workspace.local)]
    for level, path in paths:
        notice_file(path, level, workspace)


def saved(location: Path) -> None:
    """Remember what agt itself just wrote, so it's never taken for a hand edit."""

    found = _snapshot(location)
    data = _seen()
    files = data.setdefault("files", {})
    if found is None:
        files.pop(str(location), None)
    else:
        files[str(location)] = {"hash": found[0], "values": found[1] or {}}
    _keep_seen(data)


def refused(message: str, workspace: levels.Workspace | None) -> None:
    """A settings value agt won't use, logged once (until the message changes)."""

    data = _seen()
    known = list(data.get("refused") or [])
    if message in known:
        return
    append({"type": "refused", **where(workspace), "message": message})
    data["refused"] = (known + [message])[-KEEP_REFUSED:]
    _keep_seen(data)


def stamp(entry: dict[str, Any]) -> float | None:
    try:
        return datetime.strptime(str(entry.get("time")), TIME_FORMAT).timestamp()
    except ValueError:
        return None


def since(text: str, now: float | None = None) -> float:
    """The cutoff for `--since`: 30m, 12h, 7d, 2w, today, yesterday, or a date like 2026-10-01."""

    now = time.time() if now is None else now
    text = text.strip().lower()
    match = AGE_RE.match(text)
    if match:
        count, unit = int(match.group(1)), match.group(2)
        return now - count * {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[unit]
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        return midnight.timestamp()
    if text == "yesterday":
        return (midnight - timedelta(days=1)).timestamp()
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise ValueError(f"--since takes 30m, 12h, 7d, 2w, today, yesterday or a date like 2026-10-01, not {text!r}") from None


# agt log --------------------------------------------------------------------------


def _shown(value: Any, missing: str) -> str:
    if value is None:
        return missing
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "none"
    return str(value)


def _by(entry: dict[str, Any]) -> str:
    if entry.get("by") == "agent":
        said = f", the user said {json.dumps(entry['user_said'], ensure_ascii=False)}" if entry.get("user_said") else ""
        return f"by an agent ({entry.get('host') or 'unknown'}){said}"
    if entry.get("by") == "git":
        return f"from git commit {str(entry.get('commit'))[:12]}"
    return {"terminal": "typed in a terminal", "command": "by a command", "hand edit": "by hand"}.get(
        str(entry.get("by")), str(entry.get("by") or ""))


def _run_line(entry: dict[str, Any]) -> str:
    changes = entry.get("changes") or []
    done = [change for change in changes if change.get("ok")]
    counts = Counter(str(change.get("action")) for change in done)
    names = sorted({str(change.get("item", "")).partition("/")[2] for change in done})
    text = ", ".join(f"{action} {count}" for action, count in sorted(counts.items())) or "nothing to change"
    if names:
        text += f" ({', '.join(names[:5])}{', …' if len(names) > 5 else ''})"
    failed = len(changes) - len(done)
    extra = [f"{failed} failed" if failed else "", f"{len(entry.get('kept') or [])} kept as yours" if entry.get("kept") else "",
             f"{len(entry.get('conflicts') or [])} conflicts" if entry.get("conflicts") else ""]
    if entry.get("turned_off"):
        extra.insert(0, f"turned off {entry['turned_off']}, {entry.get('answer')} the copies")
    extra = [part for part in extra if part]
    return f"{entry.get('command') or 'agt share'}: {text}" + (f"; {'; '.join(extra)}" if extra else "")


def line(entry: dict[str, Any]) -> str:
    when = str(entry.get("time", ""))[:16].replace("T", " ")
    kind = entry.get("type")
    if kind == "setting":
        return (f"{when}  {entry.get('key')}: {_shown(entry.get('old'), 'not set')} → {_shown(entry.get('new'), 'removed')} "
                f"in {entry.get('file')} ({entry.get('level')}, {_by(entry)})")
    if kind == "sync":
        return f"{when}  {_run_line(entry)}"
    if kind == "turn-off":
        copies = entry.get("copies") or []
        return (f"{when}  turned off {entry.get('what')} ({_by(entry)}): {entry.get('answer')}, "
                f"{len(copies)} cop{'y' if len(copies) == 1 else 'ies'}")
    if kind == "refused":
        return f"{when}  refused: {entry.get('message')}"
    return f"{when}  {kind}: {entry.get('message', '')}"


def _matches(entry: dict[str, Any], args: Any, cutoff: float | None) -> bool:
    if args.key and entry.get("key") != args.key:
        return False
    if args.item and args.item != entry.get("item") and args.item not in (entry.get("items") or []):
        return False
    if cutoff is not None and (stamp(entry) or 0) < cutoff:
        return False
    return True


def command(args: Any) -> int:
    cutoff = since(args.since) if args.since else None
    workspace = None if args.all else levels.find_workspace()
    shown: list[dict[str, Any]] = []
    others: dict[str, int] = {}
    for entry in entries():
        if not _matches(entry, args, cutoff):
            continue
        if workspace is not None and entry.get("family") not in (None, workspace.family):
            others[str(entry.get("family"))] = others.get(str(entry.get("family")), 0) + 1
            continue
        if len(shown) < args.limit:
            shown.append(entry)
    more = sum(others.values())
    if args.json:
        print(json.dumps({"workspace": str(workspace.root) if workspace else None, "entries": shown, "more": more,
                          "other_workspaces": len(others)}, indent=2, ensure_ascii=False))
        return 0
    if not shown:
        print("Nothing logged" + (f" in {workspace.root}" if workspace else "") + (" in that time." if cutoff else " yet."))
    for entry in shown:
        print(line(entry))
    if more:
        print(f"{more} more in {len(others)} other workspace{'s' if len(others) != 1 else ''}: --all")
    return 0


def add_parser(sub: Any) -> None:
    parser = sub.add_parser("log", help="What agt changed and why: settings, sync runs and turn-offs, newest first.",
                            description="Reads agt's log, this workspace's entries and your user level's by default.")
    parser.add_argument("--all", action="store_true", help="Every workspace's entries.")
    parser.add_argument("--item", help="Only entries about this item.")
    parser.add_argument("--key", help="Only changes to this setting, like codex.max_jobs.")
    parser.add_argument("--since", help="30m, 12h, 7d, 2w, today, yesterday, or a date like 2026-10-01.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
