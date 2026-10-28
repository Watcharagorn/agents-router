"""`agt`: command-line front end for Agents Router."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from agents_router import __version__, accounts, config_cli, helptext, jobs, levels, plans, rules, setup, sync, synclog, tools, ui
from agents_router.config import NAME_RE, Account, homes_dir, load_config, save_accounts, user_settings_path
from agents_router.hosts import HOST_LABELS
from agents_router.fsutil import make_private_dir
from agents_router.runtimes import AVAILABLE as RUNTIMES, get_runtime
from agents_router.runtimes.base import ACCESS_LEVELS, JobSpec
from agents_router.share import commands as share_commands


def _print(value: Any) -> None:
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _job_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("prompt", help="The task. Use '-' to read it from stdin.")
    parser.add_argument("--runtime", default="claude", choices=RUNTIMES)
    parser.add_argument("--account")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument(
        "--override", metavar="USER_WORDS",
        help="The user's own words authorizing more parallel jobs. Bypass agt's caps and queueing only; "
        "never set this on your own judgment. Use only when the user asked for more jobs in this conversation "
        "or confirmed after a warning explaining how much of the 5-hour window is left.",
    )
    parser.add_argument("--access", default="edit", choices=ACCESS_LEVELS)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds (default 1800).")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--resume-job")


def _start(args: argparse.Namespace) -> dict[str, Any]:
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    return jobs.create_job(
        runtime=args.runtime,
        prompt=prompt,
        cwd=str(Path(args.cwd).resolve()),
        account=args.account,
        model=args.model,
        effort=args.effort,
        override=args.override,
        access=args.access,
        max_turns=args.max_turns,
        timeout_seconds=args.timeout,
        add_dirs=[str(Path(item).resolve()) for item in args.add_dir],
        resume_job=args.resume_job,
    )


# Accounts --------------------------------------------------------------------


def cmd_accounts(args: argparse.Namespace) -> int:
    config = load_config()
    action = args.action or "list"

    if action == "list":
        names = [args.runtime] if args.runtime else _runtimes_set_up(config)
        reports = [tools.account_report(name) for name in names]
        if args.json:
            _print(reports[0] if args.runtime else {"runtimes": reports})
            return 0
        total = sum(len(report.get("accounts", [])) for report in reports)
        lines: list[ui.Line] = [[ui.bold("agt accounts"), f"  {_and(names)}, {total} account{'s' if total != 1 else ''}"]]
        rows = ui.columns([_account_rows(entry, method=True) for report in reports for entry in report.get("accounts", [])],
                          gap=3)
        for name, report in zip(names, reports):
            lines += ["", ui.bold(HOST_LABELS.get(name, name)), *(rows.pop(0) for _ in report.get("accounts", []))]
        others = [name for name in names if name != "claude"]
        flag = f" [--runtime {others[0]}]" if others else ""
        lines += ["", *ui.columns([[ui.muted("Log in"), ui.cmd(f"agt accounts login <name>{flag}")],
                                   [ui.muted("Add one"), ui.cmd(f"agt accounts add <name>{flag}")]], gap=3)]
        ui.out(*lines)
        return 0
    args.runtime = args.runtime or "claude"
    runtime_config = config.runtime(args.runtime)
    runtime = get_runtime(args.runtime)

    if action == "reset":
        # The primary account can cool down too, so 'primary' is a valid name here.
        if not args.name or runtime_config.find(args.name) is None:
            print(f"No {args.runtime} account named '{args.name or ''}'. See: agt accounts list")
            return 1
        cleared = accounts.clear_cooldown(args.runtime, args.name)
        print(f"{args.runtime} account '{args.name}': " + ("cooldown cleared" if cleared else "had no cooldown"))
        return 0

    if not args.name or not NAME_RE.match(args.name) or args.name == "primary":
        print("Give the account a short name: letters, digits, dot, dash or underscore ('primary' is reserved).")
        return 2

    if action == "add":
        if runtime_config.find(args.name):
            print(f"{args.runtime} account '{args.name}' already exists.")
            return 1
        home = Path(args.home).expanduser().resolve() if args.home else homes_dir() / args.runtime / args.name
        try:
            status, account = setup.register_account(config, args.runtime, args.name, home)
        except ValueError as exc:
            print(exc)
            return 1
        if status == "exists":
            print(f"That folder already belongs to {args.runtime} account '{account.name}'.")
            return 1
        make_private_dir(home)
        save_accounts(config)
        print(f"Added {args.runtime} account '{args.name}' with folder {home}")
        if getattr(runtime, "add_note", None):
            print(runtime.add_note)
        report = runtime.prepare(runtime_config, account, _probe_spec(args.runtime))
        _print_setup(report)
        if args.no_login:
            print(f"Log in later with: agt accounts login {args.name} --runtime {args.runtime}")
            return 0
        return _login(runtime_config, runtime, account)

    account = runtime_config.find(args.name)
    if account is None:
        print(f"No {args.runtime} account named '{args.name}'. See: agt accounts list")
        return 1

    if action == "login":
        return _login(runtime_config, runtime, account)
    if action == "sync":
        _print_setup(runtime.prepare(runtime_config, account, _probe_spec(args.runtime, args.cwd)))
        return 0
    if action == "remove":
        runtime_config.accounts = [item for item in runtime_config.accounts if item.name != args.name]
        save_accounts(config)
        print(f"Removed '{args.name}' from the config. Its folder was left as is: {account.home}")
        return 0
    if action in ("enable", "disable"):
        account.enabled = action == "enable"
        save_accounts(config)
        print(f"{args.name}: {'enabled' if account.enabled else 'disabled'}")
        return 0
    print(f"Unknown action: {action}")
    return 2


def _probe_spec(runtime: str, cwd: str | None = None) -> JobSpec:
    return JobSpec(job_id="probe", runtime=runtime, prompt="", cwd=str(Path(cwd or os.getcwd()).resolve()))


def _print_setup(report: dict[str, Any]) -> None:
    for key, label in (("linked", "Linked"), ("copied", "Copied"), ("removed", "Removed")):
        if report.get(key):
            print(f"{label}: {', '.join(report[key])}")
    if report.get("mcp_servers"):
        print(f"Shared MCP servers: {', '.join(report['mcp_servers'])}")
    if report.get("plugin_dirs"):
        print(f"Shared plugins: {', '.join(Path(item).parts[-2] for item in report['plugin_dirs'])}")
    for key in ("conflicts", "warnings"):
        for message in report.get(key) or []:
            print(f"Note: {message}")


def _login(runtime_config, runtime, account: Account) -> int:
    argv, env = runtime.login_command(runtime_config, account)
    print(f"Opening the {runtime.name} login for '{account.name}'. Sign in with the account this profile should use.")
    if hasattr(runtime, "login_instructions"):
        print(runtime.login_instructions(account))
    cwd = runtime.login_cwd(account) if hasattr(runtime, "login_cwd") else None
    return subprocess.call(argv, env=env, cwd=cwd)


# Setup -----------------------------------------------------------------------


def _refresh_share_launcher(dry_run: bool) -> None:
    """agt share's launcher follows agt when it moved (only once sharing set it up)."""

    if dry_run:
        return
    try:
        from agents_router.share import triggers

        triggers.refresh_launcher()
    except OSError:
        pass


