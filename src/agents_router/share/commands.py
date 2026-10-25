"""`agt share` commands.

This build only reads: `enable --dry-run` shows what sharing would do,
`scan` lists every choice setup would offer, and `sync --dry-run` shows the
next sync. Nothing here writes to a CLI folder yet.
"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any

from agents_router.config import load_config
from agents_router.share import plan as planner, providers, state, toolmap

SUPPORTED_KINDS = ("agents", "skills")
LATER_KINDS = ("instructions", "mcp", "commands", "hooks", "plugins")
DRY_RUN_ONLY = ("agt share only does dry runs in this build: add --dry-run to see what it would do. "
                "Writing comes once you've reviewed a dry run on your own folders.")


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
        description="agt share keeps your agents and skills the same in every CLI folder you pick. "
        "This build only shows dry runs; nothing is written.",
    )
    actions = share.add_subparsers(dest="share_command", required=True)
    enable = actions.add_parser("enable", help="Show what sharing would copy, for every folder found (needs --dry-run).")
    _folder_flags(enable)
    enable.add_argument("--projects", default="none", help="Repos to share too: none (project scope comes later).")
    enable.add_argument("--only", action="append", default=[], metavar="ITEM=LABEL[,LABEL]", help="Keep an item in these CLIs only.")
    enable.add_argument("--skip", action="append", default=[], metavar="ITEM@LABEL", help="Untick one cell (repeatable).")
    enable.add_argument("--resolve", action="append", default=[], metavar="ITEM=LABEL|skip",
                        help="Which version wins for an item that differs between folders.")
    enable.add_argument("--auto-copy", default="on", help="New items go to every target: on, off or a list of kinds.")
    enable.add_argument("--tool-widening", choices=toolmap.TOOL_WIDENING, default="allow-if-shell",
                        help="Codex agents can't limit tools: allow-if-shell copies agents that have a shell, skip copies none with a tool list.")
    enable.add_argument("--dry-run", action="store_true", help="Show the plan; write nothing.")
    enable.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    enable.add_argument("--if-plan", metavar="ID", help=argparse.SUPPRESS)
    enable.add_argument("--accept-suggested", action="store_true", help=argparse.SUPPRESS)
    scan = actions.add_parser("scan", help="List every choice setup would offer; writes nothing.")
    _folder_flags(scan)
    sync = actions.add_parser("sync", help="Show the next sync (needs --dry-run).")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--json", action="store_true")


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


def _options(args: argparse.Namespace, labels: set[str]) -> planner.Options:
    options = planner.Options(kinds=_kinds(args.kinds), tool_widening=getattr(args, "tool_widening", "allow-if-shell"))
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
        if label != "skip":
            _check_label(label, labels, "--resolve")
        options.resolve[item] = label
    return options


def _check_label(label: str, labels: set[str], flag: str) -> None:
    if label not in labels:
        raise ValueError(f"{flag}: no folder labelled {label}; the labels are {', '.join(sorted(labels))}")


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


def _plan(args: argparse.Namespace) -> planner.Plan:
    config = load_config()
    current = state.load()
    explicit = _explicit_folders(args)
    rows = providers.candidates(config, explicit, no_agy=args.no_agy)
    if explicit is not None:
        given = {(cli, str(folder.resolve())) for cli, folder in explicit}
        included = [row.provider for row in rows if row.reason is None
                    and (row.provider.cli, str(row.provider.folder.resolve())) in given]
    else:
        included = [row.provider for row in rows if row.suggested]
    options = _options(args, {provider.label for provider in included})
    return planner.build(config, rows, included, options, current)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False) if not isinstance(value, str) else value)


def command(args: argparse.Namespace) -> int:
    action = args.share_command
    if action == "sync":
        if not args.dry_run:
            print(DRY_RUN_ONLY)
            return 2
        if state.load().first_run:
            message = "agt share isn't enabled yet. `agt share enable --dry-run` shows what it would do."
            _print({"enabled": False, "message": message} if args.json else message)
            return 0
        print("A sync of saved share settings comes with writing; `agt share enable --dry-run` shows the plan.")
        return 0
    if action == "enable":
        if args.projects != "none":
            raise ValueError("project scope (--projects) comes in a later phase; use --projects none")
        if not args.dry_run:
            print(DRY_RUN_ONLY)
            return 2
        plan = _plan(args)
        line = _command_line(args, plan.digest)
        _print(planner.proposal(plan, line) if args.json else planner.render_text(plan, line))
        return 0
    if action == "scan":
        plan = _plan(argparse.Namespace(**{**vars(args), "skip": [], "only": [], "resolve": [], "tool_widening": "allow-if-shell"}))
        proposal = planner.proposal(plan, "")
        if args.json:
            _print({"kinds": [{"kind": kind, "suggested": kind in SUPPORTED_KINDS, "available": True} for kind in SUPPORTED_KINDS]
                    + [{"kind": kind, "suggested": False, "available": False} for kind in LATER_KINDS],
                    **{key: proposal[key] for key in ("folders", "items", "needs_answer", "excluded")}})
        else:
            print(planner.render_text(plan, "agt share enable --dry-run"))
        return 0
    raise ValueError(f"unknown share command {action}")
