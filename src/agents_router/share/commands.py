"""`agt share` commands.

`enable` shows what sharing would do (`--dry-run`) or saves your answers and
does it (`--yes`, plus `--if-plan` from a dry run). `sync` makes the copies
match again from the saved settings, `status` says where things stand, and
`scan` lists every choice setup would offer.

The item commands act on one item at once: `pick` changes which CLIs get it,
`resolve` picks the version that wins a conflict, `rm` removes it everywhere,
and `undo` puts back what the last change to it replaced. Every copy they
replace or remove goes to the backups first. Nothing writes inside agt's own
workers, and a command that needs a choice it wasn't given exits 3 with
`needs_choice` instead of asking an agent.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from agents_router import hosts
from agents_router.config import load_config
from agents_router.share import (
    apply, engine, gitexclude, model, names, normalize, plan as planner, providers, settings, state, toolmap,
)
from agents_router.share.providers import _short as short

SUPPORTED_KINDS = settings.KINDS
LATER_KINDS = ("instructions", "mcp", "commands", "hooks", "plugins")
IN_WORKER = "agt share doesn't write inside agt's workers; run it from your own terminal or session."
NOT_ENABLED = "agt share isn't enabled yet. `agt share enable --dry-run` shows what it would do."


def _folder_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--claude-dir", action="append", default=[], metavar="PATH", help="A Claude folder to share (repeatable).")
    parser.add_argument("--codex-dir", action="append", default=[], metavar="PATH")
    parser.add_argument("--kiro-dir", action="append", default=[], metavar="PATH")
    parser.add_argument("--agy-dir", action="append", default=[], metavar="PATH", help="Antigravity's ~/.gemini folder.")
    parser.add_argument("--no-agy", action="store_true", help="Leave Antigravity out.")
    parser.add_argument("--kinds", default="agents,skills", help="What to share: agents,skills (default).")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON.")


def add_parser(subparsers) -> None:
    share = subparsers.add_parser(
        "share", help="Keep agents and skills the same across Claude Code, Codex, Antigravity and Kiro.",
        description="agt share keeps your agents and skills the same in every CLI folder you pick.",
    )
    actions = share.add_subparsers(dest="share_command", required=True)
    enable = actions.add_parser("enable", help="Turn sharing on: --dry-run shows the plan, --yes saves your answers and applies it.")
    _folder_flags(enable)
    enable.add_argument("--projects", default="none", help="Repos to share too: none (project scope comes later).")
    enable.add_argument("--only", action="append", default=[], metavar="ITEM=LABEL[,LABEL]", help="Keep an item in these CLIs only.")
    enable.add_argument("--skip", action="append", default=[], metavar="ITEM@LABEL", help="Untick one cell (repeatable).")
    enable.add_argument("--resolve", action="append", default=[], metavar="ITEM=LABEL|skip",
                        help="Which version wins for an item that differs between folders; skip doesn't share it.")
    enable.add_argument("--auto-copy", default="on", help="New items go to every target: on, off or a list of kinds.")
    enable.add_argument("--tool-widening", choices=toolmap.TOOL_WIDENING, default="allow-if-shell",
                        help="Codex agents can't limit tools: allow-if-shell copies agents that have a shell, skip copies none with a tool list.")
    enable.add_argument("--dry-run", action="store_true", help="Show the plan; write nothing.")
    enable.add_argument("--yes", action="store_true", help="Save your answers and apply the plan.")
    enable.add_argument("--if-plan", metavar="ID", help="Apply only if the plan is still the one a dry run showed.")
    enable.add_argument("--accept-suggested", action="store_true", help="Take the suggested answer for every open choice.")
    scan = actions.add_parser("scan", help="List every choice setup would offer; writes nothing.")
    _folder_flags(scan)
    sync = actions.add_parser("sync", help="Make every copy match again, from your saved settings.")
    sync.add_argument("--dry-run", action="store_true", help="Show what the sync would do; write nothing.")
    sync.add_argument("--json", action="store_true")
    status = actions.add_parser("status", help="What's in sync, what differs and what waits for you; writes nothing.")
    status.add_argument("--diff", action="store_true", help="Show how the copies of each conflict differ.")
    status.add_argument("--json", action="store_true")
    pick = actions.add_parser("pick", help="Change which CLIs get an item; with no flags, show what goes where.")
    pick.add_argument("item", nargs="?", help="The agent or skill: a bare name, or agents/<name> or skills/<name>.")
    pick.add_argument("--in", dest="in_labels", action="append", default=[], metavar="LABEL[,LABEL]",
                      help="Share it to these CLIs (again).")
    pick.add_argument("--not-in", dest="not_in", action="append", default=[], metavar="LABEL[,LABEL]",
                      help="Stop sharing it there; the copy agt made is removed, with a backup.")
    pick.add_argument("--only", dest="only_in", action="append", default=[], metavar="LABEL[,LABEL]",
                      help="Keep it in these CLIs only.")
    pick.add_argument("--keep-copies", action="store_true", help="Keep the copies you leave out as your own files.")
    pick.add_argument("--json", action="store_true")
    resolve = actions.add_parser("resolve", help="Pick the version that wins when an item's copies differ.")
    resolve.add_argument("item", nargs="?")
    resolve.add_argument("--all", action="store_true", help="Every conflict.")
    resolve.add_argument("--prefer", metavar="LABEL|newest|skip",
                         help="The copy that wins: a CLI's label, newest, or skip to stop sharing the item.")
    resolve.add_argument("--kind", choices=SUPPORTED_KINDS, help="With --all, only this kind.")
    resolve.add_argument("--json", action="store_true")
    remove = actions.add_parser("rm", help="Remove an item from every CLI, with backups.")
    remove.add_argument("item")
    remove.add_argument("--json", action="store_true")
    undo = actions.add_parser("undo", help="Put back what the last change to an item replaced, from the backups.")
    undo.add_argument("item")
    undo.add_argument("--in", dest="in_label", metavar="LABEL", help="Bring back the copy removed from this CLI.")
    undo.add_argument("--json", action="store_true")


def _kinds(value: str) -> tuple[str, ...]:
    kinds = tuple(dict.fromkeys(kind.strip() for kind in value.split(",") if kind.strip()))
    later = [kind for kind in kinds if kind in LATER_KINDS]
    if later:
        raise ValueError(f"sharing {', '.join(later)} comes in a later phase; this build shares agents and skills")
    unknown = [kind for kind in kinds if kind not in SUPPORTED_KINDS]
    if unknown or not kinds:
        raise ValueError(f"unknown kind {', '.join(unknown) or '(none)'}; use agents, skills")
    return kinds


def _explicit_folders(args: argparse.Namespace) -> list[tuple[str, Path]] | None:
    given = [("claude", path) for path in args.claude_dir] + [("codex", path) for path in args.codex_dir]
    given += [("kiro", path) for path in args.kiro_dir] + [("antigravity", path) for path in args.agy_dir]
    if not given:
        return None
    missing = [path for _, path in given if not Path(path).expanduser().is_dir()]
    if missing:
        raise ValueError(f"no such folder: {', '.join(missing)}")
    return [(cli, Path(path).expanduser().absolute()) for cli, path in given]


def _check_label(label: str, labels: set[str], flag: str) -> None:
    if label not in labels:
        raise ValueError(f"{flag}: no folder labelled {label}; the labels are {', '.join(sorted(labels))}")


def _options(args: argparse.Namespace, labels: set[str], saved: settings.Settings | None,
             resolve: dict[str, str] | None = None, keep_copies: bool = False) -> planner.Options:
    """Saved picks first, then the flags on top of them."""

    if saved is not None and not hasattr(args, "kinds"):
        options = planner.Options(kinds=saved.kinds, tool_widening=saved.tool_widening, auto_copy=saved.auto_copy)
    else:
        options = planner.Options(kinds=_kinds(args.kinds), tool_widening=getattr(args, "tool_widening", "allow-if-shell"),
                                  auto_copy=settings.parse_auto_copy(getattr(args, "auto_copy", "on")))
    if saved is not None:
        options.off |= saved.off
        options.skip |= {(key, label) for key, chosen in saved.not_in.items() for label in chosen if label in labels}
        options.only.update({key: {label for label in chosen if label in labels} for key, chosen in saved.only.items()})
    for value in getattr(args, "skip", []):
        item, at, label = value.rpartition("@")
        if not at or not item:
            raise ValueError(f"--skip takes ITEM@LABEL, not {value}")
        _check_label(label, labels, "--skip")
        options.skip.add((item, label))
    for value in getattr(args, "only", []):
        item, sign, rest = value.partition("=")
        chosen = {label.strip() for label in rest.split(",") if label.strip()}
        if not sign or not item or not chosen:
            raise ValueError(f"--only takes ITEM=LABEL[,LABEL], not {value}")
        for label in chosen:
            _check_label(label, labels, "--only")
        options.only[item] = chosen
    for value in getattr(args, "resolve", []):
        item, sign, label = value.partition("=")
        if not sign or not item or not label:
            raise ValueError(f"--resolve takes ITEM=LABEL or ITEM=skip, not {value}")
        if label == "skip":
            options.off.add(item)
            continue
        _check_label(label, labels, "--resolve")
        options.resolve[item] = label
        options.off -= {item, *(f"{kind}/{item}" for kind in SUPPORTED_KINDS)}
    options.resolve.update(resolve or {})
    options.keep_copies = keep_copies
    return options


def _command_line(args: argparse.Namespace, digest: str) -> str:
    parts = ["agt", "share", "enable", "--yes", "--if-plan", digest]
    for flag, values in (("--claude-dir", args.claude_dir), ("--codex-dir", args.codex_dir), ("--kiro-dir", args.kiro_dir),
                         ("--agy-dir", args.agy_dir), ("--only", args.only), ("--skip", args.skip), ("--resolve", args.resolve)):
        for value in values:
            parts += [flag, value]
    if args.no_agy:
        parts.append("--no-agy")
    if args.kinds != "agents,skills":
        parts += ["--kinds", args.kinds]
    if args.tool_widening != "allow-if-shell":
        parts += ["--tool-widening", args.tool_widening]
    if args.auto_copy != "on":
        parts += ["--auto-copy", args.auto_copy]
    return " ".join(shlex.quote(part) for part in parts)


def _folders(config: Any, explicit: list[tuple[str, Path]] | None, no_agy: bool,
             saved: settings.Settings | None) -> tuple[list[providers.Candidate], list[providers.Provider]]:
    """The folder rows and the ones sharing: the flags, else the saved folders, else the suggested ones."""

    if explicit is None and saved is not None and saved.enabled and saved.folders:
        explicit = [(folder.cli, folder.path) for folder in saved.folders]
    rows = providers.candidates(config, explicit, no_agy=no_agy)
    if explicit is None:
        return rows, [row.provider for row in rows if row.suggested]
    given = {(cli, str(folder.resolve())) for cli, folder in explicit}
    included = [row.provider for row in rows if row.reason is None
                and (row.provider.cli, str(row.provider.folder.resolve())) in given]
    if saved is not None:
        # The state is keyed by label, so the saved labels win over freshly worked-out ones.
        for provider in included:
            for folder in saved.folders:
                if folder.cli == provider.cli and providers.same(folder.path, provider.folder):
                    provider.label = folder.label
    return rows, included


def _plan(args: argparse.Namespace, current: state.State, saved: settings.Settings | None, *,
          resolve: dict[str, str] | None = None, keep_copies: bool = False, own: frozenset[str] = frozenset()) -> planner.Plan:
    config = load_config()
    explicit = _explicit_folders(args) if hasattr(args, "claude_dir") else None
    rows, included = _folders(config, explicit, getattr(args, "no_agy", False), saved)
    options = _options(args, {provider.label for provider in included}, saved, resolve, keep_copies)
    return planner.build(config, rows, included, options, current, own=own)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False) if not isinstance(value, str) else value)


def _item_key(plan: planner.Plan, name: str) -> str:
    """`<kind>/<name>`, as the folders spell it, for an item named on the command line (bare when it's unique)."""

    kind, slash, bare = name.partition("/")
    if not slash:
        kind, bare = "", name
    elif kind not in SUPPORTED_KINDS:
        raise ValueError(f"{name}: say agents/<name> or skills/<name>")
    wanted = names.identity(bare)
    found = sorted({(item.kind, item.name) for item in plan.items if names.identity(item.name) == wanted}
                   | {(entry.kind, entry.name) for entry in plan.excluded if names.identity(entry.name) == wanted})
    found = [entry for entry in found if not kind or entry[0] == kind]
    if not found:
        if kind:
            return name
        raise ValueError(f"no agent or skill named {name} in the folders sharing")
    if len({entry[0] for entry in found}) > 1:
        raise ValueError(f"{name} is both an agent and a skill; say agents/{name} or skills/{name}")
    return f"{found[0][0]}/{found[0][1]}"


def _identity(key: str) -> str:
    kind, _, name = key.partition("/")
    return f"{kind}/{names.identity(name)}"


def _find(plan: planner.Plan, key: str) -> planner.Item | None:
    return next((item for item in plan.items if item.key == _identity(key)), None)


def _saved_answers(plan: planner.Plan, args: argparse.Namespace) -> settings.Settings:
    options = plan.options
    result = settings.Settings(enabled=True, kinds=options.kinds, tool_widening=options.tool_widening,
                               auto_copy=options.auto_copy,
                               folders=[settings.Folder(p.cli, p.folder, p.label) for p in plan.included])
    result.off = {_item_key(plan, name) for name in options.off}
    for name, label in options.skip:
        result.not_in.setdefault(_item_key(plan, name), []).append(label)
    for name, chosen in options.only.items():
        result.only[_item_key(plan, name)] = sorted(chosen)
    for name in options.resolve:
        _item_key(plan, name)
    return result


def _needs_choice(plan: planner.Plan, command: str, as_json: bool) -> int:
    proposal = planner.proposal(plan, command)
    asks = proposal["needs_answer"]
    if as_json:
        _print({"error": "needs_choice", "ask": [ask["ask"] for ask in asks], "choices": asks,
                "retry": command + " " + " ".join(ask["flag"] for ask in asks)})
    else:
        print("Nothing was written: these items differ between folders, and agt needs to know which version wins.")
        for ask in asks:
            options = ", ".join(choice["value"] + (" (newer)" if choice.get("newer") else "") for choice in ask["choices"])
            print(f"  {ask['ask']} Choices: {options}. Answer with {ask['flag']}.")
    return 3


def _outcome_line(outcome: apply.Outcome) -> str:
    mark = {"copy": "+", "link": "+", "update": "~", "relink": "~", "remove": "-"}.get(outcome.action, "?")
    line = f"  {mark} {outcome.kind}/{outcome.name} in {', '.join(outcome.labels)}: {short(outcome.path)}"
    return f"{line} ({outcome.message})" if outcome.message else line


def _kept(plan: planner.Plan) -> list[str]:
    """Copies in folders you left out that stay, with why: your own files now, or your original."""

    return [f"{item.name} in {label}: {cell.note}" for item in plan.items for label, cell in item.cells.items()
            if item.drops.get(label) == "release" or (cell.state == planner.HAS and cell.note)]


def _report(plan: planner.Plan, result: apply.Result, saved_at: Path | None, as_json: bool, *,
            title: str = "agt share", notes: list[str] | None = None) -> None:
    extra = (notes or []) + _kept(plan)
    if as_json:
        _print({
            "run": result.run, "written": [_outcome(o) for o in result.written], "failed": [_outcome(o) for o in result.failed],
            "conflicts": [item.name for item in plan.conflicts], "waiting": [item.name for item in plan.waiting],
            "notes": extra + plan.notices + [note for item in plan.items for note in item.notes], "replayed": result.replayed,
            "settings": str(saved_at) if saved_at else None,
            "backups": str(state.share_dir() / "backups" / result.run) if any(o.backup for o in result.written) else None,
            "git_exclude": [{"file": str(path), "lines": lines} for path, lines in result.excluded],
        })
        return
    count = len(result.written)
    folders = sorted({label for outcome in result.written for label in outcome.labels})
    lines = [f"{title}: wrote {count} change{'s' if count != 1 else ''} in {len(folders)} folder{'s' if len(folders) != 1 else ''}."
             if count else f"{title}: everything is in sync; nothing to write."]
    lines += [_outcome_line(outcome) for outcome in result.written]
    if result.failed:
        lines += ["", "Not written (the next sync tries again):"] + [_outcome_line(outcome) for outcome in result.failed]
    if plan.conflicts:
        lines += ["", "Left as they are until you pick a version (`agt share status --diff` shows how they differ):"]
        lines += [f"  {item.name} ({item.kind}): {', '.join('/'.join(copy.labels) for copy in item.copies)}" for item in plan.conflicts]
    if plan.waiting:
        lines += ["", "Waiting:"] + [f"  {item.name}: {item.decision.reason}" for item in plan.waiting]
    notes = extra + result.replayed + plan.notices + [note for item in plan.items for note in item.notes]
    if notes:
        lines += ["", "Notes:"] + [f"  {note}" for note in notes]
    for path, changed in result.excluded:
        lines.append(f"\nListed {len(changed)} of agt's copies in {short(path)}, so that repo's `git status` doesn't show them.")
    if saved_at:
        lines.append(f"\nSaved your answers in {short(saved_at)}.")
    if any(outcome.backup for outcome in result.written):
        lines.append(f"Backups of what was replaced or removed are in {short(state.share_dir() / 'backups' / result.run)}.")
    print("\n".join(lines))


def _outcome(outcome: apply.Outcome) -> dict[str, Any]:
    return {"kind": outcome.kind, "name": outcome.name, "labels": outcome.labels, "action": outcome.action,
            "path": str(outcome.path), "message": outcome.message}


def _enable(args: argparse.Namespace) -> int:
    if args.projects != "none":
        raise ValueError("project scope (--projects) comes in a later phase; use --projects none")
    saved = settings.load()
    if args.dry_run or not args.yes:
        plan = _plan(args, state.load(), saved)
        for name in [*args.resolve, *args.skip, *args.only]:
            _item_key(plan, name.partition("=")[0].rpartition("@")[0] or name.partition("=")[0])
        line = _command_line(args, plan.digest)
        _print(planner.proposal(plan, line) if args.json else planner.render_text(plan, line))
        if not args.dry_run and not args.json:
            print("\nNothing was written. Add --yes to save these answers and apply the plan.")
        return 0 if args.dry_run else 2
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    config = load_config()
    _rows, included = _folders(config, _explicit_folders(args), args.no_agy, saved)
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        plan = _plan(args, current, saved)
        line = _command_line(args, plan.digest)
        if args.if_plan and args.if_plan != plan.digest:
            print(f"Nothing was written: the plan changed since your dry run (it was {args.if_plan}, now {plan.digest}).")
            print(planner.render_text(plan, line))
            return 1
        if plan.conflicts and not (args.if_plan or args.accept_suggested):
            return _needs_choice(plan, line, args.json)
        answers = _saved_answers(plan, args)
        saved_at = settings.save(answers)
        result = apply.apply(plan, current)
        result.replayed = replayed
    _report(plan, result, saved_at, args.json)
    return 1 if result.failed else 0


def _sync(args: argparse.Namespace) -> int:
    saved = settings.load()
    if not saved.enabled:
        _print({"enabled": False, "message": NOT_ENABLED} if args.json else NOT_ENABLED)
        return 0
    if args.dry_run:
        plan = _plan(args, state.load(), saved)
        _print(planner.proposal(plan, "agt share sync", saves=False) if args.json else
               planner.render_text(plan, "agt share sync", header="agt share sync: dry run, nothing was written.", saves=False))
        return 0
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        plan = _plan(args, current, saved)
        if any(item.decision.reason == "saved less than 3 s ago" for item in plan.waiting):
            time.sleep(planner.FRESH_SECONDS)  # wait the edit out instead of skipping it
            plan = _plan(args, current, saved)
        result = apply.apply(plan, current)
        result.replayed = replayed
    _report(plan, result, None, args.json)
    return 1 if result.failed else 0


def _core_text(copy: planner.Physical, kind: str) -> list[str]:
    if copy.obj is None:
        return [f"(can't be read: {copy.problem})"]
    if kind == "agents":
        core = copy.obj.core
        return [f"name: {core['name']}", f"description: {core['description']}", "", *core["body"].splitlines()]
    text = normalize.text((Path(copy.obj.real) / "SKILL.md").read_text(encoding="utf-8"))
    return text.splitlines()


def _diff(item: planner.Item) -> list[str]:
    first, *others = item.copies
    lines = []
    for other in others:
        lines += list(difflib.unified_diff(_core_text(first, item.kind), _core_text(other, item.kind),
                                           fromfile=f"{'/'.join(first.labels)} {short(first.path)}",
                                           tofile=f"{'/'.join(other.labels)} {short(other.path)}", lineterm=""))
        if item.kind == "skills" and first.obj and other.obj:
            files = lambda copy: {p.relative_to(copy.obj.real).as_posix() for p in Path(copy.obj.real).rglob("*")
                                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(copy.obj.real).parts)}
            only_first, only_other = files(first) - files(other), files(other) - files(first)
            if only_first or only_other:
                lines.append(f"files only in {'/'.join(first.labels)}: {', '.join(sorted(only_first)) or 'none'}; "
                             f"only in {'/'.join(other.labels)}: {', '.join(sorted(only_other)) or 'none'}")
    return lines


