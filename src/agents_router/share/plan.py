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
# Codex's own agent roles: a custom agent with one of these names would replace it.
CODEX_BUILTIN_AGENTS = {"default", "explorer", "worker"}
HAS, COPY, UPDATE, SKIPPED, BLOCKED, CONFLICTED, WAITING = "has", "copy", "update", "skipped", "blocked", "conflict", "wait"


@dataclass
class Options:
    kinds: tuple[str, ...] = KINDS
    tool_widening: str = "allow-if-shell"
    skip: set[tuple[str, str]] = field(default_factory=set)  # (item name, label) cells you unticked
    only: dict[str, set[str]] = field(default_factory=dict)  # item name -> the only labels it goes to
    resolve: dict[str, str] = field(default_factory=dict)  # item name -> label whose version wins, or "skip"


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

    @property
    def key(self) -> str:
        return f"{self.kind}/{names.identity(self.name)}"


@dataclass
class Change:
    kind: str
    name: str
    labels: list[str]
    action: str  # "copy", "update", "link" or "relink"
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


def read_items(included: list[Provider], options: Options, now: float, disabled: set[str]) -> tuple[list[Item], list[Excluded]]:
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
                physical = _read(kind, cli, entry, labels, now)
                key = f"{kind}/{names.identity(name)}"
                item = items.setdefault(key, Item(kind, name))
                same = next((copy for copy in item.copies if copy.real == physical.real), None)
                if same:
                    same.labels += [label for label in labels if label not in same.labels]
                else:
                    item.copies.append(physical)
                    if entry_name(item, physical) != item.name:
                        item.notes.append(f"names collide after case-folding: {item.name}, {entry_name(item, physical)}")
    shared = []
    for item in items.values():
        problem = _item_problem(item)
        if problem:
            excluded.append(Excluded(item.kind, item.name, sorted({l for c in item.copies for l in c.labels}), problem))
        else:
            shared.append(item)
    return sorted(shared, key=lambda item: (KINDS.index(item.kind), item.name)), excluded


def entry_name(item: Item, physical: Physical) -> str:
    return physical.path.parent.name if physical.path.name == model.AGY_NESTED else (
        physical.path.stem if item.kind == "agents" else physical.path.name)


def _read(kind: str, cli: str, entry: Path, labels: list[str], now: float) -> Physical:
    mtime = _mtime(entry)
    fresh = now - mtime < FRESH_SECONDS
    try:
        obj = model.read_agent(cli, entry) if kind == "agents" else model.read_skill(entry)
    except model.ItemError as exc:
        return Physical(entry, _real(entry), list(labels), None, problem=str(exc), fresh=fresh, mtime=mtime)
    real = str(obj.real) if kind == "skills" else _real(entry)
    return Physical(entry, real, list(labels), obj.digest, fresh=fresh, obj=obj, mtime=mtime)


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


def decide_items(items: list[Item], included: list[Provider], options: Options, state: State) -> None:
    by_label = {provider.label: provider for provider in included}
    for item in items:
        last = state.last(item.kind, names.identity(item.name))
        holders = {label: copy for copy in item.copies for label in copy.labels}
        source = next((copy for copy in item.copies if copy.version), None)
        allowed = options.only.get(item.name)
        targets = set()
        for label, provider in by_label.items():
            if label in holders:
                item.cells[label] = Cell(HAS)
                continue
            reason = _blocked(item, provider, options, source)
            if reason:
                item.cells[label] = Cell(BLOCKED, reason)
            elif (item.name, label) in options.skip or (allowed is not None and label not in allowed):
                item.cells[label] = Cell(SKIPPED, "you unticked it" if (item.name, label) in options.skip else "--only")
            else:
                item.cells[label] = Cell(COPY)
                targets.add(label)
        copies = [engine.Copy(label, copy.version, copy.fresh, copy.problem) for label, copy in holders.items()]
        decision = engine.decide(copies, targets | set(holders), last)
        choice = options.resolve.get(item.name)
        if decision.action == engine.CONFLICT and choice:
            decision = _resolved(item, decision, choice, holders, targets)
        item.decision = decision
        if decision.action != engine.WRITE:
            # Nothing is copied this run: a conflict, a wait, a stop or a pending delete holds the item.
            held = CONFLICTED if decision.action == engine.CONFLICT else WAITING
            for label in by_label:
                if item.cells[label].state == COPY:
                    item.cells[label] = Cell(held, decision.reason)
        for label in decision.updates:
            item.cells[label] = Cell(UPDATE)


def _resolved(item: Item, decision: engine.Decision, choice: str, holders: dict[str, Physical],
              targets: set[str]) -> engine.Decision:
    if choice == "skip":
        return engine.Decision(engine.NOTHING, reason="left as it is (--resolve skip)")
    if choice not in holders or holders[choice].version is None:
        return decision
    version = holders[choice].version
    updates = sorted(label for label, copy in holders.items() if copy.version != version)
    return engine.Decision(engine.WRITE, version=version, source=choice, updates=updates, fills=sorted(targets),
                           reason=f"resolved: {choice}'s version")


# Changes -------------------------------------------------------------------


