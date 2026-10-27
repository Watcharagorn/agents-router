"""Writing a share plan to the CLI folders, so that a crash or an edit mid-run loses nothing.

For each change:

1. Read the copy again; if it changed since the plan read it, skip the item this run.
2. Back up what's there to `state/share/backups/<run>/`, never inside a CLI folder.
3. fsync a journal entry: the path, what's there now, and what agt will write.
4. Write: a new agent file is linked into place (it never replaces one that
   appeared meanwhile); an update keeps the file's mode and hard links; a
   skill is a link to its real folder. A removal moves the entry into the
   backups (a link moves as a link), so nothing is ever deleted outright.
5. Read it back. Only a copy whose digest is the item's version counts as
   done; anything else is put back from the backup and reported.

Each run that wrote something leaves `backups/<run>/manifest.json`: the
command that ran (`op`), and each change with its backup. `agt share undo`
reads them.

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
from typing import Callable, Iterator

from agents_router import fsutil, synclog
from agents_router.share import engine, frontmatter, gitexclude, model, names, normalize, toolmap
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
    version: str | None = None  # a rename's copy as it reads now


@dataclass
class Result:
    run: str
    outcomes: list[Outcome] = field(default_factory=list)
    replayed: list[str] = field(default_factory=list)
    excluded: list[tuple[Path, list[str]]] = field(default_factory=list)  # git exclude files changed
    unlisted: list[tuple[Path, list[str]]] = field(default_factory=list)  # ... that let go of copies kept as your own files
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (old key, new key) of the renames that went through

    @property
    def written(self) -> list[Outcome]:
        return [outcome for outcome in self.outcomes if outcome.ok]

    @property
    def failed(self) -> list[Outcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]


def _in_repo() -> bool:
    from agents_router.share import scope

    return scope.current().project


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
        if entry.get("action") in RENAMES:
            found += _replay_rename(entry, path)
            continue
        now = fingerprint(path, entry["kind"])
        if now == entry.get("new"):
            record = current.items.setdefault(entry["key"], {})
            if entry.get("action") in ("copy", "link"):
                record["made"] = sorted(set(record.get("made") or []) | set(entry["labels"]))
            elif entry.get("action") == "remove" and entry.get("op") == "rm":
                # The record stays: the copies still there wait to be deleted, and `rm` again finishes it.
                found.append(f"an interrupted `agt share rm` moved {path} to the backups; "
                             f"`agt share rm {entry['key'].partition('/')[2]}` finishes it")
                continue
            elif entry.get("action") == "remove":
                gone = set(entry["labels"])
                record["made"] = sorted(set(record.get("made") or []) - gone)
                record["copies"] = {label: version for label, version in (record.get("copies") or {}).items() if label not in gone}
            found.append(f"finished an interrupted write: {path}")
        elif now != entry.get("old"):
            found.append(f"{path} changed after an interrupted write; agt treats it as your edit")
    save_state(current)
    journal_path().unlink(missing_ok=True)
    return found


def _replay_rename(entry: dict, path: Path) -> list[str]:
    """A rename cut off between making the new entry and moving the old one away: move it now.

    The record still has the old name; the next sync sees the rename and finishes it.
    """

    to = Path(entry["to"])
    if to == path or not os.path.lexists(path) or not os.path.lexists(to) or not entry.get("backup"):
        return []
    if fingerprint(path, entry["kind"]) != entry.get("old") or fingerprint(to, entry["kind"]) != entry.get("new"):
        return [f"{path} changed after an interrupted rename; agt treats it as your edit"]
    Path(entry["backup"]).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), entry["backup"])
    return [f"finished an interrupted rename: {path} is now {to}"]


# Writing one change ----------------------------------------------------------------


class _Skip(Exception):
    """This change can't be made safely now; the next run looks again."""


SKIPPED = "skipped this run: "


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
    how = fsutil.make_dir_link(source, change.path, relative=_in_repo())
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