def _status(args: argparse.Namespace) -> int:
    saved = settings.load()
    if not saved.enabled:
        _print({"enabled": False, "message": NOT_ENABLED} if args.json else NOT_ENABLED)
        return 0
    plan = _plan(args, state.load(), saved)
    pending = [item for item in plan.items if item.decision and item.decision.action == engine.PENDING_DELETE]
    in_sync = [item for item in plan.items if item.decision and item.decision.action == engine.NOTHING and item.decision.version]
    code = 1 if plan.conflicts or pending else 0
    if args.json:
        _print({"enabled": True, "folders": [{"label": p.label, "cli": p.cli, "path": str(p.folder)} for p in plan.included],
                "in_sync": len(in_sync), "to_write": len(plan.changes), "conflicts": [i.name for i in plan.conflicts],
                "waiting": [i.name for i in plan.waiting], "pending_delete": [i.name for i in pending],
                "not_shared": len(plan.excluded), "notes": plan.notices,
                "diffs": {item.name: _diff(item) for item in plan.conflicts} if args.diff else None})
        return code
    lines = ["agt share is on for " + ", ".join(f"{p.label} ({short(p.folder)})" for p in plan.included) + ".",
             f"  {len(in_sync)} items in sync, {len(plan.changes)} changes for the next sync, "
             f"{len(plan.conflicts)} conflicts, {len(plan.waiting)} waiting, {len(pending)} waiting to be deleted, "
             f"{len(plan.excluded)} not shared."]
    for item in plan.conflicts:
        lines.append(f"  Conflict: {item.name} ({item.kind}) differs in {', '.join('/'.join(c.labels) for c in item.copies)}.")
        if args.diff:
            lines += [f"    {line}" for line in _diff(item)]
    for item in pending:
        lines.append(f"  Waiting to be deleted: {item.name}: {item.decision.reason}.")
    for item in plan.waiting:
        lines.append(f"  Waiting: {item.name}: {item.decision.reason}.")
    lines += [f"  Note: {note}" for note in plan.notices]
    if plan.changes:
        lines.append("  `agt share sync` writes the changes.")
    print("\n".join(lines))
    return code


