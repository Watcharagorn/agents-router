"""Writing a share plan to the CLI folders, so that a crash or an edit mid-run loses nothing.

For each change:

1. Read the copy again; if it changed since the plan read it, skip the item this run.
2. Back up what's there to `state/share/backups/<run>/`, never inside a CLI folder.
3. fsync a journal entry: the path, what's there now, and what agt will write.
4. Write: a new agent file is linked into place (it never replaces one that
   appeared meanwhile); an update keeps the file's mode and hard links; a
   skill is a link to its real folder.
5. Read it back. Only a copy whose digest is the item's version counts as
   done; anything else is put back from the backup and reported.

An item's state record changes only when all of its writes worked. When some
didn't, the copies that were written look like the same edit next run, so
they spread instead of being reverted. A run that was cut off is replayed from
the journal at the start of the next one: a copy at the journal's new
fingerprint is done, one at its old fingerprint is still to do, and anything
else is your edit.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time
from typing import Iterator

from agents_router import fsutil
from agents_router.share import engine, gitexclude, model, names, toolmap
from agents_router.share import plan as planner
from agents_router.share.state import State, save as save_state, share_dir


class Busy(RuntimeError):
    """Another agt share run holds the lock for these folders."""


@dataclass
class Outcome:
    kind: str
    name: str
    labels: list[str]
    action: str
    path: Path
    ok: bool
    message: str = ""
    backup: Path | None = None


@dataclass
class Result:
    run: str
    outcomes: list[Outcome] = field(default_factory=list)
    replayed: list[str] = field(default_factory=list)
    excluded: list[tuple[Path, list[str]]] = field(default_factory=list)  # git exclude files changed

    @property
    def written(self) -> list[Outcome]:
        return [outcome for outcome in self.outcomes if outcome.ok]

    @property
    def failed(self) -> list[Outcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]


def in_worker() -> bool:
    """agt share never syncs inside agt's own workers."""

    try:
        depth = int(os.environ.get("AGT_DEPTH") or 0)
    except ValueError:
        depth = 1
    return depth > 0 or bool(os.environ.get("AGT_JOB_ID"))


@contextlib.contextmanager
def lock(folders: list[Path], timeout: float = 30.0) -> Iterator[None]:
    """One run at a time per set of CLI folders, whatever agt home it comes from."""

    key = hashlib.sha256("\n".join(sorted(os.path.realpath(folder) for folder in folders)).encode()).hexdigest()[:16]
    handle = fsutil.try_lock(Path(tempfile.gettempdir()) / f"agents-router-share-{key}.lock", timeout=timeout)
    if handle is None:
        raise Busy("another agt share run is using these folders; try again in a moment")
    try:
        yield
    finally:
        fsutil.unlock(handle)


# The journal -------------------------------------------------------------------


def journal_path() -> Path:
    return share_dir() / "journal.jsonl"


def fingerprint(path: Path, kind: str) -> str | None:
    """What's at `path`: a skill entry by its link target, an agent file by its bytes."""

    if not os.path.lexists(path):
        return None
    if kind == "skills":
        return f"link:{os.readlink(path)}" if os.path.islink(path) else "dir"
    try:
        return "sha256:" + fsutil.sha256_file(path)
    except OSError:
        return "unreadable"


def _journal(entry: dict) -> None:
    fsutil.append_synced(journal_path(), json.dumps(entry, sort_keys=True, ensure_ascii=False))


def replay(current: State) -> list[str]:
    """Finish the bookkeeping of a run that was cut off. Returns what it found, for the run's report."""

    try:
        lines = journal_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    entries = []
    for line in lines:
        with contextlib.suppress(ValueError):
            entries.append(json.loads(line))
    done = {(entry.get("run"), entry.get("path")) for entry in entries if entry.get("step") == "done"}
    found = []
    for entry in entries:
        if entry.get("step") != "write" or (entry.get("run"), entry.get("path")) in done:
            continue
        path = Path(entry["path"])
        now = fingerprint(path, entry["kind"])
        if now == entry.get("new"):
            if entry.get("action") in ("copy", "link"):
                record = current.items.setdefault(entry["key"], {})
                record["made"] = sorted(set(record.get("made") or []) | set(entry["labels"]))
            found.append(f"finished an interrupted write: {path}")
        elif now != entry.get("old"):
            found.append(f"{path} changed after an interrupted write; agt treats it as your edit")
    save_state(current)
    journal_path().unlink(missing_ok=True)
    return found


# Writing one change ----------------------------------------------------------------


