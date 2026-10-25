"""`agt share` commands.

`enable` shows what sharing would do (`--dry-run`) or saves your answers and
does it (`--yes`, plus `--if-plan` from a dry run). `sync` makes the copies
match again from the saved settings, `status` says where things stand, and
`scan` lists every choice setup would offer. Only `enable --yes` and `sync`
write, and never inside agt's own workers.
"""

from __future__ import annotations

import argparse
import difflib
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from agents_router.config import load_config
from agents_router.share import apply, engine, normalize, plan as planner, providers, settings, state, toolmap
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


def _options(args: argparse.Namespace, labels: set[str], saved: settings.Settings | None) -> planner.Options:
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


def _plan(args: argparse.Namespace, current: state.State, saved: settings.Settings | None) -> planner.Plan:
    config = load_config()
    explicit = _explicit_folders(args) if hasattr(args, "claude_dir") else None
    rows, included = _folders(config, explicit, getattr(args, "no_agy", False), saved)
    options = _options(args, {provider.label for provider in included}, saved)
    return planner.build(config, rows, included, options, current)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False) if not isinstance(value, str) else value)


def _item_key(plan: planner.Plan, name: str) -> str:
    """`<kind>/<name>` for an item named on the command line."""

    if "/" in name:
        return name
    kinds = sorted({item.kind for item in plan.items if item.name == name}
                   | {entry.kind for entry in plan.excluded if entry.name == name})
    if not kinds:
        raise ValueError(f"no agent or skill named {name} in the folders sharing")
    if len(kinds) > 1:
        raise ValueError(f"{name} is both an agent and a skill; say agents/{name} or skills/{name}")
    return f"{kinds[0]}/{name}"


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
    mark = {"copy": "+", "link": "+", "update": "~", "relink": "~"}.get(outcome.action, "?")
    line = f"  {mark} {outcome.kind}/{outcome.name} in {', '.join(outcome.labels)}: {short(outcome.path)}"
    return line if outcome.ok else f"{line} ({outcome.message})"


def _report(plan: planner.Plan, result: apply.Result, saved_at: Path | None, as_json: bool) -> None:
    if as_json:
        _print({
            "run": result.run, "written": [_outcome(o) for o in result.written], "failed": [_outcome(o) for o in result.failed],
            "conflicts": [item.name for item in plan.conflicts], "waiting": [item.name for item in plan.waiting],
            "notes": plan.notices + [note for item in plan.items for note in item.notes], "replayed": result.replayed,
            "settings": str(saved_at) if saved_at else None,
            "git_exclude": [{"file": str(path), "lines": lines} for path, lines in result.excluded],
        })
        return
    count = len(result.written)
    folders = sorted({label for outcome in result.written for label in outcome.labels})
    lines = [f"agt share: wrote {count} change{'s' if count != 1 else ''} in {len(folders)} folder{'s' if len(folders) != 1 else ''}."
             if count else "agt share: everything is in sync; nothing to write."]
    lines += [_outcome_line(outcome) for outcome in result.written]
    if result.failed:
        lines += ["", "Not written (the next sync tries again):"] + [_outcome_line(outcome) for outcome in result.failed]
    if plan.conflicts:
        lines += ["", "Left as they are until you pick a version (`agt share status --diff` shows how they differ):"]
        lines += [f"  {item.name} ({item.kind}): {', '.join('/'.join(copy.labels) for copy in item.copies)}" for item in plan.conflicts]
    if plan.waiting:
        lines += ["", "Waiting:"] + [f"  {item.name}: {item.decision.reason}" for item in plan.waiting]
    notes = result.replayed + plan.notices + [note for item in plan.items for note in item.notes]
    if notes:
        lines += ["", "Notes:"] + [f"  {note}" for note in notes]
    for path, changed in result.excluded:
        lines.append(f"\nListed {len(changed)} of agt's copies in {short(path)}, so that repo's `git status` doesn't show them.")
    if saved_at:
        lines.append(f"\nSaved your answers in {short(saved_at)}.")
    if any(outcome.backup for outcome in result.written):
        lines.append(f"Backups of what was replaced are in {short(state.share_dir() / 'backups' / result.run)}.")
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
        _print(planner.proposal(plan, "agt share sync") if args.json else
               planner.render_text(plan, "agt share sync", header="agt share sync: dry run, nothing was written."))
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


def command(args: argparse.Namespace) -> int:
    action = args.share_command
    try:
        if action == "enable":
            return _enable(args)
        if action == "sync":
            return _sync(args)
        if action == "status":
            return _status(args)
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