# Item commands -----------------------------------------------------------------------


def _interactive() -> bool:
    """A person at a terminal. An agent never gets a prompt, even with a terminal (decision 3)."""

    try:
        return sys.stdin.isatty() and sys.stdout.isatty() and hosts.detect_host() == "unknown"
    except (AttributeError, ValueError):
        return False


def _enabled() -> settings.Settings | None:
    saved = settings.load()
    return saved if saved.enabled else None


def _label_list(values: list[str], labels: set[str], flag: str) -> list[str]:
    found = [label.strip() for value in values for label in value.split(",") if label.strip()]
    for label in found:
        _check_label(label, labels, flag)
    return list(dict.fromkeys(found))


def _locked_plan(args: argparse.Namespace, saved: settings.Settings, current: state.State, keys: set[str],
                 **kwargs: Any) -> planner.Plan:
    """The plan for these items, after waiting out an edit saved less than 3 s ago (like `sync`)."""

    plan = _plan(args, current, saved, **kwargs).restricted(keys)
    if any(item.decision.reason == "saved less than 3 s ago" for item in plan.waiting):
        time.sleep(planner.FRESH_SECONDS)
        plan = _plan(args, current, saved, **kwargs).restricted(keys)
    return plan


def _not_shared(plan: planner.Plan, key: str) -> str | None:
    entry = next((entry for entry in plan.excluded if _identity(f"{entry.kind}/{entry.name}") == _identity(key)), None)
    return entry.reason if entry else None


