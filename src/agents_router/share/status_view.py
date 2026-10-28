"""`agt share status` for a person: a headline, a row per kind that shares, then each thing that waits with the
commands that settle it. The dry runs reuse the rows.

The JSON report and the attention list's messages are a contract for agents, so this builds its own words from
the same plan; it never changes them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from agents_router import ui
from agents_router.hosts import HOST_LABELS
from agents_router.share import attention, triggers, watch
from agents_router.share import plan as planner
from agents_router.share.providers import _short as short

# The labels on a wait's commands, in the order of its fixes (at most 19 characters, so they share one column).
FIXES = {
    "conflict": ["Pick a version"],
    "delete": ["Remove its copies"],
    "new": ["Pick where it goes"],
    "frozen": ["Keep them out", "Put them back"],
    "rename": ["Rename every copy"],
    "changed": ["Approve it again"],
    "adopt": ["Add the section"],
    "plugin": ["Install it as it is", "Keep it out"],
    "failing": ["Try again now", "Keep it out"],
}
WATCHER_FIX = {"restart": "Restart it", "install": "Install it", "repair": "Repair it"}
NOUNS = {"agents": "agent", "skills": "skill", "mcp": "MCP server", "hooks": "hook", "plugins": "plugin"}
REPO_FLAG = ("agt share pick ", "agt share resolve ", "agt share rm ", "agt share mv ", "agt share undo ")
MISSING = re.compile(r"^(?P<label>.+)'s (?P<kind>\w+) folder is missing: nothing is filled or deleted there until it's back$")


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _joined(parts: list[list[ui.Piece]]) -> list[ui.Piece]:
    row: list[ui.Piece] = []
    for part in parts:
        row += ([", "] if row else []) + part
    return row


# Rows ------------------------------------------------------------------------------------------


def _shared_row(plan: planner.Plan, in_sync: int) -> ui.Line:
    kinds = [kind for kind in ("agents", "skills") if kind in plan.options.kinds]
    noun = {("agents",): ("agent", "agents"), ("skills",): ("skill", "skills")}.get(tuple(kinds),
                                                                                   ("agent or skill", "agents and skills"))
    text = f"{_plural(in_sync, *noun)} in sync"
    row: list[ui.Piece] = [ui.ok(text) if in_sync else text]
    if plan.changes:
        row.append(f", {len(plan.changes)} to write")
    if plan.excluded:
        row.append(f", {len(plan.excluded)} not shared")
    return row


def _instructions_row(shared: Any) -> ui.Line:
    decision = shared.decision
    if decision is None:
        return "no file takes part yet"
    if shared.conflict:
        return ui.warn(f"differ between {_and(list(decision.labels))}")
    if decision.action == "wait":
        return "saved less than 3 s ago; the next sync copies it" if decision.reason == "saved less than 3 s ago" \
            else ui.warn("can't be read")
    if shared.writes:
        return f"{len(shared.writes)} to write ({', '.join(copy.label for copy in shared.writes)}), from {shared.source.label}"
    return f"the same in {', '.join(copy.label for copy in shared.copies if copy.state in ('shared', 'empty'))}"


def _targets(pairs: list[tuple[str, str]], word: str) -> str:
    """("tracker", "codex"), ... as "tracker to codex and kiro, brave-search to kiro"."""

    where: dict[str, list[str]] = {}
    for name, label in pairs:
        where.setdefault(name, []).append(label)
    return ", ".join(f"{name} {word} {_and(labels)}" for name, labels in where.items())


def _mcp_row(servers: Any, named: bool) -> ui.Line:
    if not servers.servers and not servers.problems:
        return ui.muted("none in the folders sharing")
    unsettled = {server.name for server in servers.conflicts + servers.pending}
    same = sum(1 for server in servers.servers if not server.held and server.name not in unsettled)
    parts: list[list[ui.Piece]] = []
    if same:
        parts.append([ui.ok(str(same)), ", the same everywhere it goes" if same == 1 else ", the same everywhere they go"])
    if servers.conflicts:
        parts.append([ui.warn(f"{len(servers.conflicts)} {'differs' if len(servers.conflicts) == 1 else 'differ'} between CLIs")])
    if servers.pending:
        parts.append([ui.warn(f"{len(servers.pending)} removed where {'it' if len(servers.pending) == 1 else 'they'} started")])
    if servers.writes and not named:
        parts.append([f"{len(servers.writes)} to write"])
    held = sum(1 for server in servers.servers if server.held)
    if held:
        parts.append([f"{held} {'stays in its' if held == 1 else 'stay in their'} own CLI"])
    blocked = sum(1 for server in servers.servers if not server.held
                  and any(label not in server.copies for label in server.blocked))
    if blocked:
        parts.append([f"{blocked} can't be copied everywhere"])
    if servers.writes and named:  # last, so the names' commas don't run into the counts'
        parts.append([f"{len(servers.writes)} to write: {_targets([(server.name, label) for server, label in servers.writes], 'to')}"])
    return _joined(parts) or ui.muted("none shared yet")


def _unapproved(hooks: Any) -> list[str]:
    return [hook.name for hook in hooks.hooks
            if any(why is None and not hooks._approved(hook, label) for label, why in hook.verdicts.items())]


def _hooks_row(hooks: Any) -> ui.Line:
    if not hooks.hooks:
        return ui.muted("none in the folders sharing")
    live = [copy for copy in hooks.copies if not copy.waiting]
    parts = []
    if live:
        parts.append(f"{len(live)} copy running in another CLI" if len(live) == 1 else f"{len(live)} copies running in other CLIs")
    unapproved = _unapproved(hooks)
    if unapproved:
        parts.append(f"{len(unapproved)} not approved for another CLI yet: {', '.join(unapproved)}")
    return "; ".join(parts) or f"{len(hooks.hooks)} found, none can run in another CLI"


def _plugins_row(plugins: Any, named: bool) -> ui.Line:
    if not plugins.entries and not plugins.problems:
        return ui.muted("none in the folders sharing")
    parts = [f"{sum(1 for entry in plugins.entries if not entry.held and entry.decision)} shared"]
    if plugins.writes and not named:
        parts.append(f"{len(plugins.writes)} to install")
    held = sum(1 for entry in plugins.entries if entry.held)
    if held:
        parts.append(f"{held} {'stays in its' if held == 1 else 'stay in their'} own CLI")
    blocked = sum(1 for entry in plugins.entries if not entry.held and entry.stays)
    if blocked:
        parts.append(f"{blocked} can't be installed everywhere")
    if plugins.writes and named:  # last, so the names' commas don't run into the counts'
        parts.append(f"{len(plugins.writes)} to install: {_targets([(entry.name, label) for entry, label in plugins.writes], 'in')}")
    return ", ".join(parts)


def kind_rows(plan: planner.Plan, *, named: bool = False) -> list[list[ui.Line]]:
    """A row for each kind past agents and skills that shares. `named` (a dry run) names what each write goes to."""

    rows: list[list[ui.Line]] = []
    if plan.instructions is not None:
        rows.append([ui.muted("Instructions"), _instructions_row(plan.instructions)])
    if plan.mcp is not None:
        rows.append([ui.muted("MCP servers"), _mcp_row(plan.mcp, named)])
    if plan.hooks is not None:
        rows.append([ui.muted("Hooks"), _hooks_row(plan.hooks)])
    if plan.plugins is not None:
        rows.append([ui.muted("Plugins"), _plugins_row(plan.plugins, named)])
    return rows


def kind_notes(plan: planner.Plan) -> list[str]:
    """What the kinds' writes need you to know (a login to make, a CLI to restart), and hooks approved but refused."""

    notes = plan.mcp.notes() if plan.mcp is not None and plan.mcp.writes else []
    notes += plan.plugins.notes(restart=False) if plan.plugins is not None else []
    return notes + (plan.hooks.refused if plan.hooks is not None else [])