def changes_for(item: Item, included: list[Provider]) -> list[Change]:
    decision = item.decision
    if not decision or decision.action != engine.WRITE:
        return []
    by_label = {provider.label: provider for provider in included}
    holders = {label: copy for copy in item.copies for label in copy.labels}
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
    for label in decision.updates:
        copy = holders[label]
        if copy.real in planned:
            planned[copy.real].labels.append(label)
            continue
        action = "update" if item.kind == "agents" else "relink"
        planned[copy.real] = Change(item.kind, item.name, [label], action, copy.path, source_path, decision.version)
    return list(planned.values())


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
                item.notes.append(f"{item.name} in {', '.join(missing)}: no `{server}` server ({where})")
        if tools is not None and getattr(source.obj, "cli", None) == "claude":
            for label in item.decision.fills:
                cli = by_label[label].cli
                lost = toolmap.to_kiro(tools)[1] if cli == "kiro" else toolmap.to_antigravity(tools)[1] if cli == "antigravity" else []
                lost = [tool for tool in lost if not toolmap.mcp_reference(tool)]
                if lost:
                    item.notes.append(f"{item.name} in {label}: no {cli} tool for {', '.join(lost)}")


# The whole plan --------------------------------------------------------------


def build(config: Any, rows: list[providers.Candidate], included: list[Provider], options: Options, state: State,
          now: float | None = None, home: Path | None = None) -> Plan:
    now = time.time() if now is None else now
    home = home or Path.home()
    codex = next((provider.folder for provider in included if provider.cli == "codex"), None)
    disabled = exclude.codex_disabled_skills(codex) if codex else set()
    items, excluded = read_items(included, options, now, disabled)
    decide_items(items, included, options, state)
    changes = [change for item in items for change in changes_for(item, included)]
    add_notes(items, included, home)
    return Plan(included=included, rows=rows, items=items, changes=changes, excluded=excluded, options=options)


# Showing it --------------------------------------------------------------------

MARKS = {HAS: "·", COPY: "+", UPDATE: "~", SKIPPED: "x", BLOCKED: "-", CONFLICTED: "!", WAITING: "…"}


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


def render_text(plan: Plan, command: str) -> str:
    lines = ["agt share: dry run, nothing was written.", "", "Folders:"]
    used = {provider.label for provider in plan.included}
    for row in plan.rows:
        provider = row.provider
        mark = "[x]" if provider.label in used else "[-]" if row.reason else "[ ]"
        detail = row.reason or f"{row.holds.get('agents', 0)} agents, {row.holds.get('skills', 0)} skills"
        lines.append(f"  {mark} {provider.label:<16} {short(provider.folder):<22} {detail}")
    labels = [provider.label for provider in plan.included]
    shown = [item for item in plan.items if any(cell.state != HAS for cell in item.cells.values())]
    footnotes: list[str] = []
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
                if cell.state == BLOCKED:
                    footnotes.append(f"{item.name} in {label}: {cell.note}")
                    mark += str(len(footnotes))
                cells.append(f"{mark:<12}")
            lines.append(f"  {item.name:<{width - 2}}" + "".join(cells))
    if shown:
        lines += ["", "  + copy  ~ update  · has it  x you unticked it  - can't go there  ! conflict  … waiting"]
        lines += [f"  {index}. {note}" for index, note in enumerate(footnotes, 1)]
    if plan.conflicts:
        lines += ["", "Conflicts (nothing is written for these until you pick a version):"]
        for item in plan.conflicts:
            newest = max(item.copies, key=lambda copy: copy.mtime)
            versions = ", ".join(f"{'/'.join(copy.labels)} {short(copy.path)}" + (" (newer)" if copy is newest else "")
                                 for copy in item.copies)
            lines.append(f"  {item.name} ({item.kind}): {versions}")
            lines.append(f"    choose with --resolve {item.name}=<label>, or `agt share resolve {item.name}`")
    if plan.waiting:
        lines += ["", "Waiting:"] + [f"  {item.name}: {item.decision.reason}" for item in plan.waiting]
    if plan.excluded:
        lines += ["", "Not shared:"] + [f"  {line}" for line in _group_excluded(plan.excluded)]
    notes = [note for item in plan.items for note in item.notes]
    if notes:
        lines += ["", "Notes:"] + [f"  {note}" for note in notes]
    skips = sorted(plan.options.skip)
    if skips:
        lines += ["", "Would save in your user settings:"] + [f"  {name}: not in {label}" for name, label in skips]
    if plan.new_folders:
        lines += ["", "New folders: " + ", ".join(short(folder) for folder in plan.new_folders)]
    count = len(plan.changes)
    lines += ["", f"{count} change{'s' if count != 1 else ''} in {len(plan.folders_changed)} folder"
                  f"{'s' if len(plan.folders_changed) != 1 else ''}."]
    if count or plan.conflicts:
        lines.append(f"Plan {plan.digest}. Writing isn't in this build yet; once it is, this applies the plan:")
        lines.append(f"  {command}")
    return "\n".join(lines)


def proposal(plan: Plan, command: str) -> dict[str, Any]:
    """The same plan for an agent: every cell, the answers it needs, and the command to run."""

    needs = []
    for item in plan.conflicts:
        newest = max(item.copies, key=lambda copy: copy.mtime)
        choices = [{"value": copy.labels[0], "path": str(copy.path), "newer": copy is newest} for copy in item.copies]
        needs.append({
            "id": f"resolve:{item.name}", "ask": f"{item.name} differs between {', '.join(c['value'] for c in choices)}. Which version should every CLI get?",
            "choices": choices + [{"value": "skip", "path": None, "newer": False}], "flag": f"--resolve {item.name}=<choice>",
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
        "summary": f"{len(plan.changes)} changes in {len(plan.folders_changed)} folders, {len(plan.conflicts)} conflicts",
        "command": command,
    }
