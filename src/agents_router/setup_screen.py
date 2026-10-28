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

from agents_router import rules, setup, ui
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
    "mcp": ("MCP servers: your CLIs share their user MCP servers (one with a secret written into it, or set up for one CLI, stays put)", False),
    "plugins": ("Plugins: your Claude Code and Codex plugins install in each other's folders (one a CLI would load only part of waits for your answer)", False),
}
# What the picker's count calls each line ("MCP servers on").
NAMES = {"worktrees": "worktree sharing", "auto_copy": "auto copy", "widen": "tool widening", "rules": "delegation rules",
         "codex_session": "Codex's session line", "mcp": "MCP servers", "plugins": "plugins"}
# Shorter wording for the screen; SWITCHES keeps the text `--dry-run --json` has always given agents.
LABELS = {
    "auto_copy": "New agents and skills go to every CLI (auto copy)",
    "mcp": "MCP servers: your CLIs share their user MCP servers",
    "plugins": "Plugins: your Claude Code and Codex plugins install in each other's folders",
}


def label(name: str) -> str:
    return LABELS.get(name, SWITCHES[name][0])


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
    parser.add_argument("--mcp", choices=("on", "off"), help="Share your user MCP servers too (off by default).")
    parser.add_argument("--plugins", choices=("on", "off"), help="Install your plugins in your other Claude Code and Codex "
                                                                  "folders too (off by default).")
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
        "mcp": plan.mcp is not None,
        "plugins": plan.plugins is not None,
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
    sargs.mcp = args.mcp or ("on" if saved.mcp else "off")
    sargs.plugins = args.plugins or ("on" if saved.plugins else "off")
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
                  "--mcp", "on" if switches["mcp"] else "off",
                  "--plugins", "on" if switches["plugins"] else "off",
                  "--worktrees", "auto" if switches["worktrees"] else "command",
                  "--auto-copy", "on" if switches["auto_copy"] else "off",
                  "--tool-widening", "allow-if-shell" if switches["widen"] else "skip",
                  "--projects", ",".join(str(root) for root in screen["repos"]) or "none"]
        for flag, values in (("--resolve", args.resolve), ("--skip", args.skip), ("--only", args.only)):
            for value in values:
                parts += [flag, value]
    return " ".join(shlex.quote(part) for part in parts)


def short_command(args: argparse.Namespace, screen: dict[str, Any]) -> str:
    """The apply command a person types: the plan id, which stands for every answer, plus only the flags they
    gave (the same flags give the same id, and a changed plan is refused). `--dry-run --json` keeps the long form."""

    parts = ["agt", "setup", "--yes"]
    if "digest" in screen:
        parts += ["--if-plan", screen["digest"]]
    for host in args.host or []:
        parts += ["--host", host]
    for account in args.account:
        parts += ["--account", account]
    given = (("--share", "off" if args.share == "off" else None), ("--rules", args.rules),
             ("--codex-session-line", args.codex_session_line), ("--worktrees", args.worktrees), ("--mcp", args.mcp),
             ("--plugins", args.plugins), ("--projects", args.projects), ("--auto-copy", args.auto_copy),
             ("--tool-widening", args.tool_widening))
    for flag, value in given:
        if value is not None:
            parts += [flag, value]
    for flag, values in (("--resolve", args.resolve), ("--skip", args.skip), ("--only", args.only)):
        for value in values:
            parts += [flag, value]
    return " ".join(shlex.quote(part) for part in parts)


def _open_conflicts(args: argparse.Namespace, screen: dict[str, Any]) -> list[Any]:
    answered = {value.partition("=")[0] for value in args.resolve}
    return [item for item in (screen["plan"].conflicts if "plan" in screen else []) if item.name not in answered]