def _existing(plan: planner.Plan, name: str) -> str:
    """The key of an item agt can see: shared, or left out with a reason."""

    key = _item_key(plan, name)
    if _find(plan, key) is None and _not_shared(plan, key) is None:
        raise ValueError(f"no agent or skill named {name} in the folders sharing")
    return key


def _show_picks(plan: planner.Plan, key: str | None, as_json: bool) -> int:
    """`pick` with no flags: what goes where, read-only. An agent gets `needs_choice` to ask the user."""

    if key is None:
        shown = sorted(plan.items, key=lambda item: (not any(c.state == planner.WAITING for c in item.cells.values()),
                                                     planner.KINDS.index(item.kind), item.name))
    else:
        item = _find(plan, key)
        if item is None:
            print(f"{key.partition('/')[2]} isn't shared: {_not_shared(plan, key)}. "
                  f"`agt share pick {key.partition('/')[2]} --in <label>` shares it again.")
            return 0
        shown = [item]
    retry = f"agt share pick {key.partition('/')[2] if key else '<item>'} --in <label> | --not-in <label> | --only <label>[,<label>]"
    if as_json:
        _print({"error": "needs_choice", "ask": "Which CLIs should get it?", "labels": [p.label for p in plan.included],
                "choices": [{"kind": item.kind, "name": item.name,
                             "cells": {label: {"state": cell.state, "note": cell.note} for label, cell in item.cells.items()}}
                            for item in shown], "retry": retry})
        return 3
    lines = ["agt share pick: what goes where. Nothing was changed."] + planner.table(plan, shown)
    lines += ["", f"To change it: {retry}",
              "  --not-in removes the copy agt made there, with a backup; add --keep-copies to keep it as your own file."]
    print("\n".join(lines))
    return 0 if _interactive() else 3