def _remove(change: planner.Change, item: planner.Item, cli: str, holders: dict[str, planner.Physical],
            run: str, op: str) -> Outcome:
    """Move one entry into this run's backups, after checking it's still what the plan read."""

    path = change.path
    copy = holders[change.labels[0]]
    if not os.path.lexists(path):
        raise _Skip(f"{path} is already gone")
    if copy.version is not None:
        try:
            now = model.read_agent(cli, path).digest if item.kind == "agents" else model.read_skill(path).digest
        except model.ItemError as exc:
            raise _Skip(f"{path} can't be read now ({exc})") from None
        if now != copy.version:
            raise _Skip(f"{path} changed while agt was working")
    backup = _backup_folder(run, change.labels[0]) / path.name
    if os.path.lexists(backup):
        raise _Skip(f"{backup} is already there")
    _journal({"run": run, "op": op, "step": "write", "key": item.key, "kind": item.kind, "labels": change.labels,
              "action": "remove", "path": str(path), "old": fingerprint(path, item.kind), "new": None, "backup": str(backup)})
    shutil.move(str(path), str(backup))  # a link moves as a link, a folder as a whole
    if os.path.lexists(path):
        return Outcome(item.kind, item.name, change.labels, "remove", path, False, "it's still there after the move")
    return Outcome(item.kind, item.name, change.labels, "remove", path, True, backup=backup)


def _put(backup: Path, path: Path) -> None:
    """Copy a backup into place as it was: a link as a link (same text), a folder as a folder, a file with its mode."""

    if os.path.islink(backup):
        os.symlink(os.readlink(backup), path, target_is_directory=backup.is_dir())
    elif backup.is_dir():
        shutil.copytree(backup, path, symlinks=True)
    else:
        fsutil.write_preserving(path, backup.read_bytes(), new=True)
        shutil.copystat(backup, path)


def _same(backup: Path, path: Path) -> bool:
    if os.path.islink(backup) or os.path.islink(path):
        return os.path.islink(path) and os.path.islink(backup) and os.readlink(path) == os.readlink(backup)
    if backup.is_dir():
        return path.is_dir() and fsutil.tree_signature(path) == fsutil.tree_signature(backup)
    return path.is_file() and fsutil.sha256_file(path) == fsutil.sha256_file(backup)


def restore(entry: dict, run: str) -> Outcome:
    """Put one manifest entry's backup back where it came from (for `agt share undo`).

    What's there now goes into this run's backups first, so the undo can be undone. A removed copy
    comes back only where nothing has appeared since. The backup itself is kept.
    """

    kind, name = entry["key"].partition("/")[0], entry.get("name") or entry["key"].partition("/")[2]
    path, backup, labels = Path(entry["path"]), Path(entry["backup"]), list(entry["labels"])
    fail = lambda message: Outcome(kind, name, labels, entry["action"], path, False, message)
    if not os.path.lexists(backup):
        return fail(f"its backup {backup} is gone")
    removed = entry["action"] == "remove"
    if removed and os.path.lexists(path):
        return fail("something is there again, so agt left it as it is")
    if not removed and not os.path.lexists(path):
        return fail("it's gone; `agt share undo <item> --in <label>` brings back a removed copy")
    saved = None
    if not removed:
        saved = _backup_folder(run, labels[0]) / path.name
        if os.path.lexists(saved):
            return fail(f"{saved} is already there")
    _journal({"run": run, "op": "undo", "step": "write", "key": entry["key"], "kind": kind, "labels": labels,
              "action": "restore", "path": str(path), "old": fingerprint(path, kind), "new": fingerprint(backup, kind),
              "backup": str(saved) if saved else None})
    path.parent.mkdir(parents=True, exist_ok=True)
    if saved is None:
        _put(backup, path)
        action = "copy" if kind == "agents" else "link" if os.path.islink(path) else "copy"
    elif kind == "agents" and not os.path.islink(path) and not os.path.islink(backup):
        shutil.copy2(os.path.realpath(path), saved)
        fsutil.write_preserving(path, backup.read_bytes())  # keeps the file's mode and hard links
        action = "update"
    else:
        shutil.move(str(path), str(saved))  # a link moves as a link
        _put(backup, path)
        action = "update" if kind == "agents" else "relink"
    if not _same(backup, path):
        return Outcome(kind, name, labels, action, path, False, "it didn't come back the same; the backup is still there")
    _journal({"run": run, "step": "done", "path": str(path)})
    return Outcome(kind, name, labels, action, path, True, "put back", backup=saved)


