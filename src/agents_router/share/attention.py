"""What waits for you: agt share's attention list, and its log of events.

Every writing run updates `state/share/attention.json`. Each entry says what
happened, who acts on it (`who`):

- `choice`: you decide (a conflict, a new item waiting for targets);
- `action`: you do something by hand (fix a file agt can't read);
- `agent`: an agent can run the fix alone (for kinds still to come, such as a
  stopped watcher).

Each entry also has the command that fixes it (`fix`, one or more), and when
agt first saw it (`since`), which survives later runs. A command that names
one item replaces only that item's entries; a frozen folder belongs to the
whole run. `status` works the list out fresh and never writes it.

Events that pass (a write skipped because a copy changed meanwhile, a write
that failed) go to `state/share/events.jsonl`, at most one line per item,
kind and day, and the file is kept under 256 KB.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from agents_router import fsutil
from agents_router.share import engine
from agents_router.share import plan as planner
from agents_router.share.state import State, share_dir

FAILING_RUNS = 3
EVENTS_LIMIT = 256 * 1024
FOLDER = "folder"  # the item key of entries about a whole folder


def path() -> Path:
    return share_dir() / "attention.json"


def events_path() -> Path:
    return share_dir() / "events.jsonl"


def _entry(kind: str, item: str, who: str, message: str, fix: list[str], label: str = "") -> dict[str, Any]:
    return {"id": ":".join(part for part in (kind, item, label) if part), "kind": kind, "item": item, "who": who,
            "message": message, "fix": fix}


def entries(plan: planner.Plan, current: State) -> list[dict[str, Any]]:
    """The list as this plan sees it, without `since`."""

    found = []
    for item in plan.items:
        name, decision = item.name, item.decision
        if decision is None:
            continue
        # The copies the conflict is about (the edited ones), else every copy.
        where = ", ".join(decision.labels or sorted({label for copy in item.copies for label in copy.labels}))
        if decision.action == engine.CONFLICT:
            found.append(_entry("conflict", item.key, "choice", f"{name} differs between {where} ({decision.reason})",
                                [f"agt share resolve {name}"]))
        elif decision.action == engine.PENDING_DELETE:
            found.append(_entry("delete", item.key, "choice",
                                f"{name}'s original was deleted in {', '.join(decision.labels)}; its other copies wait",
                                [f"agt share rm {name}"]))
        elif decision.action == engine.WAIT and decision.reason != "saved less than 3 s ago":
            found.append(_entry("unreadable", item.key, "action", f"agt can't read {name}: {decision.reason}", []))
        elif decision.reason == planner.NEW_WAITING:
            found.append(_entry("new", item.key, "choice", f"{name} is new and waits for you to pick where it goes",
                                [f"agt share pick {name} --in <label>"]))
        failing = (current.items.get(item.key) or {}).get("failing") or {}
        if failing.get("runs", 0) >= FAILING_RUNS:
            found.append(_entry("failing", item.key, "action",
                                f"agt couldn't write {name} in the last {failing['runs']} runs: {failing.get('message', '')}", []))
    for frozen in plan.frozen:
        label = frozen["label"]
        found.append(_entry("frozen", FOLDER, "choice",
                            f"{frozen['gone']} of {frozen['known']} {frozen['kind']} disappeared from {label} at once, "
                            "so agt deletes nothing there until you say", [f"agt share sync --allow-deletes {label}",
                                                                          f"agt share sync --refill {label}"], label))
    for skipped in plan.unrenamed:
        found.append(_entry("rename", f"{skipped['kind']}/{skipped['old']}", "choice", skipped["message"],
                            [f"agt share mv {skipped['old']} {skipped['new']}"] if skipped.get("new") else []))
    if plan.instructions is not None:
        from agents_router.share import instructions

        found += instructions.attention(plan.instructions)
    if plan.mcp is not None:
        from agents_router.share import mcp

        found += mcp.attention(plan.mcp)
    return found


def load() -> list[dict[str, Any]]:
    data = fsutil.read_json(path(), {})
    return list(data.get("entries") or []) if isinstance(data, dict) else []


def save(plan: planner.Plan, current: State, keys: set[str] | None = None) -> list[dict[str, Any]]:
    """Update the list from a run's plan. With `keys`, only those items' entries are replaced."""

    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    before = load()
    since = {entry.get("id"): entry.get("since") for entry in before}
    kept = [] if keys is None else [entry for entry in before if entry.get("item") not in keys]
    fresh = [{**entry, "since": since.get(entry["id"]) or now} for entry in entries(plan, current)
             if keys is None or entry["item"] in keys]
    result = kept + fresh
    if result != before:
        fsutil.make_private_dir(path().parent)
        fsutil.write_json(path(), {"entries": result})
    return result


def event(kind: str, item: str, message: str) -> None:
    """Log something that happened, once per item, kind and day."""

    day = time.strftime("%Y-%m-%d")
    location = events_path()
    try:
        lines = location.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        with contextlib.suppress(ValueError):
            seen = json.loads(line)
            if (seen.get("day"), seen.get("item"), seen.get("kind")) == (day, item, kind):
                return
    line = json.dumps({"day": day, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, "item": item,
                       "message": message}, ensure_ascii=False)
    if sum(len(entry) + 1 for entry in lines) + len(line) > EVENTS_LIMIT:
        lines = lines[len(lines) // 2:]  # the older half goes
        fsutil.make_private_dir(location.parent)
        fsutil.write_text(location, "\n".join(lines) + ("\n" if lines else ""))
    fsutil.make_private_dir(location.parent)
    with location.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