class _Skip(Exception):
    """This change can't be made safely now; the next run looks again."""


def _backup_folder(run: str, label: str) -> Path:
    folder = share_dir() / "backups" / run / label
    fsutil.make_private_dir(folder)
    return folder


def _write_agent(change: planner.Change, item: planner.Item, decision: engine.Decision, target: str,
                 holders: dict[str, planner.Physical], servers: set[str], run: str) -> Outcome:
    source = holders[decision.source] if decision.source in holders else next(
        copy for copy in item.copies if copy.version == decision.version)
    original: model.Agent = source.obj
    try:
        fresh = model.read_agent(original.cli, original.path)
    except model.ItemError as exc:
        raise _Skip(f"the source can't be read now ({exc})") from None
    if fresh.digest != decision.version:
        raise _Skip(f"{fresh.path} changed while agt was working")
    backup = None
    if change.action == "copy":
        if os.path.lexists(change.path):
            raise _Skip(f"{change.path} appeared while agt was working")
        if target == fresh.cli:
            data = fresh.path.read_bytes()  # the same CLI: keep every field, model included
        else:
            tools = toolmap.for_target(fresh.cli, target, fresh.tools, servers)
            data = model.render_agent(target, fresh.core, tools).encode("utf-8")
        old = None
    else:
        copy = holders[change.labels[0]]
        try:
            current = model.read_agent(target, change.path)
        except model.ItemError as exc:
            raise _Skip(f"{change.path} can't be read now ({exc})") from None
        if current.digest != copy.version:
            raise _Skip(f"{change.path} changed while agt was working")
        text = change.path.read_text(encoding="utf-8")
        data = model.update_agent(target, text, current, fresh.core).encode("utf-8")
        old = fingerprint(change.path, "agents")
        backup = _backup_folder(run, change.labels[0]) / change.path.name
        shutil.copy2(os.path.realpath(change.path), backup)
    _journal({"run": run, "step": "write", "key": item.key, "kind": "agents", "labels": change.labels,
              "action": change.action, "path": str(change.path), "old": old,
              "new": "sha256:" + hashlib.sha256(data).hexdigest(), "backup": str(backup) if backup else None})
    change.path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fsutil.write_preserving(change.path, data, new=change.action == "copy")
    except FileExistsError:
        raise _Skip(f"{change.path} appeared while agt was working") from None
    try:
        written = model.read_agent(target, change.path).digest
    except model.ItemError:
        written = None
    if written != decision.version:
        if backup:
            fsutil.write_preserving(change.path, backup.read_bytes())
        else:
            change.path.unlink()
        return Outcome(item.kind, item.name, change.labels, change.action, change.path, False,
                       "the written copy didn't read back the same, so agt put back what was there")
    return Outcome(item.kind, item.name, change.labels, change.action, change.path, True, backup=backup)


def _write_skill(change: planner.Change, item: planner.Item, decision: engine.Decision,
                 holders: dict[str, planner.Physical], run: str) -> Outcome:
    source = Path(change.source)
    try:
        if model.read_skill(source).digest != decision.version:
            raise _Skip(f"{source} changed while agt was working")
    except model.ItemError as exc:
        raise _Skip(f"the source can't be read now ({exc})") from None
    backup = None
    old = None
    if change.action == "link":
        if os.path.lexists(change.path):
            raise _Skip(f"{change.path} appeared while agt was working")
    else:
        copy = holders[change.labels[0]]
        try:
            if model.read_skill(change.path).digest != copy.version:
                raise _Skip(f"{change.path} changed while agt was working")
        except model.ItemError as exc:
            raise _Skip(f"{change.path} can't be read now ({exc})") from None
        old = fingerprint(change.path, "skills")
        backup = _backup_folder(run, change.labels[0]) / change.path.name
    _journal({"run": run, "step": "write", "key": item.key, "kind": "skills", "labels": change.labels,
              "action": change.action, "path": str(change.path), "old": old, "new": f"link:{source}",
              "backup": str(backup) if backup else None})
    if backup:
        shutil.move(str(change.path), str(backup))  # a link moves as a link, a folder as a whole
    how = fsutil.make_dir_link(source, change.path)
    if how == "copy":
        shutil.rmtree(change.path, ignore_errors=True)
    ok = how != "copy" and Path(os.path.realpath(change.path)) == Path(os.path.realpath(source))
    if ok:
        try:
            ok = model.read_skill(change.path).digest == decision.version
        except model.ItemError:
            ok = False
    if not ok:
        if os.path.lexists(change.path) and fsutil.is_link(change.path):
            fsutil.remove_link(change.path)
        if backup:
            shutil.move(str(backup), str(change.path))
        message = "this system can't make folder links here" if how == "copy" else \
            "the link didn't lead to the same skill, so agt put back what was there"
        return Outcome(item.kind, item.name, change.labels, change.action, change.path, False, message)
    return Outcome(item.kind, item.name, change.labels, change.action, change.path, True, backup=backup)