def apply_lines(args: argparse.Namespace, screen: dict[str, Any]) -> list[ui.Line]:
    """How to apply the dry run: in a terminal, or with one command."""

    rows: list[list[ui.Line]] = []
    conflicts = _open_conflicts(args, screen)
    names = ", ".join(item.name for item in conflicts)
    rows.append([ui.muted("In a terminal"), ui.cmd("agt setup"),
                 ui.muted(f"asks about {names} first, then Enter applies" if conflicts else "Enter applies it")])
    if conflicts:
        picks = " ".join(f"--resolve {item.name}=<cli>" for item in conflicts)
        rows.append([ui.muted("Pick a copy first"), ui.cmd(f"agt setup --dry-run {picks}"),
                     ui.muted("then run the command it shows")])
    else:
        rows.append([ui.muted("In one command"), ui.cmd(short_command(args, screen))])
    return ["", ui.bold("To apply it"), *ui.columns(rows, gap=3)]


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


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def render(args: argparse.Namespace, screen: dict[str, Any], *, dry_run: bool = False) -> list[ui.Line]:
    report = screen["setup"]
    state_text = "dry run, nothing written" if dry_run else "nothing is written until you press Enter"
    lines: list[ui.Line] = [[ui.bold("agt setup"), "  ", ui.muted(state_text)]]
    rows = setup.report_rows(report.done, ui.ok("+")) + setup.report_rows(report.unchanged, ui.muted("·")) \
        + setup.report_rows(report.failed, ui.Span("error-mark", "✗"))
    if rows:
        lines += ["", ui.bold("Plugin"), *ui.columns(rows, gap=2)]
    if "plan" not in screen:
        return lines
    plan = screen["plan"]
    labels = [provider.label for provider in plan.included]
    if len(labels) < 2:
        lines += ["", ui.muted("Sharing agents and skills: only one CLI folder here, so nothing to share yet.")]
    else:
        lines += ["", ui.bold("Sharing agents and skills between " + _and(labels))]
    count, changed = len(plan.changes), len(plan.folders_changed)
    if len(labels) < 2:
        pass
    elif count:
        line: list[ui.Piece] = ["  ", ui.ok("+"), f" {count} change{'s' if count != 1 else ''} in {changed} folder"
                                f"{'s' if changed != 1 else ''}"]
        if plan.new_folders:
            new = len(plan.new_folders)
            line += [f", and {new} new folder{'s' if new != 1 else ''} ",
                     ui.muted("(" + ", ".join(short(folder) for folder in plan.new_folders) + ")")]
        lines.append(line)
    elif not plan.conflicts:
        lines.append(["  ", ui.muted("·"), " every folder already has them"])
    for item in plan.conflicts:
        lines.append(["  ", ui.Span("error-mark", "!"),
                      f" {item.key} differs between folders; agt asks which copy wins before it writes."])
    for kind in (plan.instructions, plan.mcp, plan.hooks, plan.plugins):
        if kind is not None:
            lines.append(["  ", *ui.inline(kind.summary().splitlines()[0])])
    if len(labels) > 1:
        lines.append(["  ", ui.code("agt share enable --dry-run"), ui.muted(" shows every copy.")])
    repos = screen["repos"]
    if repos:
        lines += ["", ui.bold("Repos that share their own agents and skills"), *(f"  [x] {short(root)}" for root in repos)]
    else:
        lines += ["", ui.muted("Repos: none of the repos your CLIs know has its own agents or skills yet.")]
    lines += ["", ui.bold("Options"), *(f"  [{'x' if on else ' '}] {label(name)}" for name, on in screen["switches"].items())]
    return lines


def apply(args: argparse.Namespace, screen: dict[str, Any]) -> int:
    """Do all of it: the plugin, then sharing (with its repos), then the lines that aren't share settings."""

    config = load_config()
    code, report = setup.run_setup(config, args.host or ["auto"], args.account, dry_run=False, claude_route=args.claude_route)
    todo, report.todo = report.todo, []
    ui.out(*setup.report_lines(report, todo=False, footer=False))
    if "plan" not in screen:
        _finish(todo)
        return code
    switches, saved = screen["switches"], screen["saved"]
    sargs = screen["sargs"]
    sargs.projects = ",".join(str(root) for root in screen["repos"]) or "none"
    sargs.auto_copy = "on" if switches["auto_copy"] else "off"
    sargs.tool_widening = "allow-if-shell" if switches["widen"] else "skip"
    saved.worktrees = "auto" if switches["worktrees"] else "command"
    saved.codex_session = switches["codex_session"]
    share_code = share._enable_write(sargs, saved, setup=True) or 0
    if switches["rules"]:
        for target in ("claude", "codex"):
            path, _ = rules.target_path(target)
            if path.parent.is_dir():
                print(rules.update_file(path, rules.render(_rules_args(target))))
    _finish(todo)
    return max(code, share_code)


