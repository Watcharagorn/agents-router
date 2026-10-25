"""The 3-way decision for one item: what to write, given its copies now and at the last sync.

`decide()` is pure: it reads no file and writes none. A copy's version is the
digest of its normalized core fields, so different file formats never look
like edits. Each copy keeps its own last-synced version.

| Situation | Decision |
|---|---|
| A copy can't be parsed, or one was edited less than 3 s ago | wait: nothing is written for the item |
| First sight, all copies agree | fill the targets that don't have it |
| First sight, copies differ | conflict |
| No copy changed since the last sync | fill missing targets |
| The changed copies agree | write that version to every other copy and fill missing targets |
| The changed copies disagree | conflict |
| A copy was deleted and another edited | conflict that keeps the edit |
| An agt-made copy was deleted | stop sharing the item to that CLI |
| A copy you made (an original) was deleted | pending delete: it waits for `agt share rm` |
| A provider or kind folder is missing | that CLI is absent: nothing is filled or deleted there |
"""

from __future__ import annotations

from dataclasses import dataclass, field

NOTHING = "nothing"
WRITE = "write"
CONFLICT = "conflict"
WAIT = "wait"
PENDING_DELETE = "pending-delete"
STOP = "stop-sharing"


@dataclass(frozen=True)
class Copy:
    """One copy of the item as it is now."""

    label: str
    version: str | None  # None when the copy can't be parsed
    fresh: bool = False  # modified less than 3 s ago
    problem: str | None = None  # why it can't be parsed, with the file and line


@dataclass
class Last:
    """The item at the last sync (from the state)."""

    version: str
    copies: dict[str, str] = field(default_factory=dict)  # label -> that copy's version then
    made: set[str] = field(default_factory=set)  # labels where agt made the copy
    stopped: set[str] = field(default_factory=set)  # labels the item is no longer shared to
    released: set[str] = field(default_factory=set)  # copies left behind as your own files


@dataclass
class Decision:
    action: str
    version: str | None = None  # what every copy holds after this run
    source: str | None = None  # a label to read that version from
    updates: list[str] = field(default_factory=list)  # copies to rewrite
    fills: list[str] = field(default_factory=list)  # new copies
    stopped: list[str] = field(default_factory=list)  # labels the item stops being shared to
    labels: list[str] = field(default_factory=list)  # the copies involved, for conflicts and waits
    reason: str = ""

    @property
    def writes(self) -> list[str]:
        return self.updates + self.fills


def decide(copies: list[Copy], targets: set[str], last: Last | None = None, absent: set[str] | None = None) -> Decision:
    absent = absent or set()
    released = last.released if last else set()
    present = {copy.label: copy for copy in copies if copy.label not in released and copy.label not in absent}
    problems = [copy for copy in present.values() if copy.version is None]
    if problems:
        return Decision(WAIT, labels=sorted(copy.label for copy in problems),
                        reason="; ".join(copy.problem or f"{copy.label}: can't be read" for copy in problems))
    # A just-saved copy waits, unless it's exactly what agt wrote there last (agt's own write, not an edit).
    synced = last.copies if last else {}
    fresh = sorted(label for label, copy in present.items() if copy.fresh and synced.get(label) != copy.version)
    if fresh:
        return Decision(WAIT, labels=fresh, reason="saved less than 3 s ago")
    stopped = last.stopped if last else set()
    missing = sorted(targets - set(present) - absent - stopped - released)

    if last is None:
        versions = {copy.version for copy in present.values()}
        if len(versions) > 1:
            return Decision(CONFLICT, labels=sorted(present), reason="the copies differ the first time agt sees them")
        if not versions:
            return Decision(NOTHING)
        version = versions.pop()
        return _fill(version, present, missing)

    known = {label: version for label, version in last.copies.items() if label not in absent and label not in released}
    edited = {label: present[label].version for label, version in known.items() if label in present and present[label].version != version}
    new = {label: copy.version for label, copy in present.items() if label not in known}
    deleted = sorted(label for label in known if label not in present)
    changed = {**edited, **{label: version for label, version in new.items() if version != last.version}}
    versions = set(changed.values())

    if deleted and changed:
        return Decision(CONFLICT, labels=sorted({*deleted, *changed}), reason="deleted in one place and edited in another")
    if len(versions) > 1:
        return Decision(CONFLICT, labels=sorted(changed), reason="the copies were edited differently")
    if deleted:
        made = [label for label in deleted if label in last.made]
        originals = [label for label in deleted if label not in last.made]
        if originals:
            return Decision(PENDING_DELETE, labels=originals, stopped=made,
                            reason="an original was deleted; `agt share rm` removes the other copies")
        return Decision(STOP, version=last.version, stopped=made, reason="an agt-made copy was deleted")
    if versions:
        version = versions.pop()
        source = next(label for label, value in changed.items() if value == version)
        updates = sorted(label for label, copy in present.items() if copy.version != version)
        return Decision(WRITE, version=version, source=source, updates=updates, fills=missing,
                        reason=f"edited in {', '.join(sorted(changed))}")
    return _fill(last.version, present, missing)


def frozen(known: dict[str, int], gone: dict[str, int]) -> set[str]:
    """Folders whose deletes freeze this run: more than max(3, 25%) of their known items disappeared at once."""

    return {label for label, count in gone.items() if count > max(3, known.get(label, 0) / 4)}


def _fill(version: str, present: dict[str, Copy], missing: list[str]) -> Decision:
    source = next((label for label, copy in sorted(present.items()) if copy.version == version), None)
    if not missing or source is None:
        return Decision(NOTHING, version=version, source=source)
    return Decision(WRITE, version=version, source=source, fills=missing, reason="missing from " + ", ".join(missing))