RENAMES = ("rename", "rename-folder", "rename-link")


def _rename(change: planner.Change, item: planner.Item, cli: str, run: str, op: str) -> tuple[Outcome, Callable[[], None]]:
    """Give one entry the item's new name. Returns the outcome and the step that undoes it."""

    path, to, label = change.path, change.to, change.labels[0]
    backup = _backup_folder(run, label) / path.name
    if os.path.lexists(backup):
        raise _Skip(f"{backup} is already there")
    if to != path and os.path.lexists(to):
        raise _Skip(f"{to} is already there")
    entry = {"run": run, "op": op, "step": "write", "key": item.key, "kind": item.kind, "labels": change.labels,
             "action": change.action, "path": str(path), "to": str(to), "old": fingerprint(path, item.kind),
             "backup": str(backup)}
    if change.action == "rename":
        try:
            current = model.read_agent(cli, path)
        except model.ItemError as exc:
            raise _Skip(f"{path} can't be read now ({exc})") from None
        if change.version and current.digest != change.version:
            raise _Skip(f"{path} changed while agt was working")
        core = normalize.agent_core(item.name, current.description, current.body)
        data = model.update_agent(cli, path.read_text(encoding="utf-8"), current, core).encode("utf-8")
        _journal({**entry, "new": "sha256:" + hashlib.sha256(data).hexdigest()})
        shutil.copy2(os.path.realpath(path), backup)
        if to == path:  # a file you renamed already: only its name field changes
            fsutil.write_preserving(path, data)
            undo = lambda: fsutil.write_preserving(path, backup.read_bytes())
        else:
            fsutil.write_preserving(to, data, new=True)
            shutil.copymode(backup, to)
            os.unlink(path) if not os.path.islink(path) else fsutil.remove_link(path)
            undo = lambda: (fsutil.write_preserving(path, backup.read_bytes(), new=True), shutil.copymode(backup, path),
                            os.unlink(to))
        try:
            version = model.read_agent(cli, to).digest
        except model.ItemError:
            version = None
        ok = version == normalize.digest(core)
    elif change.action == "rename-folder":
        manifest = path / "SKILL.md"
        try:
            if change.version and model.read_skill(path).digest != change.version:
                raise _Skip(f"{path} changed while agt was working")
            text = manifest.read_text(encoding="utf-8")
        except (model.ItemError, OSError) as exc:
            raise _Skip(f"{path} can't be read now ({exc})") from None
        new_text = frontmatter.update(text, {"name": item.name})
        _journal({**entry, "new": "dir"})
        fsutil.make_private_dir(backup)
        shutil.copy2(manifest, backup / "SKILL.md")  # the folder itself is renamed, not copied
        if to != path:
            os.rename(path, to)
        fsutil.write_preserving(to / "SKILL.md", new_text.encode("utf-8"))
        undo = lambda: (fsutil.write_preserving(to / "SKILL.md", text.encode("utf-8")), to != path and os.rename(to, path))
        try:
            skill = model.read_skill(to)
            version, ok = skill.digest, skill.declared_name == item.name
        except model.ItemError:
            version, ok = None, False
    else:  # "rename-link": a link agt made, made again under the new name
        if not os.path.islink(path):
            raise _Skip(f"{path} isn't a link any more")
        if not change.real.is_dir():
            raise _Skip(f"{change.real} isn't there")
        _journal({**entry, "new": f"link:{change.real}"})
        if fsutil.make_dir_link(change.real, to, relative=_in_repo()) == "copy":
            shutil.rmtree(to, ignore_errors=True)
            return Outcome(item.kind, item.name, change.labels, change.action, to, False,
                           "this system can't make folder links here"), lambda: None
        shutil.move(str(path), str(backup))  # a link moves as a link
        undo = lambda: (fsutil.remove_link(to), shutil.move(str(backup), str(path)))
        try:
            skill = model.read_skill(to)
            version = skill.digest
            ok = Path(os.path.realpath(to)) == Path(os.path.realpath(change.real))
        except model.ItemError:
            version, ok = None, False
    if not ok:
        undo()
        return Outcome(item.kind, item.name, change.labels, change.action, to, False,
                       "the renamed copy didn't read back as expected, so agt put it back"), lambda: None
    return Outcome(item.kind, item.name, change.labels, change.action, to, True, backup=backup, version=version), undo


