"""`agt setup`'s one screen: the plugin install and agt share's setup, suggested together and applied with one answer.

In a terminal, `agt setup` shows everything it suggests, already ticked: the
hosts the plugin goes into, the CLI folders that would share and what goes
where, the repos that have their own agents or skills, and a line each for
worktrees joining by themselves, auto copy, tool widening, delegation rules
and Codex's session line. Nothing is written until you press Enter; `p`
changes the share picks, the repos and the lines, and `q` quits. A conflict
(an item whose copies differ) is asked first. "Still needed from you" comes
last, once.

Without a terminal, plain `agt setup` installs the plugin in this Claude
profile only and says how to set up sharing and the other profiles. An agent
runs `agt setup --dry-run --json`, which returns the same screen as a proposal
(the plugin lines, `claude_profiles`, the share table, `repos`, `switches`,
`needs_answer`, `command` and `next`), asks the user once, and runs that
`command` (`agt setup --yes --if-plan <id> ...`). `--if-plan` refuses when
anything changed since the dry run, the Claude profiles included. `--share off`
leaves sharing out. `--claude-profiles` picks the profiles (all by default).
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
    parser.add_argument("--roles", choices=("on", "off"), default="on",
                        help="Set up roles: the proposed role table and its agt-<role> agents (on by default).")
    parser.add_argument("--role", action="append", default=[], metavar="ROLE=PROVIDER[,PROVIDER]|ROLE=off",
                        help="Change one role in the proposed table, or leave it out.")
    parser.add_argument("--role-order", action="append", default=[], metavar="ROLE=FIRST,SECOND",
                        help="An edit role's providers in the order agt tries them (the answer to setup's order question).")


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
    code, report = setup.run_setup(config, args.host or ["auto"], args.account, dry_run=True, claude_route=args.claude_route,
                                   claude_profiles=args.claude_profiles or "all", role_agents=False)
    screen: dict[str, Any] = {"setup": report, "setup_code": code}
    roles = setup.plan_roles(config, args.roles, args.role, args.role_order)
    roles_report = setup.Report(dry_run=True)
    setup.apply_roles(roles, roles_report, setup.selected_folders(report), setup.named_hosts(args.host))
    screen.update(roles=roles, roles_report=roles_report, hosts=args.host)
    # The plan id covers the Claude profiles and their real folders too, so --if-plan refuses a changed set,
    # and where the plugin comes from (AGT_PLUGIN_SOURCE), which the settings route writes without a command.
    from agents_router.profiles import canonical

    profiles = [[str(canonical(item["folder"])), item["selected"]] for item in report.claude]
    source = setup.plugin_source()
    if args.share == "off":
        content: dict[str, Any] = {"share": "off", "setup": sorted(report.done), "claude": profiles, "source": source,
                                   "roles": roles.digest()}
        screen["digest"] = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:12]
        return screen
    saved = settings.load()
    sargs = _share_args(args)
    sargs.mcp = args.mcp or ("on" if saved.mcp else "off")
    sargs.plugins = args.plugins or ("on" if saved.plugins else "off")
    plan = share._plan(sargs, state.load(), saved)
    repos = share._project_choice(sargs, saved)
    screen.update(saved=saved, sargs=sargs, plan=plan, repos=repos, switches=_switches(args, saved, plan))
    content = {"share": plan.digest, "repos": sorted(str(root) for root in repos), "switches": screen["switches"],
               "setup": sorted(report.done), "claude": profiles, "source": source, "roles": roles.digest()}
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
    if screen["setup"].claude:
        parts += ["--claude-profiles", args.claude_profiles or "all"]
    if args.claude_route != "auto":
        parts += ["--claude-route", args.claude_route]
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
    parts += screen["roles"].flags()
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
    given = (("--claude-profiles", args.claude_profiles),
             ("--claude-route", args.claude_route if args.claude_route != "auto" else None),
             ("--share", "off" if args.share == "off" else None), ("--rules", args.rules),
             ("--codex-session-line", args.codex_session_line), ("--worktrees", args.worktrees), ("--mcp", args.mcp),
             ("--plugins", args.plugins), ("--projects", args.projects), ("--auto-copy", args.auto_copy),
             ("--tool-widening", args.tool_widening), ("--roles", "off" if args.roles == "off" else None))
    for flag, value in given:
        if value is not None:
            parts += [flag, value]
    for flag, values in (("--resolve", args.resolve), ("--skip", args.skip), ("--only", args.only),
                         ("--role", args.role), ("--role-order", args.role_order)):
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
    data: dict[str, Any] = {"dry_run": True, "plan": screen["digest"],
                            "plugin": {"planned": report.done, "already": report.unchanged,
                                       "failed": report.failed, "notes": report.notes}}
    if "plan" in screen:
        share_proposal = planner.proposal(screen["plan"], "")
        data.update(share=share_proposal, repos=[str(root) for root in screen["repos"]],
                    switches={name: {"on": value, "means": SWITCHES[name][0]} for name, value in screen["switches"].items()},
                    needs_answer=share_proposal["needs_answer"])
    if report.claude:
        data["claude_profiles"] = [{"name": item["name"], "folder": item["folder"], "this_session": item["this"],
                                    "gets_plugin": item["selected"]} for item in report.claude]
    data["roles"] = roles_proposal(screen)
    data.update(still_needed=report.todo, command=command_line(args, screen), next=NEXT)
    return data


def roles_proposal(screen: dict[str, Any]) -> dict[str, Any]:
    """The role part of the proposal: the table, why, the agents it writes and any order to ask."""

    from agents_router import hosts, roles

    plan, report = screen["roles"], screen["roles_report"]
    named = setup.named_hosts(screen["hosts"])
    agents_for = (", ".join(hosts.HOST_LABELS[host] for host in named) if named
                  else hosts.HOST_LABELS.get(hosts.detect_host(), "no CLI (a terminal): no role agents"))
    data: dict[str, Any] = {"state": plan.state, "means": ROLE_STATES[plan.state], "agents_for": agents_for}
    if plan.state == "propose":
        data["table"] = {name: {"access": roles.BASE_ROLES[name], "providers": table["providers"],
                                "use_when": roles.BASE_DESCRIPTIONS[name], "why": plan.reasons.get(name, "")}
                         for name, table in plan.table.items()}
        data["left_out"] = plan.off
    data["planned"] = report.done
    if report.notes:
        data["notes"] = report.notes
    data["needs_order"] = plan.needs_order
    data["answers"] = ROLE_ANSWERS
    return data


ROLE_STATES = {
    "propose": "a role table to write to your user settings, with one agt-<role> agent per role for this CLI",
    "set": "roles are already set up: kept as they are; only the agt-<role> agents follow them",
    "off": "roles skipped: no role settings and no role agents are written",
    "none": "no CLI with an account to propose roles from; `agt roles propose` can later",
}
ROLE_ANSWERS = {
    "Approve": "run `command`",
    "Change a role": "--role ROLE=PROVIDER[,PROVIDER] (or ROLE=off); an edit role with several providers: "
                     "ask their order, then --role-order ROLE=FIRST,SECOND",
    "Skip roles": "--roles off",
    "Cancel": "run nothing",
}


NEXT = ("Ask the user once, in one message: the share table, the switches, the Claude profiles that get the plugin "
        "(claude_profiles, with their folders) and the role table (roles.table), with the answers Approve, Change a role, "
        "Skip roles or Cancel. Ask each roles.needs_order question before the command runs. Apply their answer by "
        "changing the flags and running --dry-run --json again, then run `command` exactly as given. Never run it "
        "before they answer.")


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
    lines += profile_lines(report)
    lines += role_lines(screen)
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


def role_lines(screen: dict[str, Any]) -> list[ui.Line]:
    """The role table setup proposes (or why there's none), and the role agents it writes."""

    from agents_router import roles

    plan, report = screen["roles"], screen["roles_report"]
    if plan.state != "propose":
        return ["", [ui.bold("Roles"), "  ", ui.muted(ROLE_STATES[plan.state])]]
    rows = []
    for name, table in plan.table.items():
        providers = table["providers"]
        if roles.BASE_ROLES[name] == "edit" and len(providers) > 1:
            shown = "   ".join(f"{n} {item}" for n, item in enumerate(providers, 1))
        else:
            shown = "  +  ".join(providers)
        rows.append([name, shown, ui.muted(plan.reasons.get(name, ""))])
    lines: list[ui.Line] = ["", ui.bold("Roles"), *ui.columns(rows, indent=2, gap=2)]
    agents = setup.report_rows([line for line in report.done if not line.startswith("Roles: ")], ui.ok("+"))
    if agents:
        lines += ui.columns(agents, gap=2)
    lines.append(["  ", ui.muted("Change one with "), ui.code("--role ROLE=PROVIDER[,PROVIDER]"), ui.muted("; skip roles with "),
                  ui.code("--roles off"), ui.muted(".")])
    return lines


def profile_lines(report: setup.Report) -> list[ui.Line]:
    """Your Claude Code profiles, one line each, ticked when they get the plugin. Only when there's more than one."""

    if len(report.claude) < 2:
        return []
    rows = [[f"[{'x' if item['selected'] else ' '}]", item["name"], ui.muted(ui.short_path(item["folder"])),
             ui.muted("this session" if item["this"] else "")] for item in report.claude]
    return ["", ui.bold("Claude profiles that get the plugin"), *ui.columns(rows, indent=2, gap=2),
            ["  ", ui.muted("Change it with "), ui.code("--claude-profiles all|this|NAME[,NAME]"), ui.muted(".")]]


def apply(args: argparse.Namespace, screen: dict[str, Any]) -> int:
    """Do all of it: the plugin, then sharing (with its repos), then the lines that aren't share settings."""

    config = load_config()
    code, report = setup.run_setup(config, args.host or ["auto"], args.account, dry_run=False, claude_route=args.claude_route,
                                   claude_profiles=args.claude_profiles or "all", role_agents=False)
    todo, report.todo = report.todo, []
    ui.out(*setup.report_lines(report, todo=False, footer=False))
    folders = setup.selected_folders(report)
    if "plan" not in screen:
        code = max(code, _apply_roles(screen, folders, todo))
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
    roles_code = _apply_roles(screen, folders, todo)
    _finish(todo)
    return max(code, share_code, roles_code)


def _apply_roles(screen: dict[str, Any], folders: list[Any], todo: list[str]) -> int:
    """Setup's role step, last before what's left for you: the table, then this CLI's role agents."""

    report = setup.Report()
    setup.apply_roles(screen["roles"], report, folders, setup.named_hosts(screen["hosts"]))
    rows = setup.report_rows(report.done, ui.ok("+")) + setup.report_rows(report.failed, ui.Span("error-mark", "✗"))
    lines: list[ui.Line] = ["", *ui.columns(rows, gap=2)] if rows else []
    lines += [["  ", *ui.inline(ui.tilde(note))] for note in report.notes]
    if lines:
        ui.out(*lines)
    for item in report.todo:
        if item not in todo:
            todo.append(item)
    return 1 if report.failed else 0


def _ready_target(config: Any, found: list[str]) -> tuple[str, str, str] | None:
    """Where a first job can run now: (name to show, `in`/`on` phrase, `agt run` flags), or None.

    Codex, then Antigravity, when logged in; else a logged-in Claude profile that takes this session's
    jobs. An installed CLI without a login isn't offered: its job would fail.
    """

    from agents_router.runtimes import get_runtime

    for host, name in (("codex", "Codex"), ("antigravity", "Gemini")):
        if host in found:
            runtime_config = config.runtime(host)
            if (get_runtime(host).auth_status(runtime_config, runtime_config.primary_account()) or {}).get("loggedIn"):
                return name, f" in {name}", f" --runtime {host}"
    if "claude" in found:
        runtime_config = config.runtime("claude")
        adapter = get_runtime("claude")
        for account in runtime_config.worker_accounts():
            if (adapter.auth_status(runtime_config, account) or {}).get("loggedIn"):
                profile = setup.profile_name(account)
                return profile, f" on {profile}", f" --account {profile}"
    return None


def _first_job() -> list[ui.Line]:
    """Three ways to try what setup set up, on a CLI or Claude profile that can take a job now."""

    config = load_config()
    found = setup.detect_hosts(config)
    name, where, flags = _ready_target(config, found) or ("a subagent", "", "")
    rows: list[list[ui.Line]] = []
    if "claude" in found:
        rows.append([ui.muted("In Claude Code, ask"), f"Use {name} to review my last commit."])
        rows.append([ui.muted("Or type"), ui.cmd("/agents-router:delegate review my last commit" + where)])
    rows.append([ui.muted("From this terminal"), ui.cmd(f'agt run{flags} "review my last commit"')])
    return ["", ui.bold("Then try your first delegated job"), *ui.columns(rows, gap=3)]


def _finish(todo: list[str]) -> None:
    """Last, once: what's left for you (logins and restarts), then how to try it, whatever came before."""

    ui.out(*setup.still_needed(todo), *_first_job(), "",
           [ui.code("agt"), ui.muted(" on its own shows what is set up and what to do next.")])


def _rules_args(target: str) -> argparse.Namespace:
    values = {f"home_{tier}": None for tier in rules.TIERS} | {f"try_{tier}": "none" for tier in rules.TIERS}
    return argparse.Namespace(target=target, work_folder=[], main_limit=4, helper_limit=3, **values)


def edit(args: argparse.Namespace, screen: dict[str, Any]) -> None:
    """`p` on the screen: the roles, then the share lists, the repos and the lines."""

    _edit_roles(args, screen)
    if "plan" not in screen:
        return
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


def _edit_roles(args: argparse.Namespace, screen: dict[str, Any]) -> None:
    """Which proposed roles to set up: an unticked role is left out, none ticked skips roles. Providers change
    later with `agt roles config`."""

    plan = screen["roles"]
    if plan.state != "propose":
        return
    names = list(plan.table)
    state = picker.ListState("Which roles should setup add? (providers change later with agt roles config)",
                             [picker.Choice(name, f"{name}  {', '.join(plan.table[name]['providers'])}", True)
                              for name in names], verbs=("added", "left out"), enter="next", leave="skip")
    if picker.choose(state) == picker.QUIT:
        return
    checked = state.checked()
    if not checked:
        args.roles = "off"
        return
    # Keep changes to the roles still in, and roles left out before (no longer listed here).
    args.role = [value for value in args.role if value.partition("=")[0] in checked or value.partition("=")[0] not in names]
    args.role += [f"{name}=off" for name in names if name not in checked]


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
        if screen["roles"].needs_order:
            return _needs_order(args, screen)
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
        if screen["roles"].needs_order:
            for ask in screen["roles"].needs_order:
                order = _ask_order(ask)
                if order is None:
                    print("Nothing was written.")
                    return 0
                args.role_order.append(f"{ask['role']}={','.join(order)}")
            continue
        ui.out(*render(args, screen))
        try:
            answer = input("\nApply all of it? Enter yes · p change it · q quit: ").strip().lower()
        except EOFError:
            answer = "q"
        if answer == "q":
            print("Nothing was written.")
            return 0
        if answer == "p":
            edit(args, screen)
            continue
        if not answer:
            return apply(args, screen)


def _needs_order(args: argparse.Namespace, screen: dict[str, Any]) -> int:
    """An edit role was given several providers: nothing is written until the person says which goes first."""

    asks = screen["roles"].needs_order
    retry = " ".join(shlex.quote(part) for part in ["agt", "setup", "--dry-run", *_given_flags(args),
                                                     *(["--json"] if args.json else [])])
    if args.json:
        print(json.dumps({"error": "needs_choice", "ask": [ask["ask"] for ask in asks], "choices": asks,
                          "retry": retry + " " + " ".join(ask["flag"] for ask in asks)}, indent=2, ensure_ascii=False))
        return 3
    lines: list[ui.Line] = [[ui.bold("agt setup"), "  ", ui.warn("! nothing written: a role needs its order")]]
    for ask in asks:
        lines += ["", f"  {ask['ask']}", "  Add the order to the command, first provider first:",
                  ["    ", ui.cmd(ask["flag"])]]
    ui.out(*lines)
    return 3


def _given_flags(args: argparse.Namespace) -> list[str]:
    """The flags the person gave, for a retry command (short_command without the plan id)."""

    return shlex.split(short_command(args, {}))[3:]


def _ask_order(ask: dict[str, Any]) -> list[str] | None:
    """In a terminal: which provider of an edit role goes first. None quits."""

    providers = ask["providers"]
    print("\n" + ask["ask"])
    for n, item in enumerate(providers, 1):
        print(f"  {n}. {item}")
    while True:
        try:
            answer = input("First provider (number), or q to quit: ").strip().lower()
        except EOFError:
            return None
        if answer == "q":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(providers):
            first = providers[int(answer) - 1]
            return [first, *(item for item in providers if item != first)]


def scopes_note() -> str:
    """For plain `agt setup` without a terminal: how to set up sharing too."""

    listed = len(scope.listed())
    return ("Sharing agents and skills between your CLIs: run `agt setup` in a terminal, or `agt setup --dry-run --json` "
            "from an agent" + (f" ({listed} repos share now)" if listed else "") + ".")