def _pick(args: argparse.Namespace) -> int:
    saved = _enabled()
    if saved is None:
        _print({"enabled": False, "message": NOT_ENABLED} if args.json else NOT_ENABLED)
        return 0
    plan = _plan(args, state.load(), saved)
    labels = {provider.label for provider in plan.included}
    to_in = _label_list(args.in_labels, labels, "--in")
    to_out = _label_list(args.not_in, labels, "--not-in")
    only = _label_list(args.only_in, labels, "--only") if args.only_in else None
    if not args.item:
        if to_in or to_out or only:
            raise ValueError("name the item: agt share pick <item> --in <label>")
        return _show_picks(plan, None, args.json)
    key = _existing(plan, args.item)
    name = key.partition("/")[2]
    if not (to_in or to_out or only):
        return _show_picks(plan, key, args.json)
    both = sorted(set(to_in) & set(to_out))
    if both:
        raise ValueError(f"{', '.join(both)} is in both --in and --not-in")
    item = _find(plan, key)
    if item is None and not (to_in or only):
        print(f"{name} isn't shared ({_not_shared(plan, key)}), so there's nothing to take out. "
              f"`agt share pick {name} --in <label>` shares it again.")
        return 0
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    not_in = set(saved.not_in.get(key, []))
    chosen = sorted(only) if only is not None else saved.only.get(key)
    if item is not None and item.decision and item.decision.reason == planner.NEW_WAITING and chosen is None and not to_out:
        # A new item waiting for targets (auto copy off): it goes where it is now, plus where you pick.
        chosen = sorted({label for copy in item.copies for label in copy.labels})
    for label in to_in:
        not_in.discard(label)
        if chosen is not None and label not in chosen:
            chosen = sorted([*chosen, label])
    for label in to_out:
        not_in.add(label)
        if chosen is not None and label in chosen:
            chosen = [entry for entry in chosen if entry != label]
    if chosen is not None and not chosen:
        raise ValueError(f"that leaves {name} in no CLI; `agt share rm {name}` removes it everywhere")
    if only is not None:
        not_in -= set(only)
    saved.not_in.pop(key, None)
    saved.only.pop(key, None)
    if not_in:
        saved.not_in[key] = sorted(not_in)
    if chosen is not None:
        saved.only[key] = sorted(chosen)
    if to_in or only:
        saved.off.discard(key)
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        record = current.items.get(_identity(key))
        if record:
            # A CLI picked again gets the item again, whatever stopped it there.
            back = set(to_in) | set(only or [])
            for field_name in ("stopped", "released"):
                left = [label for label in record.get(field_name) or [] if label not in back]
                if left:
                    record[field_name] = left
                else:
                    record.pop(field_name, None)
        saved_at = settings.save(saved)
        plan = _locked_plan(args, saved, current, {_identity(key)}, keep_copies=args.keep_copies)
        result = apply.apply(plan, current, op="pick")
        result.replayed = replayed
    undo = [f"`agt share undo {name} --in {label}` brings a removed copy back."
            for label in sorted({label for outcome in result.written if outcome.action == "remove" for label in outcome.labels})]
    _report(plan, result, saved_at, args.json, title="agt share pick", notes=undo)
    return 1 if result.failed else 0