def cmd_setup(args: argparse.Namespace) -> int:
    from agents_router import setup_screen

    code = setup_screen.run(args)  # the one screen, the agent's proposal, or --yes
    if code != -1:
        _refresh_share_launcher(args.dry_run)
        return code
    code, report = setup.run_setup(
        load_config(), args.host or ["auto"], args.account, dry_run=False, claude_route=args.claude_route,
    )
    _refresh_share_launcher(False)
    report.notes.append(setup_screen.scopes_note())
    ui.out(*setup.report_lines(report))
    return code


def cmd_update(args: argparse.Namespace) -> int:
    code, report = setup.run_update(load_config(), args.host or ["auto"], dry_run=args.dry_run, package=not args.no_package)
    _refresh_share_launcher(args.dry_run)
    ui.out(*setup.report_lines(report, "update"))
    return code


# Doctor ----------------------------------------------------------------------


def _share_doctor(fix: bool) -> list[str]:
    """agt share's line: on or off, the watcher, the launcher and the state; `--fix` repairs what it can."""

    from agents_router.share import settings as share_settings, state as share_state, watch

    try:
        saved = share_settings.load()
    except share_settings.SettingsError as exc:
        return [f"agt share: {exc}"]
    if not saved.enabled:
        return ["agt share: off."]
    lines = [f"agt share: on for {', '.join(folder.label for folder in saved.folders)}."]
    if fix:
        lines += [f"  Fixed: {line}" for line in watch.fix(saved)]
    lines += [f"  {line}" for line in watch.health_lines(saved)]
    try:
        share_state.load()
    except share_state.StateError as exc:
        lines.append(f"  Problem: {exc}")
    return lines


def _runtimes_set_up(config: Any) -> list[str]:
    """The runtimes doctor and accounts look at when you don't name one: those whose CLI is installed."""

    found = [name for name in RUNTIMES if get_runtime(name).find_binary(config.runtime(name)) is not None]
    return found or ["claude"]


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _from(text: str, source: Any) -> str:
    return f"{text} ({source})" if source and source != "default" else text