# Recording ------------------------------------------------------------------------


def _copies_now(item: planner.Item) -> dict[str, str]:
    return {label: copy.version for copy in item.copies for label in copy.labels if copy.version}


def _record(current: State, item: planner.Item, decision: engine.Decision, outcomes: list[Outcome]) -> bool:
    """Update the item's record after its run. Returns whether anything changed."""

    key = item.key
    record = dict(current.items.get(key) or {})
    before = json.dumps(record, sort_keys=True)
    if decision.action == engine.WRITE:
        written = {label for outcome in outcomes if outcome.ok for label in outcome.labels}
        record["made"] = sorted(set(record.get("made") or []) | (set(decision.fills) & written))
        if all(outcome.ok for outcome in outcomes) and outcomes:
            copies = _copies_now(item)
            copies.update({label: decision.version for label in written})
            record.update(version=decision.version, copies=copies)
    elif decision.action == engine.NOTHING and decision.version:
        record.update(version=decision.version, copies=_copies_now(item))
    elif decision.action == engine.STOP:
        stopped = set(decision.stopped)
        record["stopped"] = sorted(set(record.get("stopped") or []) | stopped)
        record["made"] = sorted(set(record.get("made") or []) - stopped)
        record["copies"] = {label: version for label, version in (record.get("copies") or {}).items() if label not in stopped}
    else:
        return False
    record = {name: value for name, value in record.items() if value not in ([], {}, None)}
    if json.dumps(record, sort_keys=True) == before:
        return False
    current.items[key] = record
    return True


# The run ----------------------------------------------------------------------------


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def apply(plan: planner.Plan, current: State, *, home: Path | None = None) -> Result:
    """Make the plan's changes and record them. Call it holding `lock()`, after `replay()`."""

    home = home or Path.home()
    result = Result(run=new_run_id())
    by_label = {provider.label: provider for provider in plan.included}
    servers: dict[str, set[str]] = {}
    grouped: dict[str, list[planner.Change]] = {}
    for change in plan.changes:
        grouped.setdefault(f"{change.kind}/{names.identity(change.name)}", []).append(change)
    made: list[Path] = []
    rotate = True
    for item in plan.items:
        decision = item.decision
        if decision is None:
            continue
        holders = {label: copy for copy in item.copies for label in copy.labels}
        outcomes: list[Outcome] = []
        for change in grouped.get(item.key, []) if decision.action == engine.WRITE else []:
            target = by_label[change.labels[0]].cli
            try:
                if item.kind == "agents":
                    if target == "kiro" and change.labels[0] not in servers:
                        servers[change.labels[0]] = planner.mcp_server_names(by_label[change.labels[0]], home)
                    outcome = _write_agent(change, item, decision, target, holders, servers.get(change.labels[0], set()),
                                           result.run)
                else:
                    outcome = _write_skill(change, item, decision, holders, result.run)
            except _Skip as skip:
                outcome = Outcome(item.kind, item.name, change.labels, change.action, change.path, False,
                                  f"skipped this run: {skip}")
            except OSError as exc:
                outcome = Outcome(item.kind, item.name, change.labels, change.action, change.path, False,
                                  f"couldn't write it ({exc}); the next run tries again")
            if outcome.ok:
                _journal({"run": result.run, "step": "done", "path": str(change.path)})
                if change.action in ("copy", "link"):
                    made.append(change.path)
            outcomes.append(outcome)
        result.outcomes += outcomes
        if _record(current, item, decision, outcomes):
            save_state(current, rotate=rotate)
            rotate = False
    if rotate:
        save_state(current)  # a first run with nothing to write still starts the state
    if made:
        result.excluded = gitexclude.update(made)
    manifest = [{"key": f"{o.kind}/{o.name}", "labels": o.labels, "action": o.action, "path": str(o.path),
                 "backup": str(o.backup) if o.backup else None} for o in result.written]
    if manifest:
        folder = share_dir() / "backups" / result.run
        fsutil.make_private_dir(folder)
        fsutil.write_json(folder / "manifest.json", {"run": result.run, "changes": manifest})
    journal_path().unlink(missing_ok=True)
    return result