def _ask_version(item: planner.Item) -> str | None:
    """In a terminal: show the difference and ask which copy wins. None means quit."""

    latest = planner.newest(item)
    print(f"{item.name} ({item.kind}) differs between its copies:")
    for number, copy in enumerate(item.copies, 1):
        print(f"  {number}  {'/'.join(copy.labels):<20} {short(copy.path)}{' (newer)' if copy is latest else ''}")
    for line in _diff(item):
        print(f"    {line}")
    numbers = " / ".join(str(number) for number in range(1, len(item.copies) + 1))
    while True:
        try:
            answer = input(f"Which version should every copy get? {numbers}, s to stop sharing it, q to quit: ").strip().lower()
        except EOFError:
            return None
        if answer == "q":
            return None
        if answer == "s":
            return "skip"
        if answer.isdigit() and 1 <= int(answer) <= len(item.copies):
            return item.copies[int(answer) - 1].labels[0]


def _resolve_needs_choice(items: list[planner.Item], as_json: bool) -> int:
    asks = []
    for item in items:
        latest = planner.newest(item)
        choices = [{"value": copy.labels[0], "labels": copy.labels, "path": str(copy.path), "newer": copy is latest}
                   for copy in item.copies]
        choices.append({"value": "skip", "means": "don't share it; every copy stays as it is"})
        asks.append({"id": f"resolve:{item.name}", "choices": choices,
                     "ask": f"{item.name} differs between {', '.join(c['value'] for c in choices[:-1])}. Which version should every copy get?",
                     "retry": f"agt share resolve {item.name} --prefer <choice>"})
    if as_json:
        _print({"error": "needs_choice", "ask": [ask["ask"] for ask in asks], "choices": asks,
                "retry": asks[0]["retry"] if len(asks) == 1 else "agt share resolve <item> --prefer <choice>"})
    else:
        print("Nothing was changed: agt needs to know which version wins.")
        for ask in asks:
            options = ", ".join(choice["value"] + (" (newer)" if choice.get("newer") else "") for choice in ask["choices"])
            print(f"  {ask['ask']} Choices: {options}. Answer with {ask['retry']}.")
    return 3


