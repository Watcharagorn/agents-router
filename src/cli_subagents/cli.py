"""`csa`: command-line front end for CLI Subagents."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from cli_subagents import __version__, accounts, jobs, tools
from cli_subagents.config import NAME_RE, Account, homes_dir, load_config, save_config
from cli_subagents.runtimes import AVAILABLE as RUNTIMES, get_runtime
from cli_subagents.runtimes.base import ACCESS_LEVELS, JobSpec


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
        access=args.access,
        max_turns=args.max_turns,
        timeout_seconds=args.timeout,
        add_dirs=[str(Path(item).resolve()) for item in args.add_dir],
        resume_job=args.resume_job,
    )


# Accounts --------------------------------------------------------------------


def cmd_accounts(args: argparse.Namespace) -> int:
    config = load_config()
    runtime_config = config.runtime(args.runtime)
    runtime = get_runtime(args.runtime)
    action = args.action or "list"

    if action == "list":
        _print(tools.account_report(args.runtime))
        return 0

    if not args.name or not NAME_RE.match(args.name) or args.name == "primary":
        print("Give the account a short name: letters, digits, dot, dash or underscore ('primary' is reserved).")
        return 2

    if action == "add":
        if runtime_config.find(args.name):
            print(f"{args.runtime} account '{args.name}' already exists.")
            return 1
        home = Path(args.home).expanduser().resolve() if args.home else homes_dir() / args.runtime / args.name
        if os.path.normcase(str(home)) == os.path.normcase(str(runtime_config.primary.resolve())):
            print("That folder is the primary setup. Extra accounts need their own folder.")
            return 1
        home.mkdir(parents=True, exist_ok=True)
        runtime_config.accounts.append(Account(name=args.name, home=home))
        save_config(config)
        print(f"Added {args.runtime} account '{args.name}' with folder {home}")
        account = runtime_config.find(args.name)
        assert account is not None
        report = runtime.prepare(runtime_config, account, _probe_spec(args.runtime))
        _print_setup(report)
        if args.no_login:
            print(f"Log in later with: csa accounts login {args.name} --runtime {args.runtime}")
            return 0
        return _login(runtime_config, runtime, account)

    account = runtime_config.find(args.name)
    if account is None:
        print(f"No {args.runtime} account named '{args.name}'. See: csa accounts list")
        return 1

    if action == "login":
        return _login(runtime_config, runtime, account)
    if action == "sync":
        _print_setup(runtime.prepare(runtime_config, account, _probe_spec(args.runtime, args.cwd)))
        return 0
    if action == "remove":
        runtime_config.accounts = [item for item in runtime_config.accounts if item.name != args.name]
        save_config(config)
        print(f"Removed '{args.name}' from the config. Its folder was left as is: {account.home}")
        return 0
    if action in ("enable", "disable"):
        account.enabled = action == "enable"
        save_config(config)
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
    return subprocess.call(argv, env=env)


# Doctor ----------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    runtime_config = config.runtime(args.runtime)
    runtime = get_runtime(args.runtime)
    report = tools.account_report(args.runtime)
    _print(report)
    problems = 0
    if not report["binary"]:
        print(f"\nProblem: no {args.runtime} binary found. Install it or set [{args.runtime}] binary in the config.")
        return 1
    for entry in report["accounts"]:
        auth = entry.get("auth") or {}
        if not auth.get("loggedIn"):
            problems += 1
            print(f"Problem: account '{entry['name']}' is not logged in. Run: csa accounts login {entry['name']}")
    if not args.live:
        print("\nAdd --live to start one tiny job per account and compare what each one loads.")
        return 1 if problems else 0

    cwd = str(Path(args.cwd).resolve())
    started = []
    for account in runtime_config.worker_accounts():
        status = jobs.create_job(
            runtime=args.runtime,
            prompt="Reply with exactly: OK",
            cwd=cwd,
            account=account.name,
            model=args.model,
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
    print(f"Started {status['job_id']}", file=sys.stderr)
    status = jobs.wait_for(status["job_id"], args.timeout + 960)
    result = jobs.read_result(status["job_id"], 1_000_000)
    if result.get("result"):
        print(result["result"])
    print(json.dumps(tools.summarize(result), indent=2, default=str), file=sys.stderr)
    return 0 if result.get("outcome") == "success" else 1


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
    for job in jobs.list_jobs(args.limit):
        created = time.strftime("%m-%d %H:%M", time.localtime(job.get("created_at") or 0))
        print(f"{job['job_id']}  {created}  {job.get('runtime')}  {job.get('state')}/{job.get('outcome') or '-'}  {job.get('account') or '-'}")
    return 0


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="csa", description="CLI Subagents: run tasks in other agent CLIs under your own accounts.")
    parser.add_argument("--version", action="version", version=f"cli-subagents {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("mcp", help="Run the MCP server on stdio.")

    accounts_parser = sub.add_parser("accounts", help="List, add, log in, sync or remove accounts.")
    accounts_parser.add_argument("action", nargs="?", choices=("list", "add", "login", "sync", "remove", "enable", "disable"))
    accounts_parser.add_argument("name", nargs="?")
    accounts_parser.add_argument("--runtime", default="claude", choices=RUNTIMES)
    accounts_parser.add_argument("--home", help="Folder for this account (default ~/.cli-subagents/homes/<runtime>/<name>).")
    accounts_parser.add_argument("--no-login", action="store_true")
    accounts_parser.add_argument("--cwd", default=os.getcwd(), help="Project used to resolve local MCP servers for sync.")

    doctor = sub.add_parser("doctor", help="Check binaries, logins and shared setup.")
    doctor.add_argument("--runtime", default="claude", choices=RUNTIMES)
    doctor.add_argument("--live", action="store_true", help="Run one tiny job per account (uses a little quota).")
    doctor.add_argument("--model", default="haiku")
    doctor.add_argument("--cwd", default=os.getcwd())

    for name, help_text in (("run", "Run a task and print the result."), ("start", "Start a task in the background.")):
        _job_arguments(sub.add_parser(name, help=help_text))

    status = sub.add_parser("status", help="Show a job's state.")
    status.add_argument("job_id")
    status.add_argument("--wait", type=float, default=0)
    result = sub.add_parser("result", help="Show a job's result.")
    result.add_argument("job_id")
    result.add_argument("--max-chars", type=int, default=20000)
    cancel = sub.add_parser("cancel", help="Stop a job.")
    cancel.add_argument("job_id")
    jobs_parser = sub.add_parser("jobs", help="List recent jobs.")
    jobs_parser.add_argument("--limit", type=int, default=20)

    launcher = sub.add_parser("launch", help="Open an interactive session on the best account with the shared setup.")
    launcher.add_argument("--runtime", default="claude", choices=RUNTIMES)
    launcher.add_argument("--account")

    runner = sub.add_parser("_runner", help=argparse.SUPPRESS)
    runner.add_argument("job_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Everything after `csa launch [options] --` goes to the launched CLI.
    extra: list[str] = []
    if argv[:1] == ["launch"] and "--" in argv:
        index = argv.index("--")
        argv, extra = argv[:index], argv[index + 1 :]
    args = build_parser().parse_args(argv)
    try:
        if args.command == "mcp":
            from cli_subagents.mcp_server import main as serve

            return serve()
        if args.command == "_runner":
            from cli_subagents.runner import main as run_job

            return run_job(args.job_id)
        handlers = {
            "accounts": cmd_accounts,
            "doctor": cmd_doctor,
            "run": cmd_run,
            "start": cmd_start,
            "status": cmd_status,
            "result": cmd_result,
            "cancel": cmd_cancel,
            "jobs": cmd_jobs,
        }
        if args.command == "launch":
            return cmd_launch(args, extra)
        return handlers[args.command](args)
    except (ValueError, RuntimeError, tools.ToolError) as exc:
        print(f"csa: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
