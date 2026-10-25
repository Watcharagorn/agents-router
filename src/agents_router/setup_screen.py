"""`agt setup`'s one screen: the plugin install and agt share's setup, suggested together and applied with one answer.

In a terminal, `agt setup` shows everything it suggests, already ticked: the
hosts the plugin goes into, the CLI folders that would share and what goes
where, the repos that have their own agents or skills, and a line each for
worktrees joining by themselves, auto copy, tool widening, delegation rules
and Codex's session line. Nothing is written until you press Enter; `p`
changes the share picks, the repos and the lines, and `q` quits. A conflict
(an item whose copies differ) is asked first. "Still needed from you" comes
last, once.

Without a terminal, plain `agt setup` installs the plugin as before and says
how to set up sharing. An agent runs `agt setup --dry-run --json`, which
returns the same screen as a proposal (the plugin lines, the share table,
`repos`, `switches`, `needs_answer` and `command`), asks the user once, and
runs that `command` (`agt setup --yes --if-plan <id> ...`). `--if-plan`
refuses when anything changed since the dry run. `--share off` leaves
sharing out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
from typing import Any

from agents_router import rules, setup
from agents_router.config import load_config
from agents_router.share import commands as share, picker, scope, settings, state
from agents_router.share import plan as planner
from agents_router.share.providers import _short as short

SWITCHES = {
    "worktrees": ("Worktrees of a listed repo share by themselves", True),
    "auto_copy": ("New items go to every CLI (auto copy)", True),
    "widen": ("Codex agents get every tool (Codex can't limit an agent's tools)", True),
    "rules": ("Delegation rules: which models to use, in each Claude folder and Codex's AGENTS.md", False),
    "codex_session": ("Codex's session line (Codex asks you to trust it once in /hooks)", False),
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="With --dry-run: the screen as JSON, for an agent.")
    parser.add_argument("--yes", action="store_true", help="Apply the plan: the plugin, sharing and the lines you set.")
    parser.add_argument("--if-plan", metavar="ID", help="Apply only if the plan is still the one a dry run showed.")
    parser.add_argument("--share", choices=("on", "off"), default="on", help="Set up agt share too (on by default).")
    parser.add_argument("--projects", default=None, metavar="none|all-with-items|PATH[,PATH]",
                        help="Repos that share their own agents and skills (default: every repo your CLIs know that has some).")
    parser.add_argument("--rules", choices=("on", "off"), help="Install the delegation rules (off by default).")
    parser.add_argument("--codex-session-line", choices=("on", "off"), help="agt's session entry in Codex (off by default).")
    parser.add_argument("--worktrees", choices=settings.WORKTREES, help="How a listed repo's worktrees join (auto by default).")
    parser.add_argument("--resolve", action="append", default=[], metavar="ITEM=LABEL|skip")
    parser.add_argument("--skip", action="append", default=[], metavar="ITEM@LABEL")
    parser.add_argument("--only", action="append", default=[], metavar="ITEM=LABEL[,LABEL]")
    parser.add_argument("--auto-copy", help="on, off or a list of kinds.")
    parser.add_argument("--tool-widening", choices=("allow-if-shell", "skip"))


def _share_args(args: argparse.Namespace) -> argparse.Namespace:
    """The `agt share enable` arguments this setup run stands for."""

    return argparse.Namespace(
        claude_dir=[], codex_dir=[], kiro_dir=[], agy_dir=[], no_agy=False, kinds=None, json=args.json,
        projects=args.projects if args.projects is not None else "all-with-items", only=list(args.only), skip=list(args.skip),
        resolve=list(args.resolve), auto_copy=args.auto_copy, tool_widening=args.tool_widening, instructions=None,
        dry_run=False, yes=True, if_plan=None, accept_suggested=False)


def _switches(args: argparse.Namespace, saved: settings.Settings, plan: planner.Plan) -> dict[str, bool]:
    return {
        "worktrees": (args.worktrees or saved.worktrees) == "auto",
        "auto_copy": bool(plan.options.auto_copy),
        "widen": plan.options.tool_widening == "allow-if-shell",
        "rules": args.rules == "on",
        "codex_session": args.codex_session_line == "on" if args.codex_session_line else saved.codex_session,
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    """Everything the screen shows, worked out without writing anything."""

    config = load_config()
    code, report = setup.run_setup(config, args.host or ["auto"], args.account, dry_run=True, claude_route=args.claude_route)
    screen: dict[str, Any] = {"setup": report, "setup_code": code}
    if args.share == "off":
        return screen
    saved = settings.load()
    sargs = _share_args(args)
    plan = share._plan(sargs, state.load(), saved)
    repos = share._project_choice(sargs, saved)
    screen.update(saved=saved, sargs=sargs, plan=plan, repos=repos, switches=_switches(args, saved, plan))
    content = {"share": plan.digest, "repos": sorted(str(root) for root in repos), "switches": screen["switches"],
               "setup": sorted(report.done)}
    screen["digest"] = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:12]
    return screen


def command_line(args: argparse.Namespace, screen: dict[str, Any]) -> str:
    parts = ["agt", "setup", "--yes"]
    if "digest" in screen:
        parts += ["--if-plan", screen["digest"]]
    for host in args.host or []:
        parts += ["--host", host]
    for account in args.account:
        parts += ["--account", account]
    if args.share == "off":
        parts += ["--share", "off"]
    else:
        switches = screen["switches"]
        parts += ["--rules", "on" if switches["rules"] else "off",
                  "--codex-session-line", "on" if switches["codex_session"] else "off",
                  "--worktrees", "auto" if switches["worktrees"] else "command",
                  "--auto-copy", "on" if switches["auto_copy"] else "off",
                  "--tool-widening", "allow-if-shell" if switches["widen"] else "skip",
                  "--projects", ",".join(str(root) for root in screen["repos"]) or "none"]
        for flag, values in (("--resolve", args.resolve), ("--skip", args.skip), ("--only", args.only)):
            for value in values:
                parts += [flag, value]
    return " ".join(shlex.quote(part) for part in parts)


def proposal(args: argparse.Namespace, screen: dict[str, Any]) -> dict[str, Any]:
    report = screen["setup"]
    data: dict[str, Any] = {"dry_run": True, "plugin": {"planned": report.done, "already": report.unchanged,
                                                        "failed": report.failed, "notes": report.notes}}
    if "plan" in screen:
        share_proposal = planner.proposal(screen["plan"], "")
        data.update(plan=screen["digest"], share=share_proposal, repos=[str(root) for root in screen["repos"]],
                    switches={name: {"on": value, "means": SWITCHES[name][0]} for name, value in screen["switches"].items()},
                    needs_answer=share_proposal["needs_answer"])
    data.update(still_needed=report.todo, command=command_line(args, screen))
    return data


def render(args: argparse.Namespace, screen: dict[str, Any], *, dry_run: bool = False) -> str:
    report = screen["setup"]
    header = "agt setup (dry run): what it would do. Nothing was written." if dry_run else \
        "agt setup: what it would do. Nothing is written until you press Enter."
    lines = [header, "", "Plugin:"]
    lines += [f"  + {item}" for item in report.done] + [f"  · {item}" for item in report.unchanged]
    lines += [f"  ! {item}" for item in report.failed]
    if "plan" in screen:
        text = planner.render_text(screen["plan"], "", header="Sharing agents and skills between your CLIs:")
        lines += ["", *text.splitlines()]
        repos = screen["repos"]
        lines += ["", "Repos that share their own agents and skills:" if repos else
                  "Repos: none of the repos your CLIs know has its own agents or skills yet."]
        lines += [f"  [x] {short(root)}" for root in repos]
        lines += ["", "Lines:"] + [f"  [{'x' if on else ' '}] {SWITCHES[name][0]}" for name, on in screen["switches"].items()]
    return "\n".join(lines)


def apply(args: argparse.Namespace, screen: dict[str, Any]) -> int:
    """Do all of it: the plugin, then sharing (with its repos), then the lines that aren't share settings."""

    config = load_config()
    code, report = setup.run_setup(config, args.host or ["auto"], args.account, dry_run=False, claude_route=args.claude_route)
    todo, report.todo = report.todo, []
    print(setup.format_report(report).partition("\nNothing left for you to do.")[0].rstrip())
    if "plan" not in screen:
        _still_needed(todo)
        return code
    switches, saved = screen["switches"], screen["saved"]
    sargs = screen["sargs"]
    sargs.projects = ",".join(str(root) for root in screen["repos"]) or "none"
    sargs.auto_copy = "on" if switches["auto_copy"] else "off"
    sargs.tool_widening = "allow-if-shell" if switches["widen"] else "skip"
    saved.worktrees = "auto" if switches["worktrees"] else "command"
    saved.codex_session = switches["codex_session"]
    print()
    share_code = share._enable_write(sargs, saved) or 0
    if switches["rules"]:
        for target in ("claude", "codex"):
            path, _ = rules.target_path(target)
            if path.parent.is_dir():
                print(rules.update_file(path, rules.render(_rules_args(target))))
    _still_needed(todo)
    return max(code, share_code)