def _limits_text(entry: dict[str, Any]) -> str:
    shown = entry.get("limits") or {}
    jobs_text = "no cap on jobs" if shown.get("max_jobs") is None else f"up to {shown['max_jobs']} jobs"
    text = _from(jobs_text, shown.get("max_jobs_from"))
    if shown.get("max_effort"):
        text += ", " + _from(f"effort up to {shown['max_effort']}", shown.get("max_effort_from"))
    if shown.get("models"):
        text += ", " + _from(f"models {', '.join(shown['models'])}", shown.get("models_from"))
    if shown.get("suggested_plan"):
        text += f"; suggested plan {shown['suggested_plan']}"
    return text


def _login_state(entry: dict[str, Any]) -> ui.Span:
    auth = entry.get("auth")
    if not entry.get("enabled", True):
        return ui.muted("turned off")
    if auth is None:
        return ui.muted("not checked")
    return ui.ok("✓ logged in") if auth.get("loggedIn") else ui.error("✗ not logged in")


def _account_rows(entry: dict[str, Any], *, method: bool = False) -> list[ui.Line]:
    """One account: name, folder, login, then (for accounts list) how it logs in and its running jobs, and limits."""

    row: list[ui.Line] = [entry["name"], ui.muted(ui.short_path(entry.get("home") or "")), _login_state(entry)]
    detail: list[ui.Piece] = []
    if method:
        row.append(ui.muted(str((entry.get("auth") or {}).get("authMethod") or "")))
        running = entry.get("running_jobs") or 0
        detail.append(f"{running} running" if running else "no jobs running")
    if entry.get("cooldown_until"):
        detail.append(ui.warn(f"cooling down until {entry['cooldown_until']}"))
    if entry.get("usage"):
        detail.append(ui.muted("used " + ", ".join(f"{share}% of {window}" for window, share in entry["usage"].items())))
    detail.append(_limits_text(entry).replace("no cap on jobs", "no cap") if method else _limits_text(entry))
    joined: list[ui.Piece] = []
    for index, piece in enumerate(detail):
        joined += [", ", piece] if index else [piece]
    return row + [joined]


def _runtime_limits(config: Any, name: str) -> str:
    shown = config.runtime(name)
    full = "refuse new jobs" if shown.when_full == "refuse" else shown.when_full
    text = f"{full} when full, usage ceiling {shown.usage_ceiling:.0%}, failover {shown.failover}"
    return text + (f", at most {shown.max_jobs} jobs" if shown.max_jobs else "")


