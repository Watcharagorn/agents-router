"""`agt` with no command: where things stand, and the next thing to do.

Before setup it says what agt does and how to start. After setup it shows one row each for accounts,
sharing and jobs, then each thing that needs you with its command. It reads agt's own records and runs
each CLI's own login check (no model request) in parallel, for at most two seconds; a check that runs
over shows "not checked", and `agt doctor` has the full answer. It always exits 0: it's a screen you
open to look around, while `agt share status` and `agt doctor` exit 1 when something needs you.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from agents_router import __version__, hooks, jobs, ui
from agents_router.config import Config, load_config, user_settings_path
from agents_router.hosts import HOST_LABELS
from agents_router.runtimes import AVAILABLE as RUNTIMES, get_runtime

LOGIN_SECONDS = 2.0
RUNNING = ("queued", "starting", "running")


def set_up(config: Config) -> bool:
    """Whether agt was set up here: its settings exist, it has extra accounts, or it has run a job."""

    if user_settings_path().exists():
        return True
    if any(config.runtime(name).accounts for name in RUNTIMES):  # extra accounts, beyond the primary
        return True
    return bool(jobs.list_jobs(1))


def _logins(config: Config) -> dict[str, list[tuple[str, bool | None]]]:
    """Each installed runtime's accounts with whether they're logged in; None where the check ran over."""

    found: dict[str, list[tuple[str, bool | None]]] = {}
    checks = []
    for name in RUNTIMES:
        runtime_config = config.runtime(name)
        runtime = get_runtime(name)
        if runtime.find_binary(runtime_config) is None:
            continue
        found[name] = []
        for account in runtime_config.listed_accounts():
            if not account.enabled:
                continue
            slot: dict[str, Any] = {}
            found[name].append((account.name, None))
            index = len(found[name]) - 1

            def check(runtime=runtime, runtime_config=runtime_config, account=account, slot=slot):
                try:
                    slot["in"] = bool(runtime.auth_status(runtime_config, account).get("loggedIn"))
                except Exception:  # a broken check reads as "not checked", never a failed screen
                    pass

            thread = threading.Thread(target=check, daemon=True)  # daemon: a slow check never holds up exit
            thread.start()
            checks.append((name, index, account.name, slot, thread))
    deadline = time.monotonic() + LOGIN_SECONDS
    for name, index, account, slot, thread in checks:
        thread.join(max(0.0, deadline - time.monotonic()))
        found[name][index] = (account, slot.get("in"))
    return found


def _sharing() -> tuple[str | None, list[tuple[str, str]]]:
    """The Sharing row (None when sharing was never turned on) and its next steps; a broken background watcher's
    fix comes first, since nothing syncs on its own until it's fixed."""

    from agents_router.share import settings, state, watch

    try:
        saved = settings.load()
    except settings.SettingsError as exc:
        return f"can't read its settings: {exc}", [("Fix the settings file", "agt doctor")]
    if not saved.enabled:
        return None, []
    try:
        shared = sum(1 for key, record in state.load().items.items()
                     if key.startswith(("agents/", "skills/")) and record.get("version"))
    except state.StateError:
        return "on, but its records can't be read", [("Restore them", "agt doctor --fix")]
    waiting = watch.wait_count(saved)  # as status counts them: the background watcher is one
    row = f"on, {shared} {'agent or skill' if shared == 1 else 'agents and skills'} shared"
    steps = []
    if waiting:
        row += f"; {waiting} wait{'s' if waiting == 1 else ''} for you"
    problems = watch.watcher_problems(saved, check_loaded=False)
    if problems:
        fix = problems[0][0] if len(problems) == 1 else "repair"
        steps.append((f"{fix.capitalize()} the background watcher", "agt doctor --fix"))
    others = watch.wait_count(saved, watcher=False)
    if others:
        steps.append((f"See what agt share waits on ({others})", "agt share status"))
    return row, steps


