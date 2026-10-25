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
    keep_copies: bool = False  # a copy left out by a pick stays as your own file instead of being removed

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

    @property
    def key(self) -> str:
        return f"{self.kind}/{names.identity(self.name)}"


@dataclass
class Change:
    kind: str
    name: str
    labels: list[str]
    action: str  # "copy", "update", "link", "relink" or "remove"
    path: Path
    source: Path
    version: str
    new_folder: Path | None = None


@dataclass
class Excluded:
    kind: str
    name: str
    labels: list[str]
    reason: str


@dataclass
class Plan:
    included: list[Provider]
    rows: list[providers.Candidate]
    items: list[Item]
    changes: list[Change]
    excluded: list[Excluded]
    options: Options
    notices: list[str] = field(default_factory=list)

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
                    self.options, list(self.notices))

    @property
    def digest(self) -> str:
        """The plan's id for `--if-plan`: it changes whenever any write or conflict would change."""

        content = {
            "changes": sorted([c.kind, c.name, c.action, str(c.path), c.version] for c in self.changes),
            "conflicts": sorted([i.kind, i.name, sorted(p.version or "" for p in i.copies)] for i in self.conflicts),
        }
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
            excluded.append(Excluded(item.kind, item.name, sorted({l for c in item.copies for l in c.labels}), problem))
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


def absent_labels(items: list[Item], included: list[Provider], options: Options, state: State) -> tuple[dict[str, set[str]], list[str]]:
    """Per kind, the labels that count as absent this run, and why.

    A folder whose kind folder is missing, after agt has synced it, is absent:
    nothing is filled or deleted there. So is a folder where more than
    max(3, 25%) of the items agt knew disappeared at once: its deletes freeze
    until you look. Labels that stopped sharing are absent too.
    """

    by_label = {provider.label: provider for provider in included}
    present = {(item.key, label) for item in items for copy in item.copies for label in copy.labels}
    absent: dict[str, set[str]] = {kind: set() for kind in options.kinds}
    notices: list[str] = []
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
                if (key, label) not in present:
                    gone[label] = gone.get(label, 0) + 1
        for label in known:
            location = by_label[label].writable(kind)
            if location is None or not location.path.is_dir():
                absent[kind].add(label)
                notices.append(f"{label}'s {kind} folder is missing: nothing is filled or deleted there until it's back")
        for label in engine.frozen(known, gone) - absent[kind]:
            absent[kind].add(label)
            notices.append(f"{gone[label]} of {known[label]} {kind} disappeared from {label} at once, "
                           "so deletes there wait until you check (`agt share status`)")
    return absent, notices


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


def decide_items(items: list[Item], included: list[Provider], options: Options, state: State) -> list[str]:
    by_label = {provider.label: provider for provider in included}
    absent, notices = absent_labels(items, included, options, state)
    for item in items:
        last = state.last(item.kind, names.identity(item.name))
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
        item.ignored = ignore & set(holders)
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
    return notices


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
    notices = decide_items(items, included, options, state)
    changes = [change for item in items for change in changes_for(item, included)]
    add_notes(items, included, home)
    return Plan(included=included, rows=rows, items=items, changes=changes, excluded=excluded, options=options,
                notices=notices)


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


def render_text(plan: Plan, command: str, *, header: str = "agt share: dry run, nothing was written.") -> str:
    lines = [header, "", "Folders:"]
    used = {provider.label for provider in plan.included}
    for row in plan.rows:
        provider = row.provider
        mark = "[x]" if provider.label in used else "[-]" if row.reason else "[ ]"
        detail = row.reason or f"{row.holds.get('agents', 0)} agents, {row.holds.get('skills', 0)} skills"
        lines.append(f"  {mark} {provider.label:<16} {short(provider.folder):<22} {detail}")
    lines += table(plan, [item for item in plan.items if any(cell.state != HAS for cell in item.cells.values())])
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
    saving = would_save(plan)
    if saving and command:
        lines += ["", "Would save in your user settings:"] + [f"  {line}" for line in saving]
    if plan.new_folders:
        lines += ["", "New folders: " + ", ".join(short(folder) for folder in plan.new_folders)]
    count = len(plan.changes)
    lines += ["", f"{count} change{'s' if count != 1 else ''} in {len(plan.folders_changed)} folder"
                  f"{'s' if len(plan.folders_changed) != 1 else ''}."]
    if (count or plan.conflicts or saving) and command:
        lines.append(f"Plan {plan.digest}. To apply it:")
        lines.append(f"  {command}")
    return "\n".join(lines)


def proposal(plan: Plan, command: str) -> dict[str, Any]:
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
    return {
        "dry_run": True,
        "plan": plan.digest,
        "folders": [{"label": row.provider.label, "cli": row.provider.cli, "path": str(row.provider.folder),
                     "used": row.provider.label in {p.label for p in plan.included}, "suggested": row.suggested,
                     "reason": row.reason, "holds": row.holds} for row in plan.rows],
        "items": [{"kind": item.kind, "name": item.name, "decision": item.decision.action if item.decision else None,
                   "reason": item.decision.reason if item.decision else "",
                   "cells": {label: {"state": cell.state, "note": cell.note} for label, cell in item.cells.items()},
                   "notes": item.notes} for item in plan.items],
        "changes": [{"kind": c.kind, "name": c.name, "labels": c.labels, "action": c.action, "path": str(c.path),
                     "source": str(c.source), "new_folder": str(c.new_folder) if c.new_folder else None} for c in plan.changes],
        "needs_answer": needs,
        "excluded": [{"kind": e.kind, "name": e.name, "labels": e.labels, "reason": e.reason} for e in plan.excluded],
        "new_folders": [str(folder) for folder in plan.new_folders],
        "notices": plan.notices,
        "would_save": would_save(plan),
        "summary": f"{len(plan.changes)} changes in {len(plan.folders_changed)} folders, {len(plan.conflicts)} conflicts",
        "command": command,
    }