def _login_fix(runtime: str, account: str) -> ui.Line:
    if runtime == "antigravity" and account == "primary":
        return "run agy in a terminal and sign in"
    return ui.cmd(f"agt accounts login {account}" + ("" if runtime == "claude" else f" --runtime {runtime}"))


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    named = getattr(args, "runtime", None)
    names = [named] if named else _runtimes_set_up(config)
    reports = {name: tools.account_report(name) for name in names}
    problems: list[tuple[str, ui.Line]] = []  # what's wrong, and its fix
    for name, report in reports.items():
        label = HOST_LABELS.get(name, name)
        if not report.get("binary"):
            problems.append((f"no {name} binary found.",
                             f"install {label}, or set [{name}] binary in {ui.short_path(user_settings_path())}"))
            continue
        for entry in report.get("accounts", []):
            if not (entry.get("auth") or {}).get("loggedIn") and entry.get("enabled", True):
                problems.append((f"{label} account {entry['name']} is not logged in.", _login_fix(name, entry["name"])))
    if getattr(args, "json", False):
        _print(reports[names[0]] if named else {"runtimes": list(reports.values())})
        return 1 if problems else 0
    notes = list(config.notes)
    notes += [f"{found}. Two installs both register an MCP server and hooks; remove the old one."
              for found in setup.upstream_installs(config)]
    for name in names:
        runtime_config, runtime = config.runtime(name), get_runtime(name)
        for account in runtime_config.pool_accounts():
            copies = runtime.fallback_copies(account) if hasattr(runtime, "fallback_copies") else sync.fallback_copies(account.home)
            if copies and not account.is_primary:
                notes.append(f"account '{account.name}' has copies instead of links (they don't follow later edits "
                             f"until the next sync): {', '.join(copies)}")
        if name == "codex" and os.name == "nt":
            from agents_router.runtimes.codex import windows_sandbox_summary
            notes.append(windows_sandbox_summary(runtime_config))
        limits = reports[name].get("limits") or {}
        if "room_cap" in limits:
            left = limits["remaining_percent"]
            detail = f"{left:g}% left" if left is not None else "no usage data"
            notes.append(f"{name} max_jobs_by_room: {limits['room_cap']} jobs at a time, {detail}; "
                         f"effective runtime cap {limits['effective_max_jobs']}")
        for entry in reports[name].get("accounts", []):
            notes += [f"{name} {entry['name']}: {pool['state']}" for pool in entry.get("pools", [])]
        if name == "antigravity" and plans.no_parent_channel(runtime_config):
            notes.append("Antigravity workers have no parent channel; set `max_jobs_per_account = 1` under [antigravity] "
                         f"in {user_settings_path()} to get it.")
    share_lines = _share_doctor(getattr(args, "fix", False))
    count = len(problems)
    lines: list[ui.Line] = [[ui.bold("agt doctor"), f"  {_and(names)}, ",
                             ui.error(f"{count} problem{'s' if count != 1 else ''}") if count else ui.ok("✓ all good")], ""]
    width = max(len(HOST_LABELS.get(name, name)) for name in names + ["Sharing"]) + 3
    accounts_lines = ui.columns([_account_rows(entry) for name in names for entry in reports[name].get("accounts", [])],
                                indent=4, gap=3)
    for name in names:
        report = reports[name]
        label = HOST_LABELS.get(name, name)
        binary = [report["binary"], " ", ui.muted(f"({report['version']})")] if report.get("binary") and report.get("version") \
            else [report["binary"]] if report.get("binary") else [ui.error("✗ not installed")]
        lines.append(["  ", label, " " * (width - len(label)), *binary])
        lines += [accounts_lines.pop(0) for _ in report.get("accounts", [])]
    texts = {name: _runtime_limits(config, name) for name in names}
    every = config.max_jobs and f"; at most {config.max_jobs} jobs across every runtime" or ""
    if len(set(texts.values())) == 1:
        lines.append(["  ", "Limits", " " * (width - 6), next(iter(texts.values())) + every])
    else:
        lines += [["  ", f"Limits, {name}", " " * max(2, width - 8 - len(name)), text] for name, text in texts.items()]
        if every:
            lines.append(["  ", " " * width, every.lstrip("; ")])
    first, *rest = share_lines or ["agt share: off."]
    lines.append(["  ", "Sharing", " " * (width - 7), first.removeprefix("agt share: ").rstrip(".")])
    lines += [[" " * (width + 2), *ui.inline(line.strip())] for line in rest]
    if notes:
        lines += ["", ui.bold("Notes"), *(["  ", *ui.inline(ui.tilde(note))] for note in notes)]
    if problems:
        lines += ["", ui.bold(f"{count} problem{'s' if count != 1 else ''}")]
        for what, fix in problems:
            lines += [["  ", ui.Span("error-mark", "✗"), " ", what], ["    ", ui.muted("Fix it"), "   ", fix]]
    if not args.live:
        lines += ["", [ui.code("agt doctor --live"), ui.muted(" starts one tiny job per account and compares what each one loads.")]]
    ui.out(*lines)
    if not args.live:
        return 1 if problems else 0
    args.runtime = named or "claude"  # --live checks one runtime: the one named, else Claude
    runtime_config, runtime = config.runtime(args.runtime), get_runtime(args.runtime)
    problems = len(problems)  # the live jobs below add to it

    cwd = str(Path(args.cwd).resolve())
    # Claude's cheapest model is a known alias; other runtimes use their default unless asked.
    model = args.model or ("haiku" if args.runtime == "claude" else None)
    started = []
    for account in runtime_config.pool_accounts():
        status = jobs.create_job(
            runtime=args.runtime,
            prompt="Reply with exactly: OK",
            cwd=cwd,
            account=account.name,
            model=model,
            access="read",
            max_turns=1,
            timeout_seconds=180,
        )
        started.append((account.name, status["job_id"]))
    loaded: dict[str, dict[str, Any]] = {}
    for name, job_id in started:
        status = jobs.wait_for(job_id, 200)
        init = status.get("init") or {}
        loaded[name] = init
        print(f"\n[{name}] {status.get('outcome')}: {status.get('message') or ''}")
        if init:
            local = [server["name"] for server in init.get("mcp_servers", []) if server.get("source") != "claudeai"]
            connectors = [server["name"] for server in init.get("mcp_servers", []) if server.get("source") == "claudeai"]
            print(f"  model {init.get('model')}, permission mode {init.get('permission_mode')}")
            print(f"  MCP servers ({len(local)}): {', '.join(sorted(local)) or '-'}")
            print(f"  claude.ai connectors (account-specific): {len(connectors)}")
            print(f"  plugins: {', '.join(sorted(init.get('plugins') or [])) or '-'}")
            print(f"  skills: {len(init.get('skills') or [])}, agents: {len(init.get('agents') or [])}")
        if status.get("outcome") != "success":
            problems += 1
    names = list(loaded)
    if len(names) > 1:
        base = loaded[names[0]]
        for name in names[1:]:
            for key in ("plugins", "skills", "agents"):
                missing = sorted(set(base.get(key) or []) - set(loaded[name].get(key) or []))
                extra = sorted(set(loaded[name].get(key) or []) - set(base.get(key) or []))
                if missing or extra:
                    print(f"Difference in {key} between {names[0]} and {name}: missing {missing}, extra {extra}")
            base_mcp = {s["name"] for s in base.get("mcp_servers", []) if s.get("source") != "claudeai"}
            other_mcp = {s["name"] for s in loaded[name].get("mcp_servers", []) if s.get("source") != "claudeai"}
            if base_mcp != other_mcp:
                print(f"Difference in MCP servers between {names[0]} and {name}: {sorted(base_mcp ^ other_mcp)}")
    return 1 if problems else 0