def _jobs() -> tuple[str, list[tuple[str, str]]]:
    recent = jobs.list_jobs(200)
    running = sum(1 for job in recent if job.get("state") in RUNNING)
    unread = hooks.pending_jobs(os.getcwd())
    steps = []
    if len(unread) == 1:
        steps.append(("Read the finished job", f"agt result {unread[0]['job_id']}"))
    elif unread:
        steps.append((f"Read the {len(unread)} finished jobs", "agt jobs"))
    if not recent:
        return "none yet", steps
    parts = [f"{running} running" if running else "none running"]
    if unread:
        parts.append(f"{len(unread)} finished that you haven't read")
    elif not running:
        last = max(float(job.get("created_at") or 0) for job in recent)
        parts.append("the last one started " + time.strftime("%m-%d %H:%M", time.localtime(last)))
    return (", " if unread else "; ").join(parts), steps


def _found_here(config: Config) -> list[list[ui.Piece]]:
    from agents_router.share import providers

    by_cli: dict[str, list[str]] = {}
    for cli, folder in providers.discovered_folders(config):
        by_cli.setdefault(cli, []).append(ui.short_path(folder))
    return ui.columns([[HOST_LABELS.get(cli, cli), ui.muted(", ".join(paths))] for cli, paths in by_cli.items()], gap=3)


def show() -> int:
    config = load_config()
    if not set_up(config):
        lines: list[ui.Line] = [
            [ui.bold(f"agt {__version__}"), "  not set up yet"], "",
            "agt lets your coding agent hand tasks to the other agent CLIs you have, each under your own login, "
            "and keeps their agents and skills the same.",
        ]
        found = _found_here(config)
        if found:
            lines += ["", ui.bold("Found here"), *found]
        lines += ["", ui.bold("Start here"),
                  *ui.columns([[ui.muted("See what setup would do"), ui.cmd("agt setup --dry-run")],
                               [ui.muted("Set it up"), ui.cmd("agt setup")]], gap=3)]
        ui.out(*lines)
        return 0
    logins = _logins(config)
    sharing, share_steps = _sharing()
    job_row, job_steps = _jobs()
    steps: list[tuple[str, str]] = []
    accounts: list[ui.Piece] = []
    for runtime, found in logins.items():
        if accounts:
            accounts.append(" · ")
        shown = []
        for name, logged_in in found:
            if logged_in is False:
                shown.append(ui.warn(f"{name} needs a login"))
                flag = "" if runtime == "claude" else f" --runtime {runtime}"
                steps.append((f"Log in to {runtime} as {name}", f"agt accounts login {name}{flag}"))
            elif logged_in is None:
                shown.append(ui.muted(f"{name} not checked"))
            else:
                shown.append(name)
        accounts.append(f"{runtime}: ")
        for i, piece in enumerate(shown):
            accounts += [", "] if i else []
            accounts.append(piece)
    watcher = [step for step in share_steps if step[1] == "agt doctor --fix" and step[0].endswith("background watcher")]
    steps = watcher + steps + [step for step in share_steps if step not in watcher] + job_steps
    rows = [[ui.muted("Accounts"), accounts or ui.muted("no agent CLI found")],
            [ui.muted("Sharing"), sharing if sharing is not None else [ui.muted("off; "), ui.code("agt share enable --dry-run"),
                                                                        ui.muted(" shows what it would share")]],
            [ui.muted("Jobs"), job_row]]
    if steps:
        status: ui.Piece = ui.warn(f"{len(steps)} thing{'' if len(steps) == 1 else 's'} to do")
    else:
        status = ui.ok("✓ all good, nothing needs you")
    lines = [[ui.bold(f"agt {__version__}"), "  ", status], "", *ui.columns(rows, gap=3)]
    if steps:
        lines += ["", ui.bold("Next"), *ui.columns([[ui.muted(label), ui.cmd(command)] for label, command in steps], gap=3),
                  "", [ui.code("agt --help"), ui.muted(" lists every command; "), ui.code("agt doctor"),
                       ui.muted(" runs the full check.")]]
    ui.out(*lines)
    return 0
