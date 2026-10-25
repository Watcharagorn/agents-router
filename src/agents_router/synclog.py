"""agt's log: one JSON line for each thing agt changed, for tracing and audit.

Lines go to `<AGT_HOME>/state/log/<year>-<month>.jsonl`, appended with fsync,
never inside a repo or a CLI folder. Each has `time`, `type`, and the
`workspace` it happened in with its `family` (the main checkout's path, so a
repo's worktrees read as one); a change to your user settings has no
workspace. Values that can hold a secret are logged as a hash (`secret()`).
Months older than 12 go, and the oldest go first past 50 MB. `agt log`
reads it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterator

from agents_router import fsutil, levels
from agents_router.config import state_dir

KEEP_MONTHS = 12
MAX_BYTES = 50 * 1024 * 1024
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})\.jsonl$")
AGE_RE = re.compile(r"^(\d+)\s*([mhdw])$")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


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
    return {"terminal": "typed in a terminal", "command": "by a command"}.get(str(entry.get("by")), str(entry.get("by") or ""))


def line(entry: dict[str, Any]) -> str:
    when = str(entry.get("time", ""))[:16].replace("T", " ")
    if entry.get("type") == "setting":
        return (f"{when}  {entry.get('key')}: {_shown(entry.get('old'), 'not set')} → {_shown(entry.get('new'), 'removed')} "
                f"in {entry.get('file')} ({entry.get('level')}, {_by(entry)})")
    return f"{when}  {entry.get('type')}: {entry.get('message', '')}"


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
    parser = sub.add_parser("log", help="What agt changed and why: settings changes, newest first.",
                            description="Reads agt's log, this workspace's entries and your user level's by default.")
    parser.add_argument("--all", action="store_true", help="Every workspace's entries.")
    parser.add_argument("--item", help="Only entries about this item.")
    parser.add_argument("--key", help="Only changes to this setting, like codex.max_jobs.")
    parser.add_argument("--since", help="30m, 12h, 7d, 2w, today, yesterday, or a date like 2026-10-01.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