def _watcher_rows(saved: Any, health: dict[str, Any], problems: list[tuple[str, str]]) -> list[list[ui.Line]]:
    if not saved.watcher:
        row: ui.Line = "off (share.watcher = false); sessions and saves still catch up"
    elif triggers.watcher_supported():
        row = ui.muted(f"none: {triggers.watcher_supported()}")
    elif not health["installed"]:
        row = ui.error("not installed")
    elif health["stopped"]:
        row = ui.error(f"stopped, its last run was {watch._ago(health['last_run_seconds'])}"
                       if health["last_run_seconds"] is not None else "stopped before its first run")
    elif any(sentence.startswith("launchd") for _, sentence in problems):
        row = ui.warn("on, but launchd doesn't list it")
    else:
        row = f"on, last run {watch._ago(health['last_run_seconds'])}: {health['last_result']}" \
            if health["last_run_seconds"] is not None else "on, no run yet"
    rows = [[ui.muted("Watcher"), row]]
    if health["launcher_problem"]:
        rows.append([ui.muted("Launcher"), ui.warn(health["launcher_problem"])])
    return rows


def _repo_name(entry: dict[str, Any]) -> str:
    return str(entry["root"]).rstrip("/").rpartition("/")[2]


def _repo_rows(repos: list[dict[str, Any]]) -> list[list[ui.Line]]:
    rows = []
    for index, entry in enumerate(repos):
        if entry.get("busy"):
            text: ui.Line = ui.muted(f"left alone while git is in the middle of a {entry['busy']}")
        else:
            text = f"{entry['in_sync']} in sync" + (f", {entry['to_write']} to write" if entry.get("to_write") else "")
        rows.append([ui.muted("" if index else "Repos"), [_repo_name(entry), "  ", text]])
    return rows