def _apply_rename(item: planner.Item, changes: list[planner.Change], by_label: dict, current: State,
                  result: Result, op: str) -> list[Outcome]:
    """All of an item's renames, or none: when one fails, the ones done are undone, newest first."""

    done: list[tuple[Outcome, Callable[[], None]]] = []
    failed: Outcome | None = None
    for change in changes:
        try:
            outcome, undo = _rename(change, item, by_label[change.labels[0]].cli, result.run, op)
        except _Skip as skip:
            outcome, undo = Outcome(item.kind, item.name, change.labels, change.action, change.path, False,
                                    f"{SKIPPED}{skip}"), lambda: None
        except OSError as exc:
            outcome, undo = Outcome(item.kind, item.name, change.labels, change.action, change.path, False,
                                    f"couldn't rename it ({exc})"), lambda: None
        if not outcome.ok:
            failed = outcome
            break
        _journal({"run": result.run, "step": "done", "path": str(change.path)})
        done.append((outcome, undo))
    if failed:
        for outcome, undo in reversed(done):
            with contextlib.suppress(OSError):
                undo()
        others = [Outcome(o.kind, o.name, o.labels, o.action, o.path, False, "put back: the rename is all or nothing")
                  for o, _ in done]
        return [failed, *others]
    outcomes = [outcome for outcome, _ in done]
    # The record moves to the new name, with each copy as it reads now; picks and stops come along.
    old = current.items.pop(item.renamed_from, None) or {}
    copies = {label: copy.version for copy in item.copies for label in copy.labels if copy.version}
    copies.update({label: outcome.version for outcome in outcomes for label in outcome.labels if outcome.version})
    versions = sorted(set(copies.values()), key=list(copies.values()).count, reverse=True)
    record = {name: old[name] for name in ("made", "stopped", "released") if old.get(name)}
    if versions:
        record.update(version=versions[0], copies=copies)
        shape = next((copy.shape for copy in item.copies if copy.shape), None) or old.get("shape")
        if shape:
            record["shape"] = shape
    current.items[item.key] = record
    result.renamed.append((item.renamed_from, item.key))
    made = set(record.get("made") or [])
    olds = [change.path for change in changes if change.to != change.path]
    news = [change.to for change in changes if change.to != change.path and set(change.labels) & made]
    if olds:
        result.excluded += gitexclude.update(olds, remove=True)
    if news:
        result.excluded += gitexclude.update(news)
    return outcomes


# Recording ------------------------------------------------------------------------


def _copies_now(item: planner.Item, skip: set[str]) -> dict[str, str]:
    return {label: copy.version for copy in item.copies for label in copy.labels if copy.version and label not in skip}