def _first_job() -> list[ui.Line]:
    """Three ways to try what setup set up, with another CLI than Claude Code when there is one."""

    config = load_config()
    found = setup.detect_hosts(config)
    other = next((host for host in ("codex", "antigravity") if host in found), None)
    name = {"codex": "Codex", "antigravity": "Gemini"}.get(other or "", "a subagent")
    rows: list[list[ui.Line]] = []
    if "claude" in found:
        rows.append([ui.muted("In Claude Code, ask"), f"Use {name} to review my last commit."])
        rows.append([ui.muted("Or type"), ui.cmd("/agents-router:delegate review my last commit" + (f" in {name}" if other else ""))])
    runtime = f" --runtime {other}" if other else ""
    rows.append([ui.muted("From this terminal"), ui.cmd(f'agt run{runtime} "review my last commit"')])
    return ["", ui.bold("Then try your first delegated job"), *ui.columns(rows, gap=3)]


def _finish(todo: list[str]) -> None:
    """Last, once: what's left for you (logins and restarts), then how to try it, whatever came before."""

    ui.out(*setup.still_needed(todo), *_first_job(), "",
           [ui.code("agt"), ui.muted(" on its own shows what is set up and what to do next.")])


def _rules_args(target: str) -> argparse.Namespace:
    values = {f"home_{tier}": None for tier in rules.TIERS} | {f"try_{tier}": "none" for tier in rules.TIERS}
    return argparse.Namespace(target=target, work_folder=[], main_limit=4, helper_limit=3, **values)


def edit(args: argparse.Namespace, screen: dict[str, Any]) -> None:
    """`p` on the screen: the share lists, then the repos, then the lines."""

    share._enable_lists(screen["sargs"], screen["saved"], screen["plan"])
    if screen["repos"]:
        repos = picker.ListState("Which repos share their own agents and skills?",
                                 [picker.Choice(str(root), short(root), True) for root in screen["repos"]], verbs=("joins", "leaves"),
                                 enter="next", leave="skip")
        if picker.choose(repos) != picker.QUIT:
            args.projects = ",".join(sorted(repos.checked())) or "none"
    lines = picker.ListState("Options", [picker.Choice(name, label(name), on, name=NAMES[name])
                                         for name, on in screen["switches"].items()])
    if picker.choose(lines) == picker.QUIT:
        return
    checked = lines.checked()
    args.rules = "on" if "rules" in checked else "off"
    args.codex_session_line = "on" if "codex_session" in checked else "off"
    args.mcp = "on" if "mcp" in checked else "off"
    args.plugins = "on" if "plugins" in checked else "off"
    args.worktrees = "auto" if "worktrees" in checked else "command"
    args.auto_copy = "on" if "auto_copy" in checked else "off"
    args.tool_widening = "allow-if-shell" if "widen" in checked else "skip"
    args.resolve, args.skip, args.only = screen["sargs"].resolve, screen["sargs"].skip, screen["sargs"].only


def run(args: argparse.Namespace) -> int:
    """`agt setup`: the screen in a terminal, the proposal with --dry-run --json, the plan with --yes."""

    if args.dry_run:
        screen = build(args)
        if args.json:
            print(json.dumps(proposal(args, screen), indent=2, ensure_ascii=False))
            return 0
        ui.out(*render(args, screen, dry_run=True), *setup.still_needed(screen["setup"].todo, "Still needed from you after it"),
               *apply_lines(args, screen))
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
            ui.out(share._first_choice("agt setup", len(open_conflicts)), "")
            for item in open_conflicts:
                choice = share._ask_version(item)
                if choice is None:
                    print("Nothing was written.")
                    return 0
                args.resolve.append(f"{item.name}={choice}")
            continue
        ui.out(*render(args, screen))
        try:
            answer = input("\nApply all of it? Enter yes · p change it · q quit: ").strip().lower()
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