# Waits -----------------------------------------------------------------------------------------


def _title(entry: dict[str, Any], plan: planner.Plan) -> list[ui.Piece]:
    kind, item = entry["kind"], entry["item"]
    folders = entry.get("labels") or entry.get("folders") or []
    noun, _, name = item.partition("/")
    if kind == "frozen":
        label = entry["id"].rpartition(":")[2]
        frozen = next((found for found in plan.frozen if found["label"] == label), None)
        return [ui.bold(f"{frozen['kind']} in {label}" if frozen else label)]
    if kind == "adopt" or noun == "instructions":
        title: list[ui.Piece] = [ui.bold("instructions")]
    elif name:
        title = [ui.bold(f"{NOUNS.get(noun, noun.rstrip('s'))} {name.partition('@')[0] if noun == 'plugins' else name}")]
    else:
        title = [ui.bold(f"{NOUNS.get(item, item)}")]  # a whole kind, as in "agt can't read hooks in claude"
    if kind == "plugin":
        return title + [f", for {_and(folders)}"]
    if kind == "failing" and entry.get("why") is not None:
        return title + [f", in {_and(folders)}: the last install failed"]
    if kind == "adopt":
        return title + [f" in {_and(folders)}"]
    return title + ([f", in {_and(folders)}"] if folders and kind != "delete" else [])


def _sentence(text: str, names: set[str] = frozenset()) -> str:
    """A reason as a sentence. One that starts with a command, a number, "agt", or one of `names` (items and folder
    labels, which are typed as they are) keeps its case."""

    first = re.split(r"[\s':;,]", text, maxsplit=1)[0]
    keep = text.startswith(("agt ", "`")) or text[:1].isdigit() or first in names
    return (text if keep else text[:1].upper() + text[1:]) + ("" if text.endswith(".") else ".")


def _names(plan: planner.Plan) -> set[str]:
    return {provider.label for provider in plan.included} | {item.name for item in plan.items} \
        | {entry.name for entry in plan.excluded}