def _record(current: State, item: planner.Item, decision: engine.Decision, outcomes: list[Outcome]) -> bool:
    """Update the item's record after its run. Returns whether anything changed."""

    key = item.key
    record = dict(current.items.get(key) or {})
    before = json.dumps(record, sort_keys=True)
    # Copies that left the item this run: removed into the backups, or kept as your own files.
    released = {label for label, how in item.drops.items() if how == "release"}
    gone = released | {label for outcome in outcomes if outcome.ok and outcome.action == "remove" for label in outcome.labels}
    # Only copies the engine weighed are recorded at their version now; a frozen folder keeps its old entry.
    skip = gone | set(record.get("released") or []) | item.ignored
    kept = {label: version for label, version in (record.get("copies") or {}).items() if label in item.ignored - gone}
    writes = [outcome for outcome in outcomes if outcome.action != "remove"]
    if decision.action == engine.WRITE:
        written = {label for outcome in writes if outcome.ok for label in outcome.labels}
        record["made"] = sorted(set(record.get("made") or []) | (set(decision.fills) & written))
        if writes and all(outcome.ok for outcome in writes):
            copies = {**kept, **_copies_now(item, skip)}
            copies.update({label: decision.version for label in written})
            record.update(version=decision.version, copies=copies)
    elif decision.action == engine.NOTHING and decision.version:
        record.update(version=decision.version, copies={**kept, **_copies_now(item, skip)})
    elif decision.action == engine.STOP:
        stopped = set(decision.stopped)
        record["stopped"] = sorted(set(record.get("stopped") or []) | stopped)
        record["made"] = sorted(set(record.get("made") or []) - stopped)
        record["copies"] = {label: version for label, version in (record.get("copies") or {}).items() if label not in stopped}
    if gone:
        record["made"] = sorted(set(record.get("made") or []) - gone)
        record["copies"] = {label: version for label, version in (record.get("copies") or {}).items() if label not in gone}
    if released or item.unreleased:
        record["released"] = sorted((set(record.get("released") or []) | released) - item.unreleased)
    # Runs in a row where a write failed (not one skipped because a copy changed meanwhile): 3 make an attention entry.
    failures = [outcome for outcome in outcomes if not outcome.ok and not outcome.message.startswith(SKIPPED)]
    if failures:
        record["failing"] = {"runs": int((record.get("failing") or {}).get("runs", 0)) + 1, "message": failures[0].message}
    elif (outcomes and all(outcome.ok for outcome in outcomes)) or (decision.action == engine.NOTHING and decision.version):
        record.pop("failing", None)  # a run that wrote it, or found nothing to write
    if record.get("version"):
        # The name-free shape of the recorded version, so a rename can be spotted later.
        shape = next((copy.shape for copy in item.copies if copy.version == record["version"] and copy.shape), None)
        if shape:
            record["shape"] = shape
    record = {name: value for name, value in record.items() if value not in ([], {}, None)}
    if json.dumps(record, sort_keys=True) == before:
        return False
    if record:
        current.items[key] = record
    else:
        current.items.pop(key, None)
    return True


# The run ----------------------------------------------------------------------------


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def apply(plan: planner.Plan, current: State, *, home: Path | None = None, op: str = "sync",
          run: str | None = None, prior: list[Outcome] | None = None, records: dict | None = None,
          command: str | None = None, log: dict | None = None, trigger: str = "command") -> Result:
    """Make the plan's changes and record them. Call it holding `lock()`, after `replay()`.

    `op` names the command for the manifest. `prior` holds writes the command already made in this
    run (undo's restores), so the manifest lists them too. `records` keeps state records the command
    drops (`rm`), so undo can bring them back. The run goes to agt's log as `command` (by default
    `agt share <op>`), with `log`'s fields added. `trigger` says what started it: a command, the
    watcher, a session start, a save in Claude Code or an MCP server start.
    """

    started = time.monotonic()
    synclog.notice(None)  # a hand edit of your share settings shows before this run's line
    home = home or Path.home()
    result = Result(run=run or new_run_id(), outcomes=list(prior or []))
    by_label = {provider.label: provider for provider in plan.included}
    servers: dict[str, set[str]] = {}
    grouped: dict[str, list[planner.Change]] = {}
    for change in plan.changes:
        grouped.setdefault(f"{change.kind}/{names.identity(change.name)}", []).append(change)
    made: list[Path] = []
    removed: list[Path] = []
    released: list[Path] = []
    rotate = True
    for item in plan.items:
        decision = item.decision
        if decision is None:
            continue
        holders = {label: copy for copy in item.copies for label in copy.labels}
        if decision.action == "rename":
            result.outcomes += _apply_rename(item, grouped.get(item.key, []), by_label, current, result, op)
            save_state(current, rotate=rotate)
            rotate = False
            continue
        outcomes: list[Outcome] = []
        for change in grouped.get(item.key, []):
            target = by_label[change.labels[0]].cli
            try:
                if change.action == "remove":
                    outcome = _remove(change, item, target, holders, result.run, op)
                elif item.kind == "agents":
                    if target == "kiro" and change.labels[0] not in servers:
                        servers[change.labels[0]] = planner.mcp_server_names(by_label[change.labels[0]], home)
                    outcome = _write_agent(change, item, decision, target, holders, servers.get(change.labels[0], set()),
                                           result.run)
                else:
                    outcome = _write_skill(change, item, decision, holders, result.run)
            except _Skip as skip:
                outcome = Outcome(item.kind, item.name, change.labels, change.action, change.path, False,
                                  f"{SKIPPED}{skip}")
            except OSError as exc:
                outcome = Outcome(item.kind, item.name, change.labels, change.action, change.path, False,
                                  f"couldn't write it ({exc}); the next run tries again")
            if outcome.ok:
                _journal({"run": result.run, "step": "done", "path": str(change.path)})
                if change.action in ("copy", "link"):
                    made.append(change.path)
                elif change.action == "remove":
                    removed.append(change.path)
            outcomes.append(outcome)
        result.outcomes += outcomes
        # A copy kept as your own file isn't agt's any more: its line leaves the repo's exclude file.
        released.extend(holders[label].entries.get(label, holders[label].path)
                        for label, how in item.drops.items() if how == "release" and label in holders)
        if _record(current, item, decision, outcomes):
            save_state(current, rotate=rotate)
            rotate = False
    if plan.instructions is not None:
        result.outcomes += _apply_instructions(plan, current, result.run)
        save_state(current, rotate=rotate)
        rotate = False
    if plan.mcp is not None:
        result.outcomes += _apply_mcp(plan, current, result.run)
        save_state(current, rotate=rotate)
        rotate = False
    if plan.hooks is not None:
        result.outcomes += _apply_hooks(plan)
    if plan.plugins is not None:
        result.outcomes += _apply_plugins(plan, current)
        save_state(current, rotate=rotate)
        rotate = False
    if plan.claude_import is not None:
        result.outcomes += _apply_import(plan.claude_import, current, result.run, made, removed, released)
        save_state(current, rotate=rotate)
        rotate = False
    if rotate:
        save_state(current)  # a first run with nothing to write still starts the state
    if made:
        result.excluded = gitexclude.update(made)
    if removed:
        gitexclude.update(removed, remove=True)  # the copies are in the backups now
    if released:
        result.unlisted = gitexclude.update(released, remove=True)
    write_manifest(result, op, records)
    journal_path().unlink(missing_ok=True)
    _log(plan, result, command or f"agt share {op}", started, log, trigger)
    return result