def _resolve(args: argparse.Namespace) -> int:
    saved = _enabled()
    if saved is None:
        _print({"enabled": False, "message": NOT_ENABLED} if args.json else NOT_ENABLED)
        return 0
    plan = _plan(args, state.load(), saved)
    if args.all == bool(args.item):
        raise ValueError("name one item to resolve, or --all")
    if args.all:
        targets = [item for item in plan.conflicts if not args.kind or item.kind == args.kind]
        if not targets:
            print("agt share resolve: no conflicts to resolve.")
            return 0
    else:
        key = _existing(plan, args.item)
        item = _find(plan, key)
        if item is None:
            print(f"{key.partition('/')[2]} isn't shared ({_not_shared(plan, key)}), so it has nothing to resolve. "
                  f"`agt share pick {key.partition('/')[2]} --in <label>` shares it again.")
            return 0
        if item not in plan.conflicts:
            print(f"agt share resolve: {item.name} has no conflict; its copies agree.")
            return 0
        targets = [item]
    choices: dict[str, str] = {}
    skipped: list[str] = []
    for item in targets:
        if args.prefer == "newest":
            choice = planner.newest(item).labels[0]
        elif args.prefer:
            choice = args.prefer
            if choice != "skip" and not any(choice in copy.labels for copy in item.copies):
                if args.all:
                    skipped.append(f"{item.name}: {choice} has no copy, so it's left as it is")
                    continue
                where = ", ".join("/".join(copy.labels) for copy in item.copies)
                raise ValueError(f"{choice} has no copy of {item.name}; its copies are in {where}")
        elif _interactive():
            choice = _ask_version(item)
            if choice is None:
                print("Nothing was changed.")
                return 0
        else:
            return _resolve_needs_choice(targets, args.json)
        choices[item.key] = choice
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    by_key = {item.key: item for item in targets}
    winners = {by_key[key].name: label for key, label in choices.items() if label != "skip"}
    off = [f"{by_key[key].kind}/{by_key[key].name}" for key, label in choices.items() if label == "skip"]
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        saved_at = None
        if off:
            saved.off |= set(off)
            saved_at = settings.save(saved)
        plan = _locked_plan(args, saved, current, set(choices), resolve=winners)
        result = apply.apply(plan, current, op="resolve")
        result.replayed = replayed
    notes = skipped + [f"{key.partition('/')[2]}: not shared any more; every copy stays as it is" for key in off]
    _report(plan, result, saved_at, args.json, title="agt share resolve", notes=notes)
    return 1 if result.failed or plan.conflicts else 0


def _rm(args: argparse.Namespace) -> int:
    saved = _enabled()
    if saved is None:
        _print({"enabled": False, "message": NOT_ENABLED} if args.json else NOT_ENABLED)
        return 0
    plan = _plan(args, state.load(), saved)
    key = _existing(plan, args.item)
    name = key.partition("/")[2]
    if _find(plan, key) is None:
        print(f"{name} isn't shared ({_not_shared(plan, key)}), so agt leaves its copies alone. "
              f"Delete them by hand, or share it again first with `agt share pick {name} --in <label>`.")
        return 1
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        plan = _locked_plan(args, saved, current, {_identity(key)})
        item = plan.items[0] if plan.items else None
        if item is None:
            print(f"agt share rm: {name} is already gone.")
            return 0
        last = current.last(item.kind, names.identity(item.name))
        edited = sorted(label for copy in item.copies for label in copy.labels
                        if copy.version and (last is None or last.copies.get(label) != copy.version))
        item.decision = engine.Decision("remove", reason="agt share rm")
        plan.changes = planner.removal_changes(item)
        record = dict(current.items.get(item.key) or {})
        result = apply.apply(plan, current, op="rm", records={item.key: record} if record else None)
        result.replayed = replayed
        if not result.failed:
            current.items.pop(item.key, None)  # gone everywhere; the manifest keeps the record for undo
            state.save(current)
    notes = [f"{name} in {', '.join(edited)} had an edit the other copies didn't have; it's in the backups too"] if edited and last else []
    if result.written:
        notes.append(f"`agt share undo {name}` puts it back.")
    _report(plan, result, None, args.json, title="agt share rm", notes=notes)
    return 1 if result.failed else 0


def _undo_entries(key: str | None, bare: str, label: str | None) -> tuple[dict | None, list[dict], str | None]:
    """The newest run that left a backup of the item (with `label`, of a copy it removed there).

    Returns (that run's manifest, its entries for the item, the item's key).
    """

    wanted = names.identity(bare)
    for manifest in apply.manifests():
        entries = []
        for change in manifest["changes"]:
            kind, _, name = str(change.get("key", "")).partition("/")
            if names.identity(name) != wanted or (key and kind != key.partition("/")[0]) or not change.get("backup"):
                continue
            if label and (change.get("action") != "remove" or label not in change.get("labels", [])):
                continue
            entries.append(change)
        if entries:
            return manifest, entries, f"{entries[0]['key'].partition('/')[0]}/{entries[0].get('name') or bare}"
    return None, [], key


def _left_out_in(saved: settings.Settings, key: str, labels: set[str]) -> set[str]:
    """The CLIs your picks leave this item out of."""

    found = set(saved.not_in.get(key, []))
    if key in saved.only:
        found |= labels - set(saved.only[key])
    return found