# Jobs ------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    status = _start(args)
    if args.follow:
        from agents_router.follow import follow

        return follow(status["job_id"], None, args.max_wait, window=args.window)
    job_id = status["job_id"]
    person = ui.live_on(sys.stderr) and not args.json  # a spinner and one line at the end; the JSON for everyone else
    if not person:
        print(f"Started {job_id}", file=sys.stderr)
        status = jobs.wait_for(job_id, args.timeout + 960)
    else:
        from agents_router import jobs_view

        try:
            with ui.Live(jobs_view.run_frame(job_id)):
                status = jobs.wait_for(job_id, args.timeout + 960)
        except KeyboardInterrupt:
            ui.out(*jobs_view.stopped_lines(f"agt status {job_id}"), stream=sys.stderr)
            return 130
    result = jobs.read_result(job_id, 1_000_000)
    if result.get("state") == "done":
        jobs.claim_delivery(job_id)
    if result.get("result"):
        print(result["result"])
    if person:
        ui.out(*jobs_view.end_lines(tools.summarize(result)), stream=sys.stderr)
    else:
        print(json.dumps(tools.summarize(result), indent=2, default=str), file=sys.stderr)
    return 0 if result.get("outcome") == "success" else 1


def cmd_wait(args: argparse.Namespace) -> int:
    """Today's JSON for agents and pipes; a person at a terminal sees each job work, then a line per job and the
    next command. With one job that finished, its result, as `agt run` ends. The exit code is 0 either way."""

    person = ui.live_on(sys.stdout) and not args.json
    if not person:
        _print(tools.call("subagent_wait", {"job_ids": args.job_ids, "timeout_seconds": args.timeout}))
        return 0
    from agents_router import jobs_view

    try:
        with ui.Live(jobs_view.wait_frame(args.job_ids)):
            report = tools.call("subagent_wait", {"job_ids": args.job_ids, "timeout_seconds": args.timeout})
    except KeyboardInterrupt:
        ui.out(*jobs_view.stopped_lines("agt wait " + " ".join(args.job_ids)), stream=sys.stderr)
        return 130
    finished = report.get("finished") or []
    if len(args.job_ids) == 1 and finished:
        text = jobs.read_result(args.job_ids[0], 1_000_000).get("result")  # all of it, as `agt run` prints it
        if text:
            print(text)
        ui.out(*jobs_view.end_lines(finished[0]), stream=sys.stderr)
        return 0
    ui.out(*jobs_view.wait_lines(report))
    return 0


def cmd_follow(args: argparse.Namespace) -> int:
    from agents_router.follow import follow

    if args.from_offset is not None and args.from_offset < 0:
        raise ValueError("--from must be 0 or more")
    return follow(args.job_id, args.from_offset, args.max_wait, window=args.window)