def _apply_instructions(plan: planner.Plan, current: State, run: str) -> list[Outcome]:
    """The instruction files' writes, as outcomes of this run (they share agt's backups and log)."""

    from agents_router.share import instructions

    return [Outcome("instructions", "instructions", [written.label], "update", written.path, written.ok,
                    "" if written.ok else written.message, written.backup, plan.instructions.decision.version)
            for written in instructions.apply(plan.instructions, current, run)]


def _apply_mcp(plan: planner.Plan, current: State, run: str) -> list[Outcome]:
    """The MCP servers' writes, as outcomes of this run (they share agt's backups folder and log)."""

    from agents_router.share import mcp

    return [Outcome(mcp.KIND, written.name, [written.label], written.action, written.path, written.ok,
                    written.message, written.backup)
            for written in mcp.apply(plan.mcp, current, run)]


def _apply_import(change, current: State, run: str, made: list[Path], removed: list[Path],
                  released: list[Path]) -> list[Outcome]:
    """A listed repo's `CLAUDE.md` holding `@AGENTS.md`: made (never over a file that appeared), or taken away
    into the backups while it still holds only that line, or left to you. Its line in `info/exclude` follows."""

    from agents_router.share import instructions

    path, key = change.path, instructions.IMPORT_KEY
    outcome = lambda action, ok, message="", backup=None: Outcome("instructions", "CLAUDE.md", ["claude"], action, path,
                                                                  ok, message, backup)
    if change.action in ("release", "forget"):
        current.items.pop(key, None)
        released.append(path)
        return []
    if change.action == "make":
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return [outcome("copy", False, f"{SKIPPED}a CLAUDE.md appeared meanwhile")]
        except OSError as exc:
            return [outcome("copy", False, f"couldn't write it ({exc}); the next run tries again")]
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(instructions.IMPORT_TEXT)
            handle.flush()
            os.fsync(handle.fileno())
        if not instructions.is_import(path):
            return [outcome("copy", False, "it doesn't read back as written; the next run looks again")]
        current.items[key] = {"path": str(path)}
        made.append(path)
        return [outcome("copy", True, change.note)]
    if not instructions.is_import(path):
        return [outcome("remove", False, f"{SKIPPED}CLAUDE.md changed while agt was working")]
    backup = _backup_folder(run, "claude") / "CLAUDE.md"
    if os.path.lexists(backup):
        return [outcome("remove", False, f"{SKIPPED}{backup} is already there")]
    try:
        shutil.move(str(path), str(backup))
    except OSError as exc:
        return [outcome("remove", False, f"couldn't move it ({exc}); the next run tries again")]
    current.items.pop(key, None)
    removed.append(path)
    return [outcome("remove", True, change.note, backup)]