def _undo(args: argparse.Namespace) -> int:
    saved = _enabled()
    if saved is None:
        _print({"enabled": False, "message": NOT_ENABLED} if args.json else NOT_ENABLED)
        return 0
    plan = _plan(args, state.load(), saved)
    labels = {provider.label for provider in plan.included}
    label = args.in_label
    if label:
        _check_label(label, labels, "--in")
    try:
        key = _item_key(plan, args.item)
    except ValueError:
        key = None  # gone from every folder after `rm`: the backups still know it
    bare = args.item.partition("/")[2] if "/" in args.item else args.item
    manifest, entries, key = _undo_entries(key, bare, label)
    if key is None:
        raise ValueError(f"agt has no agent or skill named {args.item} and no backup of one")
    name = key.partition("/")[2]
    if label and manifest and manifest.get("op") == "rm":
        # One copy back after `rm` would leave the others looking deleted; rm's undo is all or nothing.
        print(f"agt share undo: `agt share rm` removed {name} from every CLI, so it comes back everywhere or not at all: "
              f"`agt share undo {name}`.")
        return 0
    record = state.load().items.get(_identity(key)) or {}
    held = bool(label) and label in (set(record.get("stopped") or []) | set(record.get("released") or [])
                                     | _left_out_in(saved, key, labels))
    if manifest is None and not held:
        if label:
            print(f"agt share undo: nothing to bring back; {name} wasn't removed from {label}. "
                  f"`agt share pick {name} --in {label}` shares it there.")
        else:
            print(f"agt share undo: nothing to put back; agt has no backup from a change to {name} "
                  f"(a run that only made new copies leaves none). `agt share pick {name} --not-in <label>` removes a copy.")
        return 0
    if apply.in_worker():
        print(IN_WORKER)
        return 2
    included = _folders(load_config(), None, False, saved)[1]
    with apply.lock([provider.folder for provider in included]):
        current = state.load()
        replayed = apply.replay(current)
        run = apply.new_run_id()
        restored = [apply.restore(entry, run) for entry in entries]
        came_back = [(outcome, entry) for outcome, entry in zip(restored, entries) if outcome.ok and entry["action"] == "remove"]
        back = {entry_label for _, entry in came_back for entry_label in entry["labels"]} | ({label} if label else set())
        dropped = (manifest or {}).get("records", {}).get(_identity(key)) if (manifest or {}).get("op") == "rm" else None
        if dropped and _identity(key) not in current.items:
            current.items[_identity(key)] = dict(dropped)  # undoing rm: the item is back as it was
        record = current.items.get(_identity(key))
        if record is not None:
            if manifest and manifest.get("op") == "resolve":
                # Undoing a resolve brings the conflict back: the copies differ the way they did.
                record.pop("version", None)
                record.pop("copies", None)
            for field_name in ("stopped", "released"):
                left = [entry_label for entry_label in record.get(field_name) or [] if entry_label not in back]
                if left:
                    record[field_name] = left
                else:
                    record.pop(field_name, None)
            if manifest and manifest.get("op") == "pick":
                # A copy a pick removed comes back as agt's copy, at the version it had, so later edits reach it.
                for outcome, entry in came_back:
                    record["made"] = sorted(set(record.get("made") or []) | set(entry["labels"]))
                    version = _version_at(key.partition("/")[0], included, entry["labels"][0], outcome.path)
                    if version:
                        record.setdefault("copies", {}).update({entry_label: version for entry_label in entry["labels"]})
        saved_at = None
        if back & _left_out_in(saved, key, labels):
            not_in = [entry_label for entry_label in saved.not_in.get(key, []) if entry_label not in back]
            saved.not_in.pop(key, None)
            if not_in:
                saved.not_in[key] = not_in
            if key in saved.only:
                saved.only[key] = sorted(set(saved.only[key]) | back)
            saved_at = settings.save(saved)
        own = frozenset(os.path.realpath(outcome.path) for outcome in restored if outcome.ok)
        plan = _plan(args, current, saved, own=own).restricted({_identity(key)})
        result = apply.apply(plan, current, op="undo", run=run, prior=[outcome for outcome in restored if outcome.ok])
        result.outcomes += [outcome for outcome in restored if not outcome.ok]
        result.replayed = replayed
        # Copies agt made that came back go in their repo's exclude file again; your own files never do.
        made = set((current.items.get(_identity(key)) or {}).get("made") or [])
        result.excluded += gitexclude.update([outcome.path for outcome, entry in came_back if set(entry["labels"]) & made])
    _report(plan, result, saved_at, args.json, title="agt share undo",
            notes=[f"`agt share undo {name}` again puts back what this undo replaced."]
            if any(outcome.backup for outcome in result.written) else [])
    return 1 if result.failed else 0


def _version_at(kind: str, included: list[providers.Provider], label: str, path: Path) -> str | None:
    cli = next((provider.cli for provider in included if provider.label == label), None)
    try:
        if kind == "agents":
            return model.read_agent(cli, path).digest if cli else None
        return model.read_skill(path).digest
    except model.ItemError:
        return None


def command(args: argparse.Namespace) -> int:
    action = args.share_command
    try:
        if action == "enable":
            return _enable(args)
        if action == "sync":
            return _sync(args)
        if action == "status":
            return _status(args)
        if action in ("pick", "resolve", "rm", "undo"):
            return {"pick": _pick, "resolve": _resolve, "rm": _rm, "undo": _undo}[action](args)
    except (settings.SettingsError, state.StateError, apply.Busy) as exc:
        print(f"agt share: {exc}", file=sys.stderr)
        return 1
    if action == "scan":
        plan = _plan(argparse.Namespace(**{**vars(args), "skip": [], "only": [], "resolve": [],
                                           "tool_widening": "allow-if-shell", "auto_copy": "on"}), state.load(), None)
        proposal = planner.proposal(plan, "")
        if args.json:
            _print({"kinds": [{"kind": kind, "suggested": kind in SUPPORTED_KINDS, "available": True} for kind in SUPPORTED_KINDS]
                    + [{"kind": kind, "suggested": False, "available": False} for kind in LATER_KINDS],
                    **{key: proposal[key] for key in ("folders", "items", "needs_answer", "excluded")}})
        else:
            print(planner.render_text(plan, "agt share enable --dry-run"))
        return 0
    raise ValueError(f"unknown share command {action}")