def cmd_message(args: argparse.Namespace) -> int:
    if args.from_offset is not None:
        if not args.follow:
            raise ValueError("--from requires --follow")
        if args.from_offset < 0:
            raise ValueError("--from must be 0 or more")
    text = sys.stdin.read() if args.text == "-" else args.text
    result = tools.call("subagent_message", {"job_id": args.job_id, "text": text, "reply_to": args.reply_to})
    if args.follow:
        from agents_router.follow import follow

        new_job = result.get("new_job")
        job_id = new_job["job_id"] if new_job else args.job_id
        # A continuation has its own event log; an old job's offset cannot apply.
        start = None if new_job else args.from_offset
        return follow(job_id, start, args.max_wait, window=args.window)
    _print(result)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    _print(tools.summarize(_start(args)))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _print(tools.summarize(jobs.wait_for(args.job_id, args.wait)))
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    _print(tools.call("subagent_result", {"job_id": args.job_id, "max_chars": args.max_chars}))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    _print(tools.summarize(jobs.cancel(args.job_id)))
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    workspace = None if getattr(args, "all", False) else levels.find_workspace()
    found = jobs.list_jobs(args.limit, workspace)
    if workspace is None:
        ui.out("Jobs from every workspace" if found else "No jobs yet.")
    elif not found:
        ui.out(["No jobs started in ", ui.path(workspace.root), " or its worktrees" if workspace.checkout else "", " yet."])
    else:
        ui.out(["Jobs started in ", ui.path(workspace.root), " and its worktrees" if workspace.checkout else ""])
    if found:
        rows = []
        for job in found:
            created = time.strftime("%m-%d %H:%M", time.localtime(job.get("created_at") or 0))
            override = [ui.muted(f"  override={json.dumps(job['override'], ensure_ascii=False)}")] if job.get("override") else []
            rows.append([ui.muted(job["job_id"]), created, str(job.get("runtime")), _job_state(job),
                         [job.get("account") or "-", *override]])
        ui.out("", *ui.columns(rows, gap=3))
    others = jobs.count_elsewhere(workspace) if workspace is not None else 0
    if others:
        ui.out("", [ui.muted(f"{others} more in other workspaces: "), ui.code("agt jobs --all")])
    return 0


def _job_state(job: dict[str, Any]) -> ui.Span:
    state, outcome = str(job.get("state")), job.get("outcome")
    if state != "done":
        return ui.warn(state)
    shown = f"done, {outcome}" if outcome else "done"
    return ui.ok(shown) if outcome == "success" else ui.muted(shown) if outcome == "cancelled" else ui.error(shown)


# Interactive launcher --------------------------------------------------------


def cmd_launch(args: argparse.Namespace, extra: list[str]) -> int:
    """Open an interactive CLI on the account with the most room, with the shared setup."""

    config = load_config()
    runtime_config = config.runtime(args.runtime)
    runtime = get_runtime(args.runtime)
    if args.account:
        account = runtime_config.find(args.account)
        if account is None:
            print(f"No {args.runtime} account named '{args.account}'.")
            return 1
    else:
        ranked, _ = accounts.ranked_accounts(runtime_config)
        if not ranked:
            print(f"Every {args.runtime} account is cooling down after a usage limit.")
            return 1
        account = ranked[0]
    setup = runtime.prepare(runtime_config, account, _probe_spec(args.runtime))
    argv, env = runtime.interactive(runtime_config, account, setup)
    print(f"Starting {args.runtime} on account '{account.name}'", file=sys.stderr)
    return subprocess.call([*argv, *extra], env=env)


# Entry point -----------------------------------------------------------------