def _apply_hooks(plan: planner.Plan) -> list[Outcome]:
    """The hook copies each folder's hooks file now holds, as outcomes (the file is backed up in state/share/backups/hooks)."""

    from agents_router.share import hooks

    return [Outcome(hooks.KIND, written.name, [written.label], "update", written.path, written.ok, written.message)
            for written in hooks.apply(plan.hooks)]


def _apply_plugins(plan: planner.Plan, current: State) -> list[Outcome]:
    """The plugins installed and uninstalled with each CLI's own commands, as outcomes (nothing to back up)."""

    from agents_router.share import plugins

    return [Outcome(plugins.KIND, written.name, [written.label], written.action, written.path, written.ok, written.message)
            for written in plugins.apply(plan.plugins, current)]


def _log(plan: planner.Plan, result: Result, command: str, started: float, extra: dict | None,
         trigger: str = "command") -> None:
    if trigger != "command" and not result.outcomes and not result.replayed:
        return  # a background run that changed nothing isn't a line (the watcher runs every minute)
    changes = [{"item": f"{o.kind}/{o.name}", "labels": o.labels, "action": o.action, "path": str(o.path), "ok": o.ok,
                "backup": str(o.backup) if o.backup else None, **({"message": o.message} if not o.ok else {})}
               for o in result.outcomes]
    kept = [{"item": item.key, "label": label} for item in plan.items for label, how in item.drops.items() if how == "release"]
    conflicts = sorted(item.key for item in plan.items if item.decision and item.decision.action == engine.CONFLICT)
    names = {o.name for o in result.outcomes} | {entry["item"].partition("/")[2] for entry in kept} | \
        {key.partition("/")[2] for key in conflicts}
    synclog.append({"type": "sync", **synclog.where(None), "command": command, "trigger": trigger, "run": result.run,
                    "items": sorted(names), "changes": changes, "kept": kept, "conflicts": conflicts,
                    "seconds": round(time.monotonic() - started, 3), **(extra or {})})


def write_manifest(result: Result, op: str, records: dict | None = None) -> None:
    # MCP servers, hooks and plugins are left out: `undo` puts back whole files, a Claude server's backup is only its
    # entry, hook copies follow your approvals, and plugins come and go with each CLI's own commands.
    manifest = [{"key": f"{o.kind}/{names.identity(o.name)}", "name": o.name, "labels": o.labels, "action": o.action,
                 "path": str(o.path), "backup": str(o.backup) if o.backup else None}
                for o in result.written if o.kind not in ("mcp", "hooks", "plugins")]
    if manifest:
        folder = share_dir() / "backups" / result.run
        fsutil.make_private_dir(folder)
        data = {"run": result.run, "op": op, "time": time.time_ns(), "changes": manifest}
        if records:
            data["records"] = records
        if result.renamed:
            data["renames"] = [{"from": old, "to": new} for old, new in result.renamed]
        fsutil.write_json(folder / "manifest.json", data)


def manifests() -> list[dict]:
    """Every run's manifest, newest first."""

    found = []
    for path in (share_dir() / "backups").glob("*/manifest.json"):
        data = fsutil.read_json(path, {})
        if isinstance(data, dict) and isinstance(data.get("changes"), list):
            found.append(data)
    return sorted(found, key=lambda data: (data.get("time") or 0, str(data.get("run"))), reverse=True)
