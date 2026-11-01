"""`agt run|start --role <name>`: run a task as a role and let agt pick the providers.

A read role starts one ordinary job per usable provider, all at once, tied together by
one group id (`g-<hex>`); each job goes through the usual admission on its own (caps,
cooldowns, usage ceiling, the nested-delegation depth). An edit role starts one job: on
the first provider in its order that `limits.admit` accepts, preferring one below the
runtime's usage ceiling. A provider whose account is unknown, logged out, turned off or
cooling down is skipped with the reason; with none left the call fails at once (exit 1).
An edit job that fails mid-run is not retried on the next provider: the main session
judges partial edits.

Every role call gets a group, even an edit role's single job, so `agt follow`, `agt
cancel` and the relays' end markers work the same way for both.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
import time
from typing import Any

from agents_router import accounts, jobs, limits, quota, ui
from agents_router.config import Account, Config, RuntimeConfig, load_config, session_folder, set_session
from agents_router.hosts import detect_host
from agents_router.roles import Provider, Role, RoleError, unknown_role


class NoProvider(RuntimeError):
    """No provider of the role could start (exit 1, like a job that failed)."""


@dataclass
class Check:
    """What agt knows about a provider before asking admission."""

    account: str | None = None  # None: agt picks the runtime's account with room, as for any job
    reason: str | None = None  # why it can't (or shouldn't) take the job
    quota: bool = False  # the reason is quota: worth the rebalance hint
    until: float | None = None  # cooling down until then
    soft: bool = False  # above the usage ceiling: only when no provider below it is admitted


@dataclass
class Skip:
    provider: Provider
    position: int
    reason: str
    quota: bool = False
    until: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"provider": str(self.provider), "position": self.position, "reason": self.reason, "quota": self.quota}


@dataclass
class Launch:
    role: Role
    record: dict[str, Any]
    started: list[dict[str, Any]]  # job statuses, in provider order

    @property
    def group(self) -> str:
        return self.record["group"]


# Checks ------------------------------------------------------------------------------


def check_flags(args: argparse.Namespace) -> None:
    clashes = [flag for flag, value in (("--runtime", args.runtime), ("--account", args.account),
                                        ("--resume-job", getattr(args, "resume_job", None))) if value]
    if clashes:
        raise RoleError(f"--role {args.role} picks the runtime and account from [roles.{args.role}]; "
                        f"leave out {' and '.join(clashes)}, or run without --role")
    if args.model:
        raise RoleError(f"--role {args.role} takes each provider's model from [roles.{args.role}]; leave out --model "
                        "(`agt roles config --set-model <provider>=<model>` changes it)")


def load_role(name: str, cwd: str) -> tuple[Config, Role]:
    config = load_config(cwd=cwd)
    role = config.roles.get(name)
    if role is None:
        raise unknown_role(name, config.roles)
    if not role.enabled:
        layer = role.sources.get("enabled")
        where = ui.tilde(str(layer.path)) if layer is not None else "agt's settings"
        raise RoleError(f"role {name} is off (enabled = false in {where}); "
                        f"`agt roles config --enable {name}` turns it back on.")
    return config, role


def _clock(when: float) -> str:
    return time.strftime("%H:%M", time.localtime(when))


def precheck(config: Config, provider: Provider, now: float) -> Check:
    """The provider's account, and why it can't take a job now, if agt can tell before admission."""

    runtime = config.runtime(provider.runtime)
    name = provider.account  # a short provider (`codex`) leaves the account to agt's usual choice
    model = provider.model or runtime.default_model
    try:
        ranked, earliest = accounts.ranked_accounts(runtime, pinned=name, model=model,
                                                    pool=quota.pool_for(runtime, model), now=now)
    except ValueError as exc:
        if name and runtime.find(name) is None:
            return Check(name, f"no {provider.runtime} account named {name} (removed, renamed or logged out)")
        return Check(name, str(exc))
    who = name or f"every {provider.runtime} account"
    if not ranked:
        if earliest:
            return Check(name, f"{who} at its limit until {_clock(earliest)}", quota=True, until=earliest)
        if not name:
            return Check(name, f"no {provider.runtime} account can take jobs from here")
        account = runtime.find(name)
        if account is not None and not account.enabled:
            return Check(name, f"{name} is turned off in agt's settings")
        return Check(name, f"{name} doesn't allow model {model}")
    if name:  # a pinned account must be logged in; a short provider leaves a logged-out one to the runner's failover
        account = ranked[0]
        if logged_in(runtime, account) is False:
            return Check(name, f"{name} is logged out · log in with {_login_fix(provider.runtime, account.name)}")
    record = accounts.account_record(runtime, ranked[0], accounts.load_usage(provider.runtime))
    used = limits.utilization(record, now)
    if used is not None and used >= runtime.usage_ceiling:  # ranked[0] is the least full: every one is above
        left = max(0, round((1 - used) * 100))
        return Check(name, f"{name or ranked[0].name} {left}% left · above the "
                     f"{round(runtime.usage_ceiling * 100)}% usage ceiling",
                     quota=True, until=_window_reset(record, now), soft=True)
    return Check(name)


def logged_in(runtime: RuntimeConfig, account: Account) -> bool | None:
    """Whether the account is logged in, without a model request; None when agt can't tell cheaply.

    A found Claude profile carries its cached `claude auth status` answer; a registered Claude
    account asks the same, through the same cache (ten minutes, dropped on a login or an auth
    failure). Codex asks `codex login status` (a local file read, no cache: `agt accounts login`
    drops only Claude's). An extra Antigravity account has a login file. Antigravity's own login
    (`agy models` can take a minute) and Kiro's are left to the job.
    """

    from agents_router import profiles
    from agents_router.runtimes import get_runtime

    if account.found:
        return account.logged_in
    adapter = get_runtime(runtime.runtime)
    if runtime.runtime == "antigravity":
        return None if account.is_primary else adapter.logged_in(account)
    if runtime.runtime not in ("claude", "codex") or not adapter.find_binary(runtime):
        return None

    def check(_folder: Path) -> bool | None:
        status = adapter.auth_status(runtime, account)
        return None if status.get("error") else bool(status.get("loggedIn"))

    return profiles.logged_in(account.home, check) if runtime.runtime == "claude" else check(account.home)


def _login_fix(runtime: str, account: str) -> str:
    """The login command `agt doctor` suggests."""

    from agents_router.cli import _login_fix as fix

    return ui.plain(fix(runtime, account))


def _window_reset(record: dict[str, Any] | None, now: float) -> float | None:
    """When the fullest open usage window resets, if known."""

    open_windows = [window for window in ((record or {}).get("windows") or {}).values()
                    if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float))
                    and not (isinstance(window.get("resets_at"), (int, float)) and window["resets_at"] <= now)]
    if not open_windows:
        return None
    resets = max(open_windows, key=lambda window: window["utilization"]).get("resets_at")
    return float(resets) if isinstance(resets, (int, float)) else None


def _refusal(exc: Exception) -> str:
    """A limit refusal's first sentence: the cap and where it comes from, without the advice for single jobs."""

    text = str(exc).split("\n", 1)[0].replace(limits.OVERRIDE_HINT, "").strip()
    head, sep, _ = text.partition(". ")
    return head + "." if sep else text


def failure(role: Role, skips: list[Skip]) -> str:
    reasons = "; ".join(f"{skip.provider} — {skip.reason}" for skip in skips)
    if skips and all(skip.quota for skip in skips):
        resets = [skip.until for skip in skips if skip.until]
        when = f"; the earliest resets around {_clock(min(resets))}" if resets else ""
        return f"every {role.name} provider is at its usage limit{when} ({reasons})"
    return f"no {role.name} provider can run: {reasons}" if reasons else f"role {role.name} has no providers"


# Starting ------------------------------------------------------------------------------


def _task(args: argparse.Namespace, role: Role) -> str:
    task = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not task or not task.strip():
        raise ValueError("prompt is empty")
    preface = role.prompt()
    if role.prompt_file and preface is None:
        raise RoleError(f"role {role.name}: can't read its prompt_file {role.prompt_file}")
    return f"{preface.strip()}\n\n{task}" if preface and preface.strip() else task


def launch(args: argparse.Namespace) -> Launch:
    """Check the call, pick the providers and start their jobs under one new group. Raises RoleError (exit 2)."""

    check_flags(args)
    cwd = str(Path(args.cwd).resolve())
    config, role = load_role(args.role, cwd)
    prompt = _task(args, role)
    set_session(config, session_folder(detect_host()))  # as create_job does: the session's own profile is never used
    group = jobs.new_group_id()
    now = time.time()
    access = "edit" if role.access == "edit" else "read"
    started: list[tuple[int, Provider, dict[str, Any]]] = []
    skips: list[Skip] = []

    def create(provider: Provider, account: str | None) -> dict[str, Any]:
        return jobs.create_job(
            runtime=provider.runtime, prompt=prompt, cwd=cwd, account=account, model=provider.model,
            effort=provider.effort or args.effort, override=args.override, access=access, max_turns=args.max_turns,
            timeout_seconds=args.timeout, add_dirs=[str(Path(item).resolve()) for item in args.add_dir], group=group,
        )

    def skip(position: int, provider: Provider, check: Check, reason: str | None = None) -> None:
        skips.append(Skip(provider, position, reason or check.reason or "unusable", check.quota, check.until))

    try:
        if role.access == "edit":
            fallback: tuple[int, Provider, Check] | None = None
            for position, provider in enumerate(role.providers, 1):
                check = precheck(config, provider, now)
                if check.reason and not check.soft:
                    skip(position, provider, check)
                    continue
                try:
                    admission = limits.admit(config, provider.runtime, account=check.account, model=provider.model,
                                             effort=provider.effort or args.effort, override=args.override, now=now)
                except limits.LimitError as exc:
                    skip(position, provider, check, check.reason if check.soft else _refusal(exc))
                    continue
                if check.soft or admission.account is None:
                    # Admitted, but above the ceiling or only to wait in the queue: used when nothing better is.
                    fallback = fallback or (position, provider, check)
                    skip(position, provider, check, check.reason or "every account busy; it would wait in the queue")
                    continue
                try:
                    started.append((position, provider, create(provider, check.account)))
                    break
                except limits.LimitError as exc:  # taken since admit looked: try the next one
                    skip(position, provider, check, _refusal(exc))
            if not started and fallback is not None:
                position, provider, check = fallback
                try:
                    started.append((position, provider, create(provider, check.account)))
                    skips = [item for item in skips if item.position != position]
                except limits.LimitError as exc:
                    skips = [item if item.position != position else Skip(provider, position, _refusal(exc), check.quota)
                             for item in skips]
        else:
            for position, provider in enumerate(role.providers, 1):
                check = precheck(config, provider, now)
                if check.reason and not check.soft:
                    skip(position, provider, check)
                    continue
                try:
                    started.append((position, provider, create(provider, check.account)))
                except limits.LimitError as exc:
                    skip(position, provider, check, check.reason if check.soft else _refusal(exc))
    finally:
        record = {
            "group": group,
            "role": role.name,
            "access": role.access,
            "created_at": now,
            "cwd": cwd,
            "providers": len(role.providers),
            "jobs": [{"job_id": status["job_id"], "provider": str(provider), "position": position}
                     for position, provider, status in started],
            "skipped": [item.to_dict() for item in sorted(skips, key=lambda item: item.position)],
            "failure": None if started else failure(role, skips),
        }
        jobs.write_group(record)
    return Launch(role, record, [status for _, _, status in started])


# Commands ------------------------------------------------------------------------------


def _plain_lines(record: dict[str, Any]) -> list[str]:
    lines = [f"role {record['role']} · {record['access']} · group {record['group']}"]
    lines += [f"  [{number}] {item['provider']} · job {item['job_id']}" for number, item in enumerate(record["jobs"], 1)]
    lines += [f"  skipped {item['provider']} — {item['reason']}" for item in record["skipped"]]
    if any(item.get("quota") for item in record["skipped"]):
        lines.append(f'  hint: to move role {record["role"]} off a provider that keeps running out, ask your agent to '
                     '"rebalance my roles" (orchestrator-rebalance)')
    return lines


def run(args: argparse.Namespace) -> int:
    """`agt run --role`: with --follow, the group's events and its end marker; else the result when all jobs end."""

    from agents_router.follow import follow_group, group_result

    launched = launch(args)
    if args.follow:
        return follow_group(launched.group, None, args.max_wait, window=args.window)
    record = launched.record
    print("\n".join(_plain_lines(record)), file=sys.stderr)
    if not record["jobs"]:
        print(f"FAILED · {record['failure']}", file=sys.stderr)
        return 1
    for item in record["jobs"]:
        jobs.wait_for(item["job_id"], args.timeout + 960)
    text, succeeded = group_result(record, claim=True)
    if text:
        print(text)
    total = len(record["jobs"])
    print(f"{'DONE' if succeeded else 'FAILED'} · {succeeded} of {total} succeeded · group {record['group']}",
          file=sys.stderr)
    return 0 if succeeded else 1


def start(args: argparse.Namespace) -> dict[str, Any]:
    """`agt start --role`: the group and its jobs, started in the background."""

    from agents_router import tools

    launched = launch(args)
    record = launched.record
    if not launched.started:
        raise NoProvider(record["failure"])
    return {
        "group": record["group"],
        "role": record["role"],
        "access": record["access"],
        "jobs": [{**tools.summarize(status), "provider": item["provider"]}
                 for status, item in zip(launched.started, record["jobs"])],
        "skipped": record["skipped"],
        "follow": f"agt follow {record['group']}",
    }
