"""What a share run would do, worked out only by reading: the item × folder table.

For each kind, every location of every included folder is listed once by real
path, so `~/.agents/skills` and `~/.claude-lab/skills` are one place. Each item
(an agent file or a skill folder, by normalized name) gets one cell per folder:
it has it, it would get it, you unticked it, or it can't go there and why. The
engine decides each item from its copies, and the changes are the writes those
decisions need, one per real target path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time
import tomllib
from typing import Any

from agents_router.share import engine, exclude, model, names, providers, toolmap
from agents_router.share.providers import Provider, _short as short
from agents_router.share.state import State

KINDS = ("agents", "skills")
FRESH_SECONDS = 3
# Codex's own agent roles: a custom agent with one of these names would replace it. Codex 0.164.0 ships
# explorer.toml and awaiter.toml and a "Default agent"; worker is in its multi-agent docs.
CODEX_BUILTIN_AGENTS = {"default", "explorer", "worker", "awaiter"}
HAS, COPY, UPDATE, SKIPPED, BLOCKED, CONFLICTED, WAITING = "has", "copy", "update", "skipped", "blocked", "conflict", "wait"
REMOVE, RELEASED = "remove", "released"
NOT_SHARED = "you chose not to share it; every copy stays as it is"
NEW_WAITING = "new, waiting for you to pick its targets"


@dataclass
class Options:
    kinds: tuple[str, ...] = KINDS
    tool_widening: str = "allow-if-shell"
    skip: set[tuple[str, str]] = field(default_factory=set)  # (item name, label) cells you unticked
    only: dict[str, set[str]] = field(default_factory=dict)  # item name -> the only labels it goes to
    resolve: dict[str, str] = field(default_factory=dict)  # item name -> label whose version wins
    off: set[str] = field(default_factory=set)  # items that aren't shared, by name or <kind>/<name>
    auto_copy: tuple[str, ...] = KINDS  # kinds whose new items go to every target
    instructions_from: str | None = None  # `--resolve instructions=<label>`: the instruction file that wins
    keep_copies: bool = False  # a copy left out by a pick stays as your own file instead of being removed
    deletes: dict[str, str] = field(default_factory=dict)  # frozen label -> "apply" or "refill", for this run only

    def names(self, item: "Item") -> tuple[str, str]:
        return item.name, f"{item.kind}/{item.name}"


@dataclass
class Physical:
    """One copy on disk; several labels can see it when their locations are the same real folder."""

    path: Path
    real: str
    labels: list[str]
    version: str | None
    problem: str | None = None
    fresh: bool = False
    obj: Any = None
    mtime: float = 0.0
    entries: dict[str, Path] = field(default_factory=dict)  # label -> its own entry (a skill can have a link and a folder)

    @property
    def shape(self) -> str | None:
        return getattr(self.obj, "shape", None) or None


@dataclass
class Cell:
    state: str
    note: str = ""


@dataclass
class Item:
    kind: str
    name: str
    copies: list[Physical] = field(default_factory=list)
    cells: dict[str, Cell] = field(default_factory=dict)
    decision: engine.Decision | None = None
    notes: list[str] = field(default_factory=list)
    drops: dict[str, str] = field(default_factory=dict)  # label -> "remove" or "release": copies in folders you left out
    ignored: set[str] = field(default_factory=set)  # labels holding a copy the engine didn't weigh (frozen, left out)
    renamed_from: str | None = None  # the `<kind>/<identity>` key this item had before a rename
    unreleased: set[str] = field(default_factory=set)  # kept copies that are gone now: those CLIs get the item again

    @property
    def key(self) -> str:
        return f"{self.kind}/{names.identity(self.name)}"


@dataclass
class Change:
    kind: str
    name: str
    labels: list[str]
    action: str  # "copy", "update", "link", "relink", "remove", or a rename: "rename", "rename-folder", "rename-link"
    path: Path
    source: Path
    version: str
    new_folder: Path | None = None
    to: Path | None = None  # a rename's new path (the same path when only the name field changes)
    real: Path | None = None  # "rename-link": the skill folder the new link leads to


@dataclass
class Excluded:
    kind: str
    name: str
    labels: list[str]
    reason: str
    item: Item | None = None  # its copies, when agt read them (a name field that doesn't match, say)


@dataclass
class Plan:
    included: list[Provider]
    rows: list[providers.Candidate]
    items: list[Item]
    changes: list[Change]
    excluded: list[Excluded]
    options: Options
    notices: list[str] = field(default_factory=list)
    frozen: list[dict[str, Any]] = field(default_factory=list)  # folders whose deletes wait for you: label, kind, gone, known
    unrenamed: list[dict[str, Any]] = field(default_factory=list)  # renames agt left alone: kind, old, new, message
    instructions: Any = None  # the instruction files' plan (instructions.Plan) when they share

    @property
    def conflicts(self) -> list[Item]:
        return [item for item in self.items if item.decision and item.decision.action == engine.CONFLICT]

    @property
    def waiting(self) -> list[Item]:
        return [item for item in self.items if item.decision and item.decision.action == engine.WAIT]

    @property
    def new_folders(self) -> list[Path]:
        found: list[Path] = []
        for change in self.changes:
            if change.new_folder and change.new_folder not in found:
                found.append(change.new_folder)
        return found

    @property
    def folders_changed(self) -> list[str]:
        found: list[str] = []
        for change in self.changes:
            for label in change.labels:
                if label not in found:
                    found.append(label)
        return found

    def restricted(self, keys: set[str]) -> "Plan":
        """The same plan for some items only (`<kind>/<identity>` keys), for commands that name an item."""

        changes = [change for change in self.changes if f"{change.kind}/{names.identity(change.name)}" in keys]
        return Plan(self.included, self.rows, [item for item in self.items if item.key in keys], changes,
                    [entry for entry in self.excluded if f"{entry.kind}/{names.identity(entry.name)}" in keys],
                    self.options, list(self.notices), list(self.frozen),
                    [entry for entry in self.unrenamed if f"{entry['kind']}/{entry['old']}" in keys])

    @property
    def digest(self) -> str:
        """The plan's id for `--if-plan`: it changes whenever any write or conflict would change."""

        content = {
            "changes": sorted([c.kind, c.name, c.action, str(c.path), c.version] for c in self.changes),
            "conflicts": sorted([i.kind, i.name, sorted(p.version or "" for p in i.copies)] for i in self.conflicts),
        }
        if self.instructions is not None and self.instructions.decision is not None:
            decision = self.instructions.decision
            content["instructions"] = [decision.action, decision.version, sorted(c.label for c in self.instructions.writes),
                                       sorted(decision.labels)]
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:12]