def _reasons(entry: dict[str, Any], plan: planner.Plan) -> list[ui.Line]:
    """Why it waits, never the item and folders again: the title has them."""

    kind, item = entry["kind"], entry["item"]
    folders = entry.get("labels") or entry.get("folders") or []
    if kind == "plugin":
        return [_sentence(entry["why"])]
    if kind == "failing" and entry.get("why") is not None:
        return [ui.inline(_sentence(entry["why"])), "agt tries again when its version changes."]
    if kind in ("failing", "unreadable"):
        why = entry.get("why") or entry["message"].partition(": ")[2] or entry["message"]
        return [ui.inline(_sentence(why, _names(plan))), ui.muted("Fix it by hand; the next sync tries again.")]
    if kind == "adopt":
        by_label = {provider.label: provider for provider in plan.included}
        files = {copy.label: copy.path.name for copy in plan.instructions.unadopted} if plan.instructions is not None else {}
        if len(folders) == 1 and folders[0] in by_label:
            cli = HOST_LABELS.get(by_label[folders[0]].cli, folders[0])
            return [f"{cli}'s {files.get(folders[0], 'instruction file')} has no shared section yet, "
                    f"so {cli} doesn't get your shared instructions."]
        return ["Their instruction files have no shared section yet, so they don't get your shared instructions."]
    if kind == "changed":
        return ["It changed since you approved it, so its copy there does nothing until you approve it again."]
    if kind == "conflict":
        return ["The copies were edited differently."]
    if kind == "delete":
        where = _and(folders) if folders else "one CLI"
        if item.startswith(("agents/", "skills/")):
            return [f"Its original was deleted in {where}."]
        return [f"It was {'uninstalled' if item.startswith('plugins/') else 'removed'} in {where}."]
    if kind == "new":
        return ["It's new; with auto-copy off it waits for you to pick where it goes."]
    return [ui.inline(_sentence(entry["message"], _names(plan) | {entry["item"].partition("/")[2]}))]


def _commands(entry: dict[str, Any], repo: dict[str, Any] | None) -> list[tuple[str, str]]:
    labels = FIXES.get(entry["kind"], [])
    found = []
    for index, command in enumerate(attention.commands(entry)):
        if repo is not None:  # the folders a bare command acts on are yours; name the repo's
            root = short(Path(repo["root"]))
            command = f"{command} --repo {root}" if command.startswith(REPO_FLAG) else f"cd {root} && {command}"
        found.append((labels[index] if index < len(labels) else "Run", command))
    return found


def _watcher_wait(problems: list[tuple[str, str]]) -> dict[str, Any]:
    """Every problem with the background side is one wait, fixed by one `agt doctor --fix`."""

    label = WATCHER_FIX[problems[0][0]] if len(problems) == 1 else WATCHER_FIX["repair"]
    return {"kind": "watcher", "reasons": [sentence for _, sentence in problems], "label": label}


def diff_lines(diff: list[str]) -> list[ui.Line]:
    """A difference between copies, muted, with its added lines green and its removed lines red."""

    return [["    ", ui.ok(line) if line.startswith("+") and not line.startswith("+++") else
             ui.error(line) if line.startswith("-") and not line.startswith("---") else ui.muted(line)] for line in diff]


def wait_lines(waits: list[tuple[dict[str, Any], dict[str, Any] | None]], plan: planner.Plan,
               diff: Callable[[dict[str, Any]], list[str]] | None = None) -> list[ui.Line]:
    """Each wait (with its listed repo, or None): a mark and title, why, and its commands in one column throughout."""

    shown = []
    for entry, repo in waits:
        if entry["kind"] == "watcher":
            shown.append((ui.Span("error-mark", "✗"), [ui.bold("background watcher")], entry["reasons"],
                          [(entry["label"], "agt doctor --fix")], entry))
            continue
        mark = ui.Span("error-mark", "✗") if entry["kind"] in ("unreadable", "failing") else ui.Span("warn-mark", "!")
        title = _title(entry, plan) + ([f" (repo {_repo_name(repo)})"] if repo is not None else [])
        shown.append((mark, title, _reasons(entry, plan), _commands(entry, repo), entry))
    column = max((len(label) for *_, commands, _ in shown for label, _ in commands), default=0) + 3
    lines: list[ui.Line] = [ui.bold("Waiting for you")]
    for index, (mark, title, reasons, commands, entry) in enumerate(shown):
        lines += [""] if index else []
        lines.append(["  ", mark, " ", *title])
        lines += [["    ", *(reason if isinstance(reason, list) else [reason])] for reason in reasons]
        lines += [["    ", ui.muted(label), " " * (column - len(label)), ui.cmd(command)] for label, command in commands]
        if diff is not None and entry["kind"] != "watcher":
            lines += diff_lines(diff(entry))
    return lines