def _positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--window must be a positive integer") from None
    if seconds < 1:
        raise argparse.ArgumentTypeError("--window must be a positive integer")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = helptext.Parser(prog="agt", description="Agents Router: run tasks in other agent CLIs under your own accounts.",
                             page=helptext.ROOT)
    parser.add_argument("--version", action="version", version=f"agents-router {__version__}")
    parser.add_argument("--home", dest="agt_home", help="Use this folder instead of ~/.agents-router (sets AGT_HOME).")
    # main() takes --color out of argv wherever it is; this entry is what --help shows.
    parser.add_argument("--color", choices=ui.MODES,
                        help="auto (the default) colors output for a person at a terminal, never for an agent or a pipe.")
    sub = parser.add_subparsers(dest="command", metavar="<command>")  # no command: the home screen
    rules.add_parser(sub)
    share_commands.add_parser(sub)
    config_cli.add_parser(sub)
    synclog.add_parser(sub)

    mcp_parser = sub.add_parser("mcp", help="Run the MCP server on stdio.")
    mcp_parser.add_argument("--worker", help=argparse.SUPPRESS)

    accounts_parser = sub.add_parser("accounts", help="List, add, log in, sync, reset or remove accounts.")
    accounts_parser.add_argument(
        "action", nargs="?", choices=("list", "add", "login", "sync", "remove", "enable", "disable", "reset"),
        help="reset clears an account's usage-limit cooldown.",
    )
    accounts_parser.add_argument("name", nargs="?")
    accounts_parser.add_argument("--runtime", choices=RUNTIMES, help="Claude unless you name one; list shows every runtime.")
    accounts_parser.add_argument("--json", action="store_true", help="With list: the report as JSON.")
    accounts_parser.add_argument("--home", help="Folder for this account (default ~/.agents-router/homes/<runtime>/<name>).")
    accounts_parser.add_argument("--no-login", action="store_true")
    accounts_parser.add_argument("--cwd", default=os.getcwd(), help="Project used to resolve local MCP servers for sync.")

    setup_parser = sub.add_parser(
        "setup", help="Install the plugin into Claude Code, Codex and Antigravity and register existing account folders.",
        description="Installs the agents-router plugin through each host's own terminal commands, registers "
        "existing logged-in account folders, and lists what is left for you (logins, restarts). "
        "It never runs a login and never reads credential files.",
    )
    setup_parser.add_argument(
        "--host", action="append", choices=("claude", "codex", "antigravity", "auto"),
        help="Host to install into; repeat for several (default auto: every host that is installed).",
    )
    setup_parser.add_argument(
        "--account", action="append", default=[], metavar="[RUNTIME:]NAME=FOLDER",
        help="Register an existing, logged-in account folder and sync it, e.g. max-2=~/.claude-2 or "
        "codex:plus-2=~/.codex-2 (the runtime is guessed from the folder when left out). Repeatable.",
    )
    setup_parser.add_argument("--dry-run", action="store_true", help="Print the plan; change nothing (read-only checks still run).")
    from agents_router import setup_screen

    setup_screen.add_arguments(setup_parser)
    setup_parser.add_argument(
        "--claude-route", choices=("auto", "cli", "settings"), default="auto",
        help="auto: `claude plugin ...`, or settings.json when the claude command is missing or fails. "
        "settings: only declare the plugin in settings.json (Claude Code installs it on its next start).",
    )

    update_parser = sub.add_parser(
        "update", help="Reinstall agt from the source you installed it from, and update the plugin in every host that has it.",
        description="Reinstalls the agt package with the tool that installed it (pipx, uv or pip), from the same "
        "source (GitHub, or the folder or archive you installed from), and updates "
        "the agents-router plugin through each host's own terminal commands.",
    )
    update_parser.add_argument("--host", action="append", choices=("claude", "codex", "antigravity", "auto"),
                               help="Host to update; repeat for several (default auto: every host that has the plugin).")
    update_parser.add_argument("--no-package", action="store_true", help="Only update the plugins, not the agt package.")
    update_parser.add_argument("--dry-run", action="store_true", help="Print the plan; change nothing (read-only checks still run).")

    doctor = sub.add_parser("doctor", help="Check binaries, logins and shared setup.")
    doctor.add_argument("--runtime", choices=RUNTIMES, help="Check only this runtime (default: every one installed).")
    doctor.add_argument("--json", action="store_true", help="The report as JSON.")
    doctor.add_argument("--live", action="store_true", help="Run one tiny job per account (uses a little quota).")
    doctor.add_argument("--model", help="Model for --live jobs (default: haiku for claude, the CLI's default otherwise).")
    doctor.add_argument("--cwd", default=os.getcwd())
    doctor.add_argument("--fix", action="store_true",
                        help="Repair agt share: its state from the last good copy, the launcher, the watcher and its entries.")

    for name, help_text in (("run", "Run a task and print the result."), ("start", "Start a task in the background.")):
        _job_arguments(sub.add_parser(name, help=help_text))
    run_parser = sub.choices["run"]
    run_parser.add_argument(
        "--follow", action="store_true",
        help="Print the worker's steps as they happen, then the result (see agt follow).",
    )
    run_parser.add_argument("--max-wait", type=float, default=540, help="With --follow: stop watching after this many seconds (default 540).")
    run_parser.add_argument("--json", action="store_true",
                            help="Print the JSON summary on stderr, as for an agent or a pipe, instead of a spinner and one line.")
    follow_parser = sub.add_parser(
        "follow", help="Print a job's steps as they happen, then its result.",
        description="Prints one line per worker event and ends with one marker: DONE or FAILED (with the result), "
        "STILL RUNNING (with the command to keep watching) or QUESTION (with the commands to answer and continue).",
    )
    follow_parser.add_argument("job_id")
    follow_parser.add_argument("--from", dest="from_offset", type=int, metavar="OFFSET",
                               help="Continue at this byte offset, as printed by an earlier follow (default: from the start).")
    follow_parser.add_argument("--max-wait", type=float, default=540, help="Stop watching after this many seconds (default 540).")

    status = sub.add_parser("status", help="Show a job's state.")
    status.add_argument("job_id")
    status.add_argument("--wait", type=float, default=0)
    result = sub.add_parser("result", help="Show a job's result.")
    result.add_argument("job_id")
    result.add_argument("--max-chars", type=int, default=20000)
    cancel = sub.add_parser("cancel", help="Stop a job.")
    cancel.add_argument("job_id")
    wait = sub.add_parser("wait", help="Wait until a job finishes or sends a message.")
    wait.add_argument("job_ids", nargs="+")
    wait.add_argument("--timeout", type=int, default=1800)
    wait.add_argument("--json", action="store_true", help="Print the JSON report, as for an agent or a pipe.")
    message = sub.add_parser("message", help="Send a follow-up or an answer to a job.")
    message.add_argument("job_id")
    message.add_argument("text", help="The message. Use '-' to read it from stdin.")
    message.add_argument("--reply-to")
    message.add_argument("--follow", action="store_true",
                         help="Follow the running job or its new continuation, with an authenticated header and status.")
    message.add_argument("--from", dest="from_offset", type=int, metavar="OFFSET",
                         help="With --follow: continue at this event offset if the same job is still running; "
                         "a new continuation always starts from the beginning.")
    message.add_argument("--max-wait", type=float, default=540,
                         help="With --follow: stop watching after this many seconds (default 540).")
    from agents_router.follow import DEFAULT_WINDOW

    for watcher in (run_parser, follow_parser, message):
        watcher.add_argument("--window", type=_positive_seconds, nargs="?", const=DEFAULT_WINDOW, metavar="SECONDS",
                             help="Return after this many seconds and repeat the window in continuations "
                             "(bare --window: 60; omitted: keep --max-wait behavior).")
    jobs_parser = sub.add_parser("jobs", help="List recent jobs started in this workspace.")
    jobs_parser.add_argument("--limit", type=int, default=20)
    jobs_parser.add_argument("--all", action="store_true", help="Every job, from every workspace.")

    launcher = sub.add_parser("launch", help="Open an interactive session on the best account with the shared setup.")
    launcher.add_argument("--runtime", default="claude", choices=RUNTIMES)
    launcher.add_argument("--account")

    hook = sub.add_parser("hook", help="Claude Code hook commands (see hooks/hooks.json).")
    hook.add_argument(
        "action", choices=("await", "pending", "inbox"),
        help="await: wake the main session when a job finishes or asks something. "
        "pending: list uncollected results at session start. inbox: a worker's follow-ups.",
    )
    hook.add_argument("job_id", nargs="?", help="The worker's job (inbox only).")

    runner = sub.add_parser("_runner", help=argparse.SUPPRESS)
    runner.add_argument("job_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # Everything after `agt launch [options] --` goes to the launched CLI.
    extra: list[str] = []
    if argv[:1] == ["launch"] and "--" in argv:
        index = argv.index("--")
        argv, extra = argv[:index], argv[index + 1 :]
    if argv[:1] == ["help"]:  # `agt help [command]` is `agt [command] --help`
        argv = [*argv[1:], "--help"]
    try:
        argv, color = ui.take_color_flag(argv)
    except ValueError as exc:
        ui.out(*helptext.mistake_lines("agt", str(exc)), stream=sys.stderr)
        return 2
    ui.set_color(color)
    ui.ARGV[:] = argv
    args = build_parser().parse_args(argv)
    if getattr(args, "agt_home", None):
        os.environ["AGT_HOME"] = args.agt_home
    if args.command == "hook":
        from agents_router.hooks import main as run_hook

        return run_hook(args.action, args.job_id)
    try:
        if args.command == "mcp":
            from agents_router.mcp_server import main as serve

            return serve(args.worker)
        if args.command == "_runner":
            from agents_router.runner import main as run_job

            return run_job(args.job_id)
        handlers = {
            "rules": rules.command,
            "share": share_commands.command,
            "config": config_cli.command,
            "log": synclog.command,
            "accounts": cmd_accounts,
            "setup": cmd_setup,
            "update": cmd_update,
            "doctor": cmd_doctor,
            "run": cmd_run,
            "start": cmd_start,
            "status": cmd_status,
            "result": cmd_result,
            "cancel": cmd_cancel,
            "jobs": cmd_jobs,
            "wait": cmd_wait,
            "message": cmd_message,
            "follow": cmd_follow,
        }
        if args.command is None:
            from agents_router import home

            return home.show()
        if args.command == "launch":
            return cmd_launch(args, extra)
        return handlers[args.command](args)
    except ui.Mistake as exc:
        ui.out(*helptext.mistake_lines(exc.prog, exc.message, exc.suggestion, exc.typed, exc.hint), stream=sys.stderr)
        return 2
    except (ValueError, RuntimeError, tools.ToolError) as exc:
        ui.out(*helptext.mistake_lines("agt", str(exc)), stream=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