# Reading -------------------------------------------------------------------


def _groups(included: list[Provider], kind: str) -> list[tuple[Path, str, list[str]]]:
    """Each real folder of this kind once: (folder to list, its CLI, the labels that see it)."""

    groups: dict[str, tuple[Path, str, list[str]]] = {}
    for provider in included:
        for location in provider.locations.get(kind, []):
            real = location.real
            if real in groups:
                if provider.label not in groups[real][2]:
                    groups[real][2].append(provider.label)
            else:
                groups[real] = (location.path, provider.cli, [provider.label])
    return list(groups.values())


def _mtime(path: Path) -> float:
    try:
        if path.is_dir():
            return max((child.stat().st_mtime for child in path.rglob("*") if child.is_file()), default=path.stat().st_mtime)
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _kiro_data(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_items(included: list[Provider], options: Options, now: float, disabled: set[str],
               own: frozenset[str] = frozenset()) -> tuple[list[Item], list[Excluded]]:
    items: dict[str, Item] = {}
    excluded: list[Excluded] = []
    for kind in options.kinds:
        for folder, cli, labels in _groups(included, kind):
            if not folder.is_dir():
                continue
            entries = model.agent_files(cli, folder) if kind == "agents" else sorted(e for e in folder.iterdir() if e.is_dir())
            for entry in entries:
                name = model.agent_name(cli, entry) if kind == "agents" else entry.name
                if name.startswith("."):
                    continue  # hidden entries (.trash, .system, .DS_Store) aren't items at all
                reason = (exclude.agent(cli, name, entry, _kiro_data(entry) if cli == "kiro" else None) if kind == "agents"
                          else exclude.skill(name, entry, disabled))
                if reason:
                    excluded.append(Excluded(kind, name, labels, reason))
                    continue
                physical = _read(kind, cli, entry, labels, now, own)
                key = f"{kind}/{names.identity(name)}"
                item = items.setdefault(key, Item(kind, name))
                same = next((copy for copy in item.copies if copy.real == physical.real), None)
                if same:
                    same.labels += [label for label in labels if label not in same.labels]
                    same.entries.update({label: entry for label in labels if label not in same.entries})
                else:
                    item.copies.append(physical)
                    if entry_name(item, physical) != item.name:
                        item.notes.append(f"names collide after case-folding: {item.name}, {entry_name(item, physical)}")
    shared = []
    for item in items.values():
        problem = NOT_SHARED if set(options.names(item)) & options.off else _item_problem(item)
        if problem:
            excluded.append(Excluded(item.kind, item.name, sorted({l for c in item.copies for l in c.labels}), problem, item))
        else:
            shared.append(item)
    return sorted(shared, key=lambda item: (KINDS.index(item.kind), item.name)), excluded


def entry_name(item: Item, physical: Physical) -> str:
    return physical.path.parent.name if physical.path.name == model.AGY_NESTED else (
        physical.path.stem if item.kind == "agents" else physical.path.name)


def _read(kind: str, cli: str, entry: Path, labels: list[str], now: float, own: frozenset[str] = frozenset()) -> Physical:
    mtime = _mtime(entry)
    fresh = now - mtime < FRESH_SECONDS and _real(entry) not in own  # agt's own restore is no edit in progress
    entries = {label: entry for label in labels}
    try:
        obj = model.read_agent(cli, entry) if kind == "agents" else model.read_skill(entry)
    except model.ItemError as exc:
        return Physical(entry, _real(entry), list(labels), None, problem=str(exc), fresh=fresh, mtime=mtime, entries=entries)
    real = str(obj.real) if kind == "skills" else _real(entry)
    return Physical(entry, real, list(labels), obj.digest, fresh=fresh, obj=obj, mtime=mtime, entries=entries)


def _real(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _item_problem(item: Item) -> str | None:
    collisions = [note for note in item.notes if note.startswith("names collide")]
    if collisions:
        return collisions[0]
    problem = names.problem(item.name)
    if problem:
        return problem
    for copy in item.copies:
        declared = getattr(copy.obj, "declared_name", None)
        if copy.obj is not None and declared is not None and declared != item.name:
            return f"its name field says `{declared}`, but its file is {copy.path.name}"
    return None


# Deciding ------------------------------------------------------------------


def _blocked(item: Item, provider: Provider, options: Options, source: Physical | None) -> str | None:
    location = provider.writable(item.kind)
    if location is None:
        return f"{provider.cli} has no folder agt can write {item.kind} to"
    problem = names.problem(item.name, provider.cli)
    if problem:
        return problem
    if item.kind == "agents" and provider.cli == "codex":
        if item.name in CODEX_BUILTIN_AGENTS:
            return f"Codex has a built-in agent named {item.name}"
        tools = getattr(source.obj, "tools", None) if source else None
        if source and getattr(source.obj, "cli", "codex") != "codex":
            problem = toolmap.codex_problem(tools, options.tool_widening)
            if problem:
                return problem
    return None


@dataclass
class Absent:
    labels: dict[str, set[str]]  # kind -> labels agt neither fills nor deletes in this run
    notices: list[str]
    frozen: list[dict[str, Any]]
    refill: dict[str, set[str]]  # kind -> frozen labels you asked to fill again (`sync --refill`)


def absent_labels(items: list[Item], included: list[Provider], options: Options, state: State) -> Absent:
    """Per kind, the labels that count as absent this run, and why.

    A folder whose kind folder is missing, after agt has synced it, is absent:
    nothing is filled or deleted there. So is a folder where more than
    max(3, 25%) of the copies agt made there disappeared at once: its deletes freeze,
    run after run, until you answer: `sync --allow-deletes <label>` lets them
    go, and `sync --refill <label>` puts the copies back. Labels that stopped
    sharing are absent too.
    """

    by_label = {provider.label: provider for provider in included}
    present = {(item.key, label) for item in items for copy in item.copies for label in copy.labels}
    absent: dict[str, set[str]] = {kind: set() for kind in options.kinds}
    notices: list[str] = []
    frozen: list[dict[str, Any]] = []
    refill: dict[str, set[str]] = {kind: set() for kind in options.kinds}
    for kind in options.kinds:
        known: dict[str, int] = {}
        gone: dict[str, int] = {}
        for key, record in state.items.items():
            if not key.startswith(kind + "/"):
                continue
            for label in record.get("copies") or {}:
                if label not in by_label:
                    absent[kind].add(label)  # a folder that isn't sharing any more
                    continue
                known[label] = known.get(label, 0) + 1
                # Only copies agt made count: a vanished original writes nothing anyway, and waits for `agt share rm`.
                if (key, label) not in present and label in (record.get("made") or []):
                    gone[label] = gone.get(label, 0) + 1
        for label in known:
            location = by_label[label].writable(kind)
            if location is None or not location.path.is_dir():
                absent[kind].add(label)
                notices.append(f"{label}'s {kind} folder is missing: nothing is filled or deleted there until it's back")
        for label in sorted(engine.frozen(known, gone) - absent[kind]):
            answer = options.deletes.get(label)
            if answer == "apply":
                continue  # the deletes go through this run, like any other
            if answer == "refill":
                refill[kind].add(label)
                continue
            absent[kind].add(label)
            frozen.append({"label": label, "kind": kind, "gone": gone[label], "known": known[label]})
            notices.append(f"{gone[label]} of {known[label]} {kind} disappeared from {label} at once, so agt deletes "
                           f"nothing there until you say: `agt share sync --allow-deletes {label}` lets them go, "
                           f"`--refill {label}` puts them back")
    return Absent(absent, notices, frozen, refill)


def _drop(label: str, copy: Physical, wanted: set[str], last: engine.Last | None, made: set[str],
          options: Options) -> tuple[str | None, Cell]:
    """What happens to a copy in a folder you left out: removed with a backup, or kept as your own file."""

    entry = copy.entries.get(label, copy.path)
    sharing = sorted(other for other, path in copy.entries.items() if other != label and other in wanted and path == entry)
    if sharing:
        return None, Cell(HAS, f"{label} reads {sharing[0]}'s folder, so it keeps seeing it")
    if copy.version is None:
        return None, Cell(WAITING, copy.problem or "can't be read now")
    if copy.fresh and (last is None or last.copies.get(label) != copy.version):  # agt's own write is no edit
        return None, Cell(WAITING, "saved less than 3 s ago")
    if label not in made:
        if options.keep_copies:
            return "release", Cell(RELEASED, "your original stays as your own file (--keep-copies); agt stops updating it")
        return None, Cell(HAS, "your original: agt never removes it, so it stays shared")
    if last is None or last.copies.get(label) != copy.version:
        return "release", Cell(RELEASED, "it has an edit the other copies don't have, so it stays as your own file")
    if options.keep_copies:
        return "release", Cell(RELEASED, "kept as your own file (--keep-copies); agt stops updating it")
    return "remove", Cell(REMOVE, "removed, with a backup")


def decide_items(items: list[Item], included: list[Provider], options: Options, state: State,
                 found: Absent | None = None) -> list[str]:
    by_label = {provider.label: provider for provider in included}
    found = found or absent_labels(items, included, options, state)
    absent = found.labels
    for item in items:
        last = state.last(item.kind, names.identity(item.name))
        holding = {label for copy in item.copies for label in copy.labels}
        back = found.refill.get(item.kind, set()) - holding
        if last is not None and back & set(last.copies):
            # `sync --refill`: the copies that disappeared from a frozen folder are filled again, not deleted.
            last = engine.Last(last.version, {label: v for label, v in last.copies.items() if label not in back},
                               last.made, last.stopped, last.released)
        if last is not None:
            # A copy kept as your own file that you deleted since: that CLI gets the item again (unless you left it out).
            item.unreleased = {label for label in last.released
                               if label in by_label and label not in holding and label not in absent.get(item.kind, set())}
            if item.unreleased:
                last = engine.Last(last.version, last.copies, last.made, last.stopped, last.released - item.unreleased)
        made = set((state.items.get(item.key) or {}).get("made") or [])
        holders = {label: copy for copy in item.copies for label in copy.labels}
        source = next((copy for copy in item.copies if copy.version), None)
        allowed = next((options.only[name] for name in options.names(item) if name in options.only), None)
        unticked = {label for name, label in options.skip if name in options.names(item)}
        unwanted = {label for label in by_label if label in unticked or (allowed is not None and label not in allowed)}
        released = last.released if last else set()
        picked = allowed is not None or bool(unticked)
        held_new = last is None and not state.first_run and item.kind not in options.auto_copy and not picked
        wanted = set(by_label) - unwanted - released
        targets = set()
        for label, provider in by_label.items():
            if label in holders:
                if label in released:
                    item.cells[label] = Cell(RELEASED, "your own file; agt doesn't update it")
                elif label in unwanted:
                    how, item.cells[label] = _drop(label, holders[label], wanted, last, made, options)
                    if how:
                        item.drops[label] = how
                else:
                    item.cells[label] = Cell(HAS)
                continue
            reason = _blocked(item, provider, options, source)
            if reason:
                item.cells[label] = Cell(BLOCKED, reason)
            elif label in unwanted:
                item.cells[label] = Cell(SKIPPED, "you unticked it" if label in unticked else "--only")
            elif held_new:
                item.cells[label] = Cell(WAITING, NEW_WAITING + " (auto copy is off)")
            elif label in absent.get(item.kind, set()):
                item.cells[label] = Cell(WAITING, "its folder is missing or frozen this run")
            else:
                item.cells[label] = Cell(COPY)
                targets.add(label)
        if held_new:
            item.decision = engine.Decision(engine.NOTHING, reason=NEW_WAITING)
            continue
        # Copies in folders you left out aren't part of the item any more, whether they go now or wait.
        left_out = {label for label in holders if label in unwanted and item.cells[label].state != HAS}
        copies = [engine.Copy(label, copy.version, copy.fresh, copy.problem) for label, copy in holders.items()]
        ignore = absent.get(item.kind, set()) | left_out
        item.ignored = ignore  # their state entries stay as they were, so a freeze lasts until you answer it
        decision = engine.decide(copies, targets | (set(holders) - left_out), last, ignore)
        choice = options.resolve.get(item.name) or options.resolve.get(f"{item.kind}/{item.name}")
        if decision.action == engine.CONFLICT and choice:
            decision = _resolved(item, decision, choice, holders, targets, left_out | released)
        item.decision = decision
        if decision.action != engine.WRITE:
            # Nothing is copied this run: a conflict, a wait, a stop or a pending delete holds the item.
            held = CONFLICTED if decision.action == engine.CONFLICT else WAITING
            for label in by_label:
                if item.cells[label].state == COPY:
                    item.cells[label] = Cell(held, decision.reason)
        for label in decision.updates:
            item.cells[label] = Cell(UPDATE)
    return found.notices


def _resolved(item: Item, decision: engine.Decision, choice: str, holders: dict[str, Physical],
              targets: set[str], left_out: set[str] = frozenset()) -> engine.Decision:
    if choice not in holders or choice in left_out or holders[choice].version is None:
        return decision
    version = holders[choice].version
    updates = sorted(label for label, copy in holders.items() if copy.version != version and label not in left_out)
    return engine.Decision(engine.WRITE, version=version, source=choice, updates=updates, fills=sorted(targets),
                           reason=f"resolved: {choice}'s version")


# Changes -------------------------------------------------------------------


def _entry_key(path: Path) -> str:
    """One entry in one real folder, however many labels see it."""

    return os.path.join(_real(path.parent), path.name)


def changes_for(item: Item, included: list[Provider]) -> list[Change]:
    holders = {label: copy for copy in item.copies for label in copy.labels}
    removals: dict[str, Change] = {}
    for label, how in sorted(item.drops.items()):
        if how != "remove":
            continue
        copy = holders[label]
        entry = copy.entries.get(label, copy.path)
        found = removals.get(_entry_key(entry))
        if found:
            found.labels.append(label)
        else:
            removals[_entry_key(entry)] = Change(item.kind, item.name, [label], "remove", entry, entry, copy.version or "")
    decision = item.decision
    if not decision or decision.action != engine.WRITE:
        return list(removals.values())
    by_label = {provider.label: provider for provider in included}
    source = holders.get(decision.source) or next(copy for copy in item.copies if copy.version == decision.version)
    # A skill is linked to its real folder, never to another link.
    source_path = Path(source.real) if item.kind == "skills" else source.path
    planned: dict[str, Change] = {}
    for label in decision.fills:
        provider = by_label[label]
        folder = provider.writable(item.kind).path
        target = model.new_agent_path(provider.cli, folder, item.name) if item.kind == "agents" else folder / item.name
        real_folder = _real(folder)
        if real_folder in planned:
            planned[real_folder].labels.append(label)
            continue
        new_folder = None if folder.is_dir() else folder
        action = "copy" if item.kind == "agents" else "link"
        planned[real_folder] = Change(item.kind, item.name, [label], action, target, source_path, decision.version, new_folder)
    relinks: list[Change] = []
    for label in decision.updates:
        copy = holders[label]
        # Each label's own entry: a skill seen through a link and through its folder has two to relink.
        path = copy.entries.get(label, copy.path) if item.kind == "skills" else copy.path
        key = _entry_key(path) if item.kind == "skills" else copy.real
        if key in planned:
            planned[key].labels.append(label)
            continue
        action = "update" if item.kind == "agents" else "relink"
        planned[key] = Change(item.kind, item.name, [label], action, path, source_path, decision.version)
        relinks.append(planned[key])
    # Links first: a link through a folder that's relinked first would already lead to the new version.
    order = {id(change): (change.action == "relink" and not os.path.islink(change.path)) for change in relinks}
    changes = sorted(planned.values(), key=lambda change: order.get(id(change), False))
    return changes + list(removals.values())


def newest(item: Item) -> Physical:
    """The copy saved last among those the conflict is about (agt's own copies don't count as edits)."""

    involved = set(item.decision.labels) if item.decision else set()
    candidates = [copy for copy in item.copies if involved & set(copy.labels)] or item.copies
    return max(candidates, key=lambda copy: copy.mtime)


def rename_changes(item: Item, new: str, included: list[Provider], made: set[str]) -> tuple[list[Change], list[str]]:
    """Every entry of the item under its new name, keeping each copy's own fields. Returns (changes, why not).

    An agent file is written again with only its name changed. A skill's folder is renamed (its SKILL.md
    name too) and agt's links are made again to it; a link you made would break, so it stops the rename.
    """

    by_label = {provider.label: provider for provider in included}
    changes: dict[str, Change] = {}
    problems: list[str] = []
    for copy in item.copies:
        real = Path(copy.real)
        for label, entry in copy.entries.items():
            key = _entry_key(entry)
            if key in changes:
                changes[key].labels.append(label)
                continue
            if item.kind == "agents":
                if entry.name == model.AGY_NESTED:
                    problems.append(f"{short(entry)} is in a folder of its own; rename it by hand")
                    continue
                to = model.new_agent_path(by_label[label].cli, entry.parent, new)
                changes[key] = Change(item.kind, new, [label], "rename", entry, entry, copy.version or "", to=to)
            elif os.path.islink(entry):
                if label not in made:
                    problems.append(f"{short(entry)} is a link you made to it, and renaming its folder would break it")
                    continue
                changes[key] = Change(item.kind, new, [label], "rename-link", entry, entry, copy.version or "",
                                      to=entry.parent / new, real=real.parent / new)
            else:
                changes[key] = Change(item.kind, new, [label], "rename-folder", entry, entry, copy.version or "",
                                      to=entry.parent / new)
        if item.kind == "skills" and not any(c.action == "rename-folder" and _real(c.path) == copy.real for c in changes.values()):
            problems.append(f"its folder {short(real)} isn't in a CLI folder agt shares; rename it by hand")
    for change in changes.values():
        if change.to != change.path and os.path.lexists(change.to):
            problems.append(f"{short(change.to)} is already there")
    # Folders first, so the new links lead somewhere.
    return sorted(changes.values(), key=lambda change: change.action != "rename-folder"), problems


@dataclass
class Left:
    """One entry that turning off a kind or CLI leaves behind, and what happens to it."""

    item: Item
    labels: list[str]
    path: Path
    status: str  # "agt" (agt made it, unchanged), "edited", "original" or "shared" (a CLI still sharing reads it)
    note: str


def leave(plan: Plan, state: State, kinds: set[str], labels: set[str]) -> list[Left]:
    """What turning off these kinds in these CLIs leaves behind, entry by entry.

    Only copies agt made, unchanged since the last sync, can be removed. Your originals stay, so does a copy
    with an edit the others don't have, and so does an entry a CLI that keeps sharing reads (Codex through
    claude-lab's folder). Copies already kept as your own files aren't listed.
    """

    found: list[Left] = []
    for item in plan.items:
        if item.kind not in kinds:
            continue
        record = state.items.get(item.key) or {}
        made, released, synced = set(record.get("made") or []), set(record.get("released") or []), record.get("copies") or {}
        seen: set[str] = set()
        for copy in item.copies:
            for label, entry in copy.entries.items():
                key = _entry_key(entry)
                if label not in labels or label in released or key in seen:
                    continue
                seen.add(key)
                sharing = sorted(other for other, path in copy.entries.items() if _entry_key(path) == key)
                outside = [other for other in sharing if other not in labels and other not in released]
                inside = [other for other in sharing if other in labels]
                if outside:
                    found.append(Left(item, inside, entry, "shared", f"{label} reads {outside[0]}'s folder, so it stays"))
                elif not set(inside) <= made:
                    found.append(Left(item, inside, entry, "original", "your original stays"))
                elif copy.version is None or any(synced.get(other) != copy.version for other in inside):
                    found.append(Left(item, inside, entry, "edited",
                                      "it has an edit the other copies don't have, so it stays as your own file"))
                else:
                    found.append(Left(item, inside, entry, "agt", "agt made it"))
    return found


def turn_off_plan(plan: Plan, left: list[Left], remove: bool) -> Plan:
    """The writes for a turn-off: agt's unchanged copies removed (or kept), edited ones kept as your own files."""

    items: dict[str, Item] = {}
    for entry in left:
        if entry.status not in ("agt", "edited"):
            continue
        item = items.get(entry.item.key)
        if item is None:
            item = Item(entry.item.kind, entry.item.name, copies=entry.item.copies,
                        decision=engine.Decision("turn-off", reason="turned off"))
            items[item.key] = item
        how = "remove" if remove and entry.status == "agt" else "release"
        for label in entry.labels:
            item.drops[label] = how
            item.cells[label] = Cell(REMOVE if how == "remove" else RELEASED, entry.note)
    chosen = list(items.values())
    return Plan(plan.included, plan.rows, chosen, [change for item in chosen for change in changes_for(item, plan.included)],
                [], plan.options)


def removal_changes(item: Item) -> list[Change]:
    """`agt share rm`: one removal per entry of the item, links before the folders they lead to."""

    found: dict[str, Change] = {}
    for copy in item.copies:
        for label, entry in copy.entries.items():
            key = _entry_key(entry)
            if key in found:
                found[key].labels.append(label)
            else:
                found[key] = Change(item.kind, item.name, [label], "remove", entry, entry, copy.version or "")
    return sorted(found.values(), key=lambda change: not os.path.islink(change.path))


# Renames ---------------------------------------------------------------------

NAME_FIELD = "its name field says"


def _expected_entry(provider: Provider, kind: str, name: str) -> Path | None:
    location = provider.writable(kind)
    if location is None:
        return None
    return model.new_agent_path(provider.cli, location.path, name) if kind == "agents" else location.path / name


def detect_renames(items: list[Item], excluded: list[Excluded], included: list[Provider], state: State,
                   absent: dict[str, set[str]]) -> tuple[list[Item], list[Change], list[dict[str, Any]]]:
    """Items that came back under another name: (rename items, their changes, notes on ones agt left alone).

    A rename is an item agt knew that disappeared from some CLIs, plus a new item there whose content,
    without its name, is the same (its `shape`). It only counts when the match is one to one, every
    copy left under the old name is as agt last synced it, no copy was saved in the last 3 s, and no
    folder involved is missing or frozen. A new file whose name field still says the old name counts
    too (`mv dana.md reviewer.md`): agt sets the field to match.
    """

    if state.first_run:
        return [], [], []
    by_label = {provider.label: provider for provider in included}
    present = {item.key: item for item in items}
    lost: dict[str, tuple[dict, Item | None, set[str]]] = {}
    for key, record in state.items.items():
        kind = key.partition("/")[0]
        if not record.get("version") or not record.get("shape") or kind not in absent:
            continue
        item = present.get(key)
        known = set(record.get("copies") or {})
        held = {label for copy in item.copies for label in copy.labels} if item else set()
        gone = known - held
        if gone and not known & absent[kind]:
            lost[key] = (record, item, gone)
    newcomers: list[tuple[Item, str | None]] = [(item, None) for item in items if state.last(item.kind, names.identity(item.name)) is None]
    for entry in excluded:
        if entry.item is not None and entry.reason.startswith(NAME_FIELD):
            declared = {getattr(copy.obj, "declared_name", None) for copy in entry.item.copies}
            if len(declared) == 1:
                newcomers.append((entry.item, declared.pop()))
    candidates: dict[str, list[tuple[Item, str | None]]] = {}
    for item, declared in newcomers:
        labels = {label for copy in item.copies for label in copy.labels}
        shapes = {copy.shape for copy in item.copies}
        if len(shapes) != 1 or None in shapes or any(copy.fresh or copy.version is None for copy in item.copies) \
                or labels & absent.get(item.kind, set()):
            continue
        shape = shapes.pop()
        for key, (record, _old, gone) in lost.items():
            old_name = key.partition("/")[2]
            if key.partition("/")[0] == item.kind and record["shape"] == shape and labels <= gone \
                    and (declared is None or names.identity(declared) == old_name):
                candidates.setdefault(key, []).append((item, declared))
    claimed = [item.key for found in candidates.values() for item, _ in found]
    renamed: list[Item] = []
    changes: list[Change] = []
    notes: list[dict[str, Any]] = []
    for key, found in candidates.items():
        record, old, gone = lost[key]
        old_name = key.partition("/")[2]
        if len(found) != 1 or claimed.count(found[0][0].key) != 1:
            notes.append({"kind": lost[key][1].kind if lost[key][1] else key.partition("/")[0], "old": old_name, "new": None,
                          "message": f"{old_name} may have been renamed, but more than one item matches it, "
                                     "so agt leaves them as they are"})
            continue
        new, declared = found[0]
        # A copy at its last-synced version is unchanged, even if agt itself wrote it a moment ago.
        stale = sorted(label for copy in (old.copies if old else []) for label in copy.labels
                       if copy.version is None or record["copies"].get(label) != copy.version)
        if stale:
            notes.append({"kind": new.kind, "old": old_name, "new": None,
                          "message": f"{new.name} looks like {old_name} renamed, but {old_name} in {', '.join(stale)} changed "
                                     "since the last sync, so agt treats them as two items"})
            continue
        made = set(record.get("made") or [])
        planned, problems = rename_changes(old, new.name, included, made) if old else ([], [])
        if declared is not None:  # the new file still has the old name inside: set the field to match
            for copy in new.copies:
                for label, entry in copy.entries.items():
                    if new.kind == "agents":
                        planned.append(Change(new.kind, new.name, [label], "rename", entry, entry, copy.version or "", to=entry))
                    elif not os.path.islink(entry):
                        planned.insert(0, Change(new.kind, new.name, [label], "rename-folder", entry, entry, copy.version or "",
                                                 to=entry))
        if new.kind == "skills":
            # agt's links to the old folder lead nowhere now; they're made again under the new name.
            for label in sorted(gone - {l for copy in new.copies for l in copy.labels}):
                link = _expected_entry(by_label[label], "skills", old_name) if label in by_label else None
                if link is None or not os.path.islink(link) or os.path.exists(link):
                    continue
                if label not in made:
                    notes.append({"kind": new.kind, "old": old_name, "new": None,
                                  "message": f"{short(link)} is a link you made, and it leads nowhere now; fix or remove it by hand"})
                    continue
                planned.append(Change(new.kind, new.name, [label], "rename-link", link, link, "", to=link.parent / new.name,
                                      real=Path(new.copies[0].real)))
        if problems:
            notes.append({"kind": new.kind, "old": old_name, "new": None,
                          "message": f"{new.name} looks like {old_name} renamed, but agt can't rename it: {'; '.join(problems)}"})
            continue
        for change in planned:
            change.name = new.name
        item = Item(new.kind, new.name, copies=new.copies, decision=engine.Decision("rename", reason=f"renamed from {old_name}"),
                    renamed_from=key)
        touched = {label for change in planned for label in change.labels}
        for label in by_label:
            held = any(label in copy.labels for copy in new.copies)
            item.cells[label] = Cell(UPDATE, f"renamed from {old_name}") if label in touched else \
                Cell(HAS) if held else Cell(WAITING, "filled right after the rename")
        renamed.append(item)
        changes += planned
    return renamed, changes, notes


# Notes ---------------------------------------------------------------------


def mcp_server_names(provider: Provider, home: Path) -> set[str]:
    """The names of a CLI folder's user-level MCP servers. Only the names are taken; nothing else is kept."""

    found: set[str] = set()
    folder = provider.folder
    if provider.cli == "claude":
        files = [folder / ".claude.json"] + ([home / ".claude.json"] if folder == home / ".claude" else [])
        for path in files:
            data = _json(path)
            if isinstance(data.get("mcpServers"), dict):
                found |= set(data["mcpServers"])
    elif provider.cli == "codex":
        try:
            data = tomllib.loads((folder / "config.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data.get("mcp_servers"), dict):
            found |= set(data["mcp_servers"])
    else:
        path = folder / ("settings/mcp.json" if provider.cli == "kiro" else "config/mcp_config.json")
        data = _json(path)
        if isinstance(data.get("mcpServers"), dict):
            found |= set(data["mcpServers"])
    return found


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def add_notes(items: list[Item], included: list[Provider], home: Path) -> None:
    servers = {provider.label: mcp_server_names(provider, home) for provider in included}
    everywhere = set().union(*servers.values()) if servers else set()
    by_label = {provider.label: provider for provider in included}
    for item in items:
        if item.kind != "agents" or not item.decision or not item.decision.fills:
            continue
        source = next((copy for copy in item.copies if copy.version == item.decision.version), None)
        tools = getattr(source.obj, "tools", None) if source else None
        for server in toolmap.servers_named(tools):
            missing = [label for label in item.decision.fills if server not in servers.get(label, set())]
            if missing:
                where = "no CLI here has one" if server not in everywhere else "Phase 3 brings it along"
                kiro = [label for label in missing if by_label[label].cli == "kiro"]
                left = f"; the Kiro copy leaves out its {server} tools" if kiro else ""
                item.notes.append(f"{item.name} in {', '.join(missing)}: no `{server}` server ({where}){left}")
        if tools is not None and getattr(source.obj, "cli", None) == "claude":
            for label in item.decision.fills:
                cli = by_label[label].cli
                lost = toolmap.to_kiro(tools)[1] if cli == "kiro" else toolmap.to_antigravity(tools)[1] if cli == "antigravity" else []
                lost = [tool for tool in lost if not toolmap.mcp_reference(tool)]
                if lost:
                    item.notes.append(f"{item.name} in {label}: no {cli} tool for {', '.join(lost)}")


# The whole plan --------------------------------------------------------------


def build(config: Any, rows: list[providers.Candidate], included: list[Provider], options: Options, state: State,
          now: float | None = None, home: Path | None = None, own: frozenset[str] = frozenset()) -> Plan:
    """The plan. `own` holds the real paths agt itself just wrote, which don't count as edits in progress."""

    now = time.time() if now is None else now
    home = home or Path.home()
    codex = next((provider.folder for provider in included if provider.cli == "codex"), None)
    disabled = exclude.codex_disabled_skills(codex) if codex else set()
    items, excluded = read_items(included, options, now, disabled, own)
    found = absent_labels(items, included, options, state)
    renamed, changes, unrenamed = detect_renames(items, excluded, included, state, found.labels)
    involved = {item.renamed_from for item in renamed} | {item.key for item in renamed}
    items = [item for item in items if item.key not in involved]
    excluded = [entry for entry in excluded if entry.item is None or entry.item.key not in involved]
    decide_items(items, included, options, state, found)
    changes += [change for item in items for change in changes_for(item, included)]
    add_notes(items, included, home)
    items = sorted(items + renamed, key=lambda item: (KINDS.index(item.kind), item.name))
    return Plan(included=included, rows=rows, items=items, changes=changes, excluded=excluded, options=options,
                notices=found.notices + [entry["message"] for entry in unrenamed], frozen=found.frozen, unrenamed=unrenamed)


# Showing it --------------------------------------------------------------------

MARKS = {HAS: "·", COPY: "+", UPDATE: "~", SKIPPED: "x", BLOCKED: "-", CONFLICTED: "!", WAITING: "…", REMOVE: "rm",
         RELEASED: "k"}
LEGEND = "  + copy  ~ update  · has it  x you unticked it  - can't go there  ! conflict  … waiting"


def _group_excluded(excluded: list[Excluded]) -> list[str]:
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for entry in excluded:
        grouped.setdefault((entry.kind, ", ".join(entry.labels), entry.reason), []).append(entry.name)
    lines = []
    for (kind, labels, reason), found in sorted(grouped.items()):
        crew = sorted(name for name in found if name.startswith("kirocrew-"))
        rest = sorted(name for name in found if not name.startswith("kirocrew-"))
        shown = rest + ([f"kirocrew-* ({len(crew)})"] if len(crew) > 3 else crew)
        lines.append(f"{kind} in {labels}: {', '.join(shown)} ({reason})")
    return lines


def would_save(plan: Plan) -> list[str]:
    """Your picks from the flags, as they'd be saved in the user settings."""

    lines = [f"{name}: not in {label}" for name, label in sorted(plan.options.skip)]
    lines += [f"{name}: only in {', '.join(sorted(labels))}" for name, labels in sorted(plan.options.only.items())]
    lines += [f"{name.partition('/')[2] or name}: not shared; every copy stays as it is" for name in sorted(plan.options.off)]
    return lines


def table(plan: Plan, shown: list[Item]) -> list[str]:
    """The item × folder table for these items, with its legend and footnotes."""

    labels = [provider.label for provider in plan.included]
    lines: list[str] = []
    footnotes: list[str] = []
    used: set[str] = set()
    width = max([len(item.name) for item in shown] + [10]) + 4
    for kind in plan.options.kinds:
        rows = [item for item in shown if item.kind == kind]
        if not rows:
            continue
        lines += ["", f"{kind.capitalize():<{width}}" + "".join(f"{label:<12}" for label in labels)]
        for item in rows:
            cells = []
            for label in labels:
                cell = item.cells.get(label, Cell(BLOCKED))
                mark = MARKS[cell.state]
                used.add(cell.state)
                if cell.state in (BLOCKED, RELEASED) or (cell.state == HAS and cell.note):
                    footnotes.append(f"{item.name} in {label}: {cell.note}")
                    mark += str(len(footnotes))
                cells.append(f"{mark:<12}")
            lines.append(f"  {item.name:<{width - 2}}" + "".join(cells))
    if shown:
        extra = "".join(text for state, text in ((REMOVE, "  rm removed, with a backup"), (RELEASED, "  k kept as your own file"))
                        if state in used)
        lines += ["", LEGEND + extra]
        lines += [f"  {index}. {note}" for index, note in enumerate(footnotes, 1)]
    return lines


def render_text(plan: Plan, command: str, *, header: str = "agt share: dry run, nothing was written.",
                saves: bool = True) -> str:
    """The plan as text. `saves` is false for a run that saves no answers (`sync`), so your saved picks aren't news."""

    lines = [header, "", "Folders:"]
    used = {provider.label for provider in plan.included}
    for row in plan.rows:
        provider = row.provider
        mark = "[x]" if provider.label in used else "[-]" if row.reason else "[ ]"
        detail = row.reason or f"{row.holds.get('agents', 0)} agents, {row.holds.get('skills', 0)} skills"
        lines.append(f"  {mark} {provider.label:<16} {short(provider.folder):<22} {detail}")
    lines += table(plan, [item for item in plan.items if any(cell.state != HAS for cell in item.cells.values())])
    renames = [item for item in plan.items if item.decision and item.decision.action == "rename"]
    if renames:
        lines += ["", "Renames (agt renames every copy; each keeps its own fields, and what it replaces is backed up):"]
        for item in renames:
            where = sorted({label for change in plan.changes if change.name == item.name and change.kind == item.kind
                            for label in change.labels})
            lines.append(f"  {item.renamed_from.partition('/')[2]} -> {item.name} ({item.kind}) in {', '.join(where)}")
    if plan.conflicts:
        lines += ["", "Conflicts (nothing is written for these until you pick a version):"]
        for item in plan.conflicts:
            latest = newest(item)
            versions = ", ".join(f"{'/'.join(copy.labels)} {short(copy.path)}" + (" (newer)" if copy is latest else "")
                                 for copy in item.copies)
            lines.append(f"  {item.name} ({item.kind}): {versions}")
            lines.append(f"    pick one with --resolve {item.name}=<label>, or --resolve {item.name}=skip to leave every copy as it is")
    if plan.waiting:
        lines += ["", "Waiting:"] + [f"  {item.name}: {item.decision.reason}" for item in plan.waiting]
    if plan.excluded:
        lines += ["", "Not shared:"] + [f"  {line}" for line in _group_excluded(plan.excluded)]
    notes = plan.notices + [note for item in plan.items for note in item.notes]
    if notes:
        lines += ["", "Notes:"] + [f"  {note}" for note in notes]
    saving = would_save(plan) if saves else []
    if saving and command:
        lines += ["", "Would save in your user settings:"] + [f"  {line}" for line in saving]
    if plan.new_folders:
        lines += ["", "New folders: " + ", ".join(short(folder) for folder in plan.new_folders)]
    if plan.instructions is not None:
        lines += ["", plan.instructions.summary()]
    count = len(plan.changes)
    lines += ["", f"{count} change{'s' if count != 1 else ''} in {len(plan.folders_changed)} folder"
                  f"{'s' if len(plan.folders_changed) != 1 else ''}."]
    extra = plan.instructions is not None and (plan.instructions.writes or plan.instructions.conflict)
    if (count or plan.conflicts or saving or extra) and command:
        lines.append(f"Plan {plan.digest}. To apply it:")
        lines.append(f"  {command}")
    return "\n".join(lines)


def proposal(plan: Plan, command: str, *, saves: bool = True) -> dict[str, Any]:
    """The same plan for an agent: every cell, the answers it needs, and the command to run."""

    needs = []
    for item in plan.conflicts:
        latest = newest(item)
        choices = [{"value": copy.labels[0], "path": str(copy.path), "newer": copy is latest} for copy in item.copies]
        needs.append({
            "id": f"resolve:{item.name}", "ask": f"{item.name} differs between {', '.join(c['value'] for c in choices)}. Which version should every CLI get?",
            "choices": choices + [{"value": "skip", "path": None, "newer": False, "means": "don't share it; every copy stays as it is"}],
            "flag": f"--resolve {item.name}=<choice>",
        })
    shared = plan.instructions
    if shared is not None and shared.conflict:
        needs.append({"id": "resolve:instructions", "ask": f"The instructions differ between {', '.join(shared.decision.labels)}. "
                      "Which version should every CLI get?",
                      "choices": [{"value": label, "path": next((str(c.path) for c in shared.copies if c.label == label), None),
                                   "newer": False} for label in shared.decision.labels],
                      "flag": "--resolve instructions=<choice>"})
    return {
        "dry_run": True,
        "plan": plan.digest,
        "instructions": None if shared is None else {
            "mode": shared.mode, "summary": shared.summary(), "writes": [copy.label for copy in shared.writes],
            "conflict": shared.conflict, "unadopted": [copy.label for copy in shared.unadopted]},
        "folders": [{"label": row.provider.label, "cli": row.provider.cli, "path": str(row.provider.folder),
                     "used": row.provider.label in {p.label for p in plan.included}, "suggested": row.suggested,
                     "reason": row.reason, "holds": row.holds} for row in plan.rows],
        "items": [{"kind": item.kind, "name": item.name, "decision": item.decision.action if item.decision else None,
                   "reason": item.decision.reason if item.decision else "",
                   "cells": {label: {"state": cell.state, "note": cell.note} for label, cell in item.cells.items()},
                   "notes": item.notes} for item in plan.items],
        "changes": [{"kind": c.kind, "name": c.name, "labels": c.labels, "action": c.action, "path": str(c.path),
                     "source": str(c.source), "new_folder": str(c.new_folder) if c.new_folder else None,
                     "to": str(c.to) if c.to else None} for c in plan.changes],
        "renames": [{"kind": item.kind, "from": item.renamed_from.partition("/")[2], "to": item.name}
                    for item in plan.items if item.decision and item.decision.action == "rename"],
        "needs_answer": needs,
        "excluded": [{"kind": e.kind, "name": e.name, "labels": e.labels, "reason": e.reason} for e in plan.excluded],
        "new_folders": [str(folder) for folder in plan.new_folders],
        "notices": plan.notices,
        "would_save": would_save(plan) if saves else [],
        "summary": f"{len(plan.changes)} changes in {len(plan.folders_changed)} folders, {len(plan.conflicts)} conflicts",
        "command": command,
    }