def notes_lines(plan: planner.Plan, needs: list[dict[str, Any]], fresh: list[planner.Item],
                extra: list[str] = ()) -> list[ui.Line]:
    """The plan's notices that no wait already says, the items saved a moment ago, and `extra`."""

    shown = {entry["message"] for entry in needs}
    notes = []
    for note in plan.notices:
        if any(note.startswith(message) for message in shown) or "disappeared from" in note:
            continue
        missing = MISSING.match(note)
        notes.append(f"The {missing['kind']} folder in {missing['label']} is missing, so nothing is copied to it or "
                     "deleted from it until it's back." if missing else note)
    if fresh:
        names = _and([item.name for item in fresh])
        notes.append(f"{names} {'was' if len(fresh) == 1 else 'were'} saved less than 3 s ago; the next sync copies "
                     f"{'it' if len(fresh) == 1 else 'them'}.")
    notes += list(extra)
    return [ui.bold("Notes"), *(["  ", *ui.inline(ui.tilde(note))] for note in notes)] if notes else []


# The screen ------------------------------------------------------------------------------------


def lines(plan: planner.Plan, saved: Any, *, in_sync: int, needs: list[dict[str, Any]], health: dict[str, Any],
          fresh: list[planner.Item], repos: list[dict[str, Any]],
          diff: Callable[[dict[str, Any]], list[str]] | None = None) -> list[ui.Line]:
    """`needs` is the attention list as this plan sees it; `fresh`, the items saved less than 3 s ago."""

    problems = watch.watcher_problems(saved, health)
    rows: list[list[ui.Line]] = [[ui.muted("Folders"), ", ".join(provider.label for provider in plan.included)],
                                 [ui.muted("Shared"), _shared_row(plan, in_sync)], *kind_rows(plan)]
    rows += _watcher_rows(saved, health, problems) + _repo_rows(repos)
    waits: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
        (entry, None) for entry in attention.grouped(needs) if entry["kind"] != "watcher"]
    waits += [(_watcher_wait(problems), None)] if problems else []
    waits += [(entry, repo) for repo in repos for entry in attention.grouped(repo.get("attention", []))]
    writes = len(plan.changes) + sum(len(shared.writes) for shared in (plan.instructions, plan.mcp, plan.plugins)
                                     if shared is not None) + sum(repo.get("to_write") or 0 for repo in repos)
    if waits:
        headline: ui.Piece = ui.warn(f"{_plural(len(waits), 'thing waits', 'things wait')} for you")
    elif writes:
        headline = f"{_plural(writes, 'change', 'changes')} to write"
    else:
        headline = ui.ok("all in sync")
    found: list[ui.Line] = [[ui.bold("agt share"), "  on, ", headline], "", *ui.columns(rows, gap=3)]
    if waits:
        found += ["", *wait_lines(waits, plan, diff)]
    pointers: list[ui.Line] = []
    if plan.hooks is not None and _unapproved(plan.hooks):
        pointers.append([ui.muted("Approve one with "), ui.code("agt share pick hooks/<name> --in <cli>"), ui.muted(".")])
    if plan.plugins is not None and any(entry.held or entry.stays for entry in plan.plugins.entries):
        pointers.append([ui.code("agt share plugins"), ui.muted(" says why the others stay where they are.")])
    if writes:
        running = saved.watcher and health["installed"] and not health["stopped"] and not triggers.watcher_supported() \
            and not problems
        pointers.append([ui.code("agt share sync"), " writes the changes"
                         + (" now; the watcher does it within a minute." if running else ".")])
    found += ["", *pointers] if pointers else []
    notes = notes_lines(plan, needs, fresh, plan.hooks.refused if plan.hooks is not None else [])
    return found + (["", *notes] if notes else [])