def _still_needed(todo: list[str]) -> None:
    """Last, once: what's left for you (logins and restarts), whatever came before it."""

    lines = ["", "Still needed from you:", *(f"  {n}. {item}" for n, item in enumerate(todo, 1))] if todo else \
        ["", "Nothing left for you to do."]
    print("\n".join([*lines, "", "Check everything with `agt doctor` (add --runtime codex for Codex accounts)."]))


def _rules_args(target: str) -> argparse.Namespace:
    values = {f"home_{tier}": None for tier in rules.TIERS} | {f"try_{tier}": "none" for tier in rules.TIERS}
    return argparse.Namespace(target=target, work_folder=[], main_limit=4, helper_limit=3, **values)


def edit(args: argparse.Namespace, screen: dict[str, Any]) -> None:
    """`p` on the screen: the share lists, then the repos, then the lines."""

    share._enable_lists(screen["sargs"], screen["saved"], screen["plan"])
    if screen["repos"]:
        repos = picker.ListState("Which repos share their own agents and skills?",
                                 [picker.Choice(str(root), short(root), True) for root in screen["repos"]], "repos")
        if picker.choose(repos) != picker.QUIT:
            args.projects = ",".join(sorted(repos.checked())) or "none"
    lines = picker.ListState("Lines", [picker.Choice(name, SWITCHES[name][0], on) for name, on in screen["switches"].items()],
                             "lines")
    if picker.choose(lines) == picker.QUIT:
        return
    checked = lines.checked()
    args.rules = "on" if "rules" in checked else "off"
    args.codex_session_line = "on" if "codex_session" in checked else "off"
    args.worktrees = "auto" if "worktrees" in checked else "command"
    args.auto_copy = "on" if "auto_copy" in checked else "off"
    args.tool_widening = "allow-if-shell" if "widen" in checked else "skip"
    args.resolve, args.skip, args.only = screen["sargs"].resolve, screen["sargs"].skip, screen["sargs"].only


def run(args: argparse.Namespace) -> int:
    """`agt setup`: the screen in a terminal, the proposal with --dry-run --json, the plan with --yes."""

    if args.dry_run:
        screen = build(args)
        report = screen["setup"]
        todo = ["", "Still needed from you after it:"] + [f"  {n}. {item}" for n, item in enumerate(report.todo, 1)] \
            if report.todo else []
        print(json.dumps(proposal(args, screen), indent=2, ensure_ascii=False) if args.json else
              "\n".join([render(args, screen, dry_run=True), *todo, "", "To apply it:", "  " + command_line(args, screen)]))
        return 0
    if args.yes:
        screen = build(args)
        if args.if_plan and "digest" in screen and args.if_plan != screen["digest"]:
            print(f"Nothing was written: the plan changed since your dry run (it was {args.if_plan}, now {screen['digest']}). "
                  "Run `agt setup --dry-run` again.")
            return 1
        plan = screen.get("plan")
        if plan is not None and plan.conflicts:  # asked before anything is written, the plugin included
            return share._needs_choice(plan, command_line(args, screen), args.json)
        return apply(args, screen)
    if not share._interactive():
        return -1  # the plain, non-interactive setup
    while True:
        screen = build(args)
        plan = screen.get("plan")
        answered = {value.partition("=")[0] for value in args.resolve}
        open_conflicts = [item for item in (plan.conflicts if plan else []) if item.name not in answered]
        if open_conflicts:
            for item in open_conflicts:
                choice = share._ask_version(item)
                if choice is None:
                    print("Nothing was written.")
                    return 0
                args.resolve.append(f"{item.name}={choice}")
            continue
        print(render(args, screen))
        try:
            answer = input("\nEnter does all of it, p changes it, q quits: ").strip().lower()
        except EOFError:
            answer = "q"
        if answer == "q":
            print("Nothing was written.")
            return 0
        if answer == "p" and plan is not None:
            edit(args, screen)
            continue
        if not answer:
            return apply(args, screen)


def scopes_note() -> str:
    """For plain `agt setup` without a terminal: how to set up sharing too."""

    listed = len(scope.listed())
    return ("Sharing agents and skills between your CLIs: run `agt setup` in a terminal, or `agt setup --dry-run --json` "
            "from an agent" + (f" ({listed} repos share now)" if listed else "") + ".")

