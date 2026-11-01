"""Job records on disk: create, start, inspect, wait, cancel, list, clean up.

One folder per job under `~/.agents-router/jobs/<job_id>/`:

    spec.json      what to run (written once)
    status.json    live state, written atomically by the runner
    events.jsonl   raw events from the worker CLI
    result.md      final answer text
    runner.log     runner stdout/stderr (diagnostics)
    cancel         marker file; its presence asks the runner to stop
    notify.pending the result waits for delivery; renamed to notify.claimed
                   by whoever delivers it first (hook, subagent_wait, subagent_result)
    notify.lock    held while either marker changes

A role call (`agt run --role`) starts one ordinary job per provider and ties them
together with a group id `g-<hex>`: each member's status.json carries `group`, and
`~/.agents-router/state/groups/<group>.json` keeps the role, the members in provider
order, the providers skipped with their reasons, and the cursors `agt follow <group>
--from N` continues at.
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from typing import Any, Callable, Iterator

from agents_router import levels, limits, quota, synclog
from agents_router.config import jobs_dir, load_config, session_folder, set_session, state_dir
from agents_router.fsutil import (
    file_lock,
    kill_tree,
    make_private_dir,
    pid_alive,
    python_executable,
    read_json,
    read_text,
    same_process,
    spawn_detached,
    write_json,
    write_text,
)
from agents_router.hosts import detect_host, host_context
from agents_router.runtimes import get_runtime
from agents_router.runtimes.base import ACCESS_LEVELS, CANCELLED, ERROR, LOST, JobSpec

ACTIVE_STATES = ("queued", "starting", "running")
JOB_ID_CHARS = set("0123456789abcdefghijklmnopqrstuvwxyz-")
NOTIFY_PENDING = "notify.pending"
NOTIFY_CLAIMED = "notify.claimed"
NOTIFY_LOCK = "notify.lock"
GROUP_PREFIX = "g-"
MAX_CURSORS = 50


def job_dir(job_id: str) -> Path:
    if not job_id or set(job_id) - JOB_ID_CHARS or len(job_id) > 64:
        raise ValueError(f"invalid job id: {job_id!r}")
    return jobs_dir() / job_id


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def is_group(job_id: str) -> bool:
    return isinstance(job_id, str) and job_id.startswith(GROUP_PREFIX)


def new_group_id() -> str:
    return GROUP_PREFIX + secrets.token_hex(4)


def group_path(group_id: str) -> Path:
    if not is_group(group_id) or set(group_id) - JOB_ID_CHARS or len(group_id) > 64:
        raise ValueError(f"invalid group id: {group_id!r}")
    return state_dir() / "groups" / f"{group_id}.json"


def write_group(record: dict[str, Any]) -> None:
    path = group_path(record["group"])
    make_private_dir(path.parent)
    write_json(path, record)


def read_group(group_id: str) -> dict[str, Any]:
    record = read_json(group_path(group_id))
    if not isinstance(record, dict):
        raise ValueError(f"unknown group: {group_id}")
    return record


def group_members(group_id: str) -> list[str]:
    return [str(item["job_id"]) for item in read_group(group_id).get("jobs") or []]


def save_cursor(group_id: str, state: dict[str, Any]) -> int:
    """Store where a group follow stopped; return the number `--from` names it by (1, 2, ...)."""

    path = group_path(group_id)
    with file_lock(path.with_suffix(".lock")):
        record = read_group(group_id)
        cursors = record.get("cursors") or {}
        number = max((int(key) for key in cursors), default=0) + 1
        cursors[str(number)] = state
        for key in sorted(cursors, key=int)[:-MAX_CURSORS]:
            del cursors[key]
        record["cursors"] = cursors
        write_json(path, record)
    return number


def load_cursor(group_id: str, number: int) -> dict[str, Any]:
    """A saved cursor; 0 is the start of every member's events."""

    if number == 0:
        return {}
    cursor = (read_group(group_id).get("cursors") or {}).get(str(number))
    if not isinstance(cursor, dict):
        raise ValueError(f"group {group_id} has no follow position {number}; follow it again without --from")
    return cursor


def create_job(**kwargs: Any) -> dict[str, Any]:
    """Validate a request, check the limits, write the job record and start its runner.

    Raises limits.LimitError (a ValueError) when a limit says the job can't start now.
    Counting and starting happen under one lock, so two callers can't both take the last slot.
    """

    if not kwargs.get("start", True):
        return _create_job(**kwargs)
    with file_lock(state_dir() / "admission.lock", timeout=30):
        return _create_job(**kwargs)


def _create_job(
    *,
    runtime: str,
    prompt: str,
    cwd: str,
    account: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    override: str | None = None,
    access: str = "edit",
    max_turns: int | None = None,
    timeout_seconds: int = 1800,
    add_dirs: list[str] | None = None,
    resume_job: str | None = None,
    host: str | None = None,
    start: bool = True,
    group: str | None = None,
) -> dict[str, Any]:
    limits.validate_override(override)
    workspace = levels.find_workspace(cwd)
    try:
        config = load_config(cwd=cwd)
    except levels.SettingsError as exc:
        synclog.refused(str(exc), workspace)
        raise
    synclog.notice(workspace)
    host = host or detect_host()
    session = session_folder(host)
    set_session(config, session)  # the calling session's own profile never takes its delegated jobs
    runtime_config = config.runtime(runtime)
    depth = int(os.environ.get("AGT_DEPTH", "0") or 0)
    if depth >= config.max_depth:
        raise ValueError(
            "this session is itself an Agents Router worker; nested delegation is disabled "
            f"(max_depth = {config.max_depth})"
        )
    if not prompt or not prompt.strip():
        raise ValueError("prompt is empty")
    if access not in ACCESS_LEVELS:
        raise ValueError(f"access must be one of {ACCESS_LEVELS}")
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    if path.resolve() == Path(path.anchor).resolve() or path.resolve() == Path.home().resolve():
        raise ValueError("cwd cannot be a drive root or your home folder; use a project folder")
    timeout_seconds = int(timeout_seconds)
    if not 30 <= timeout_seconds <= 6 * 3600:
        raise ValueError("timeout_seconds must be between 30 and 21600")
    directories = []
    for item in add_dirs or []:
        directory = Path(item).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError(f"add_dirs entry is not an existing absolute folder: {item}")
        directories.append(str(directory))

    resume_session = None
    resume_account = None
    resume_home = None
    if resume_job:
        previous = read_status(resume_job)
        closed = read_json(job_dir(resume_job) / "inbox-closed.json", {}) or {}
        if previous.get("state") in ACTIVE_STATES:
            if not closed.get("session_id"):
                raise ValueError(
                    f"job {resume_job} is still running; wait for it to finish, or send it a follow-up with subagent_message"
                )
        resume_session = closed.get("session_id") or previous.get("session_id")
        if not resume_session:
            raise ValueError(f"job {resume_job} has no session to continue")
        if previous.get("runtime") != runtime:
            raise ValueError("resume_job belongs to a different runtime")
        resume_account = closed.get("account") or previous.get("account")
        resume_home = closed.get("home") or next(
            (attempt.get("home") for attempt in reversed(previous.get("attempts") or [])
             if isinstance(attempt, dict) and attempt.get("account") == resume_account and attempt.get("home")), None)
        if not hasattr(get_runtime(runtime), "transfer_session"):
            # The session lives in one account's folder and can't be moved: stay there.
            account = resume_account or account

    admission = limits.admit(config, runtime, account=account, model=model, effort=effort, override=override) if start else None
    if admission is not None:
        effort = admission.effort

    model = model or runtime_config.default_model
    pool = quota.pool_for(runtime_config, model)
    spec = JobSpec(
        job_id=new_job_id(),
        runtime=runtime,
        prompt=prompt,
        cwd=str(path.resolve()),
        account=account or None,
        model=model or None,
        effort=effort or None,
        override=override,
        access=access,
        max_turns=int(max_turns) if max_turns else None,
        timeout_seconds=timeout_seconds,
        add_dirs=directories,
        resume_session=resume_session,
        resume_account=resume_account,
        resume_home=resume_home,
        parent={**host_context(host, str(path.resolve())), "config_dir": str(session) if session else None},
        depth=depth + 1,
        created=time.time(),
    )
    folder = job_dir(spec.job_id)
    make_private_dir(folder.parent)
    folder.mkdir(mode=0o700)
    write_json(folder / "spec.json", spec.to_dict())
    status = {
        "job_id": spec.job_id,
        "runtime": runtime,
        "override": override,
        "state": "queued",
        "outcome": None,
        "cwd": spec.cwd,
        "account": None,
        "assigned_account": admission.account if admission else account,
        "pool": pool.name if pool else None,
        "created_at": spec.created,
        "updated_at": spec.created,
        "attempts": [],
        "warnings": [],
        # The runner rewrites "warnings"; this key survives, and summarize() shows it with them.
        "limit_notes": list(admission.warnings) if admission else [],
    }
    if group:
        status["group"] = group
    write_json(folder / "status.json", status)
    if start:
        try:
            pid = spawn_detached(
                [python_executable(), "-m", "agents_router", "_runner", spec.job_id],
                folder / "runner.log",
                env=_runner_env(),
            )
        except OSError as exc:
            mark_pending(spec.job_id)
            return update_status(
                spec.job_id, state="done", outcome=ERROR, message=f"could not start the runner: {exc}", finished_at=time.time()
            )
        # The runner owns status.json; the launcher only leaves its PID beside it.
        write_text(folder / "runner.pid", str(pid))
        status["runner_pid"] = pid
    return status


def _runner_env() -> dict[str, str]:
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = package_root + (os.pathsep + existing if existing else "")
    return env


def read_spec(job_id: str) -> JobSpec:
    data = read_json(job_dir(job_id) / "spec.json")
    if not isinstance(data, dict):
        raise ValueError(f"unknown job: {job_id}")
    return JobSpec.from_dict(data)


def read_status(job_id: str) -> dict[str, Any]:
    folder = job_dir(job_id)
    status = read_json(folder / "status.json")
    if not isinstance(status, dict):
        raise ValueError(f"unknown job: {job_id}")
    if status.get("state") in ACTIVE_STATES:
        pid = status.get("runner_pid") or _runner_pid(folder)
        stale = time.time() - float(status.get("updated_at") or 0) > 15
        if pid and stale and not pid_alive(int(pid)):
            status.update(state="done", outcome=LOST, message="the runner process stopped unexpectedly")
            _stop_orphan(status)
    return status


def _stop_orphan(status: dict[str, Any]) -> None:
    """Kill the worker a dead runner left behind, if it is still the same process."""

    pid = status.get("child_pid")
    started = status.get("child_started")
    try:
        if pid and same_process(int(pid), int(started) if started is not None else None):
            kill_tree(int(pid))
    except (TypeError, ValueError, OSError, subprocess.SubprocessError):
        pass


def _runner_pid(folder: Path) -> int | None:
    try:
        return int((folder / "runner.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def update_status(job_id: str, **fields: Any) -> dict[str, Any]:
    folder = job_dir(job_id)
    status = read_json(folder / "status.json", {}) or {}
    status.update(fields)
    status["updated_at"] = time.time()
    write_json(folder / "status.json", status)
    return status


def mark_pending(job_id: str) -> None:
    """Record that the job's result waits for delivery. Written before the final status."""

    folder = job_dir(job_id)
    with file_lock(folder / NOTIFY_LOCK):
        if not (folder / NOTIFY_CLAIMED).exists():
            write_text(folder / NOTIFY_PENDING, str(time.time()))


def is_pending(job_id: str) -> bool:
    return (job_dir(job_id) / NOTIFY_PENDING).exists()


def claim_delivery(job_id: str) -> bool:
    """Take the one delivery of a finished result. True only for the first caller.

    Racing callers (the wake-up hook, subagent_wait, subagent_result) take turns
    under notify.lock. The rename alone is not enough: on Windows, callers that
    open notify.pending at the same moment can all rename it, the later ones
    "renaming" notify.claimed to itself. A finished job that never got a marker
    (its runner died) is claimed by creating notify.claimed exclusively.
    """

    folder = job_dir(job_id)
    if not folder.is_dir() or (folder / NOTIFY_CLAIMED).exists():
        return False  # no such job (the lock would create its folder), or delivered already
    try:
        with file_lock(folder / NOTIFY_LOCK):
            return _claim_locked(job_id, folder)
    except TimeoutError:
        return False


def _claim_locked(job_id: str, folder: Path) -> bool:
    try:
        os.rename(folder / NOTIFY_PENDING, folder / NOTIFY_CLAIMED)
        return True
    except FileNotFoundError:
        pass
    except OSError:
        return False  # notify.claimed already exists (Windows refuses to rename over it)
    if (folder / NOTIFY_CLAIMED).exists() or (folder / NOTIFY_PENDING).exists():
        return False
    try:
        if read_status(job_id).get("state") in ACTIVE_STATES:
            return False
        fd = os.open(folder / NOTIFY_CLAIMED, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except (ValueError, OSError):
        return False
    os.close(fd)
    return True


def wait_for(job_id: str, seconds: float) -> dict[str, Any]:
    """Block up to `seconds` until the job finishes; return its status."""

    deadline = time.monotonic() + max(0.0, seconds)
    status = read_status(job_id)
    while status.get("state") in ACTIVE_STATES and time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        status = read_status(job_id)
    return status


def read_result(job_id: str, max_chars: int = 20000) -> dict[str, Any]:
    status = read_status(job_id)
    path = job_dir(job_id) / "result.md"
    text = read_text(path) if path.exists() else ""
    truncated = len(text) > max_chars
    return {
        **status,
        "result": text[:max_chars],
        "result_truncated": truncated,
        "result_path": str(path) if path.exists() else None,
        "events_path": str(job_dir(job_id) / "events.jsonl"),
    }


def cancel(job_id: str) -> dict[str, Any]:
    status = read_status(job_id)
    if status.get("outcome") == LOST:
        # The runner is gone; read_status already stopped its worker. Record the cancel.
        _stop_orphan(status)
        return update_status(
            job_id, state="done", outcome=CANCELLED, message="cancelled (the runner had stopped)",
            finished_at=time.time(), child_pid=None,
        )
    if status.get("state") not in ACTIVE_STATES:
        return status
    folder = job_dir(job_id)
    write_text(folder / "cancel", str(time.time()))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = read_status(job_id)
        if status.get("state") not in ACTIVE_STATES:
            return status
        time.sleep(0.3)
    # The runner didn't react; stop everything ourselves.
    _stop_orphan(status)
    runner_pid = status.get("runner_pid") or _runner_pid(folder)
    if runner_pid and pid_alive(int(runner_pid)):
        kill_tree(int(runner_pid))
    return update_status(job_id, state="done", outcome=CANCELLED, message="cancelled", finished_at=time.time())


def cancel_group(group_id: str) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Stop every job of a role call. Returns (member, status before, status after) per member.

    Every runner is asked first, so their waits overlap instead of adding up.
    """

    members = read_group(group_id).get("jobs") or []
    before = {}
    for item in members:
        status = read_status(item["job_id"])
        before[item["job_id"]] = status
        if status.get("state") in ACTIVE_STATES:
            write_text(job_dir(item["job_id"]) / "cancel", str(time.time()))
    return [(item, before[item["job_id"]], cancel(item["job_id"])) for item in members]


def _statuses() -> Iterator[dict[str, Any]]:
    folder = jobs_dir()
    if not folder.is_dir():
        return
    for job_id in sorted((path.name for path in folder.iterdir() if path.is_dir()), reverse=True):
        try:
            yield read_status(job_id)
        except ValueError:
            continue


def _started_in(workspace: levels.Workspace, seen: dict[str, str | None]) -> Callable[[dict[str, Any]], bool]:
    """Whether a job started in `workspace` or one of its worktrees."""

    def check(status: dict[str, Any]) -> bool:
        cwd = str(status.get("cwd") or "")
        if cwd not in seen:
            found = levels.find_workspace(cwd) if cwd else None
            seen[cwd] = found.family if found else None
        return seen[cwd] == workspace.family

    return check


def started_from(job_id: str, session: Path | None) -> bool:
    """Whether a job was started from this Claude folder's session. Jobs that don't record one match any."""

    if session is None:
        return True
    try:
        parent = read_spec(job_id).parent
    except (OSError, ValueError, TypeError):
        return True
    folder = parent.get("config_dir") if isinstance(parent, dict) else None
    if not folder:
        return True
    from agents_router.profiles import same_folder

    return same_folder(folder, session)


def list_jobs(limit: int = 20, workspace: levels.Workspace | None = None, session: Path | None = None) -> list[dict[str, Any]]:
    """The newest jobs; with `workspace`, the newest started in it or its worktrees; with `session`, from that Claude folder."""

    keep = _started_in(workspace, {}) if workspace is not None else (lambda status: True)
    jobs = []
    for status in _statuses():
        if len(jobs) == limit:
            break
        if keep(status) and started_from(str(status.get("job_id") or ""), session):
            jobs.append(
                {key: status.get(key) for key in
                 ("job_id", "runtime", "state", "outcome", "account", "created_at", "cwd", "override", "group")}
            )
    return jobs


def count_elsewhere(workspace: levels.Workspace) -> int:
    """How many jobs started outside `workspace` and its worktrees."""

    keep = _started_in(workspace, {})
    return sum(1 for status in _statuses() if not keep(status))


def clean_old_jobs(days: int) -> int:
    config = load_config()
    if any(pool.weekly_jobs is not None for runtime in config.runtimes.values() for pool in runtime.pools):
        days = max(days, 7)  # weekly budgets must keep the records they count
    folder = jobs_dir()
    if not folder.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in folder.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            status = read_status(path.name)  # a job whose runner died reads as done/lost
        except ValueError:
            status = {}
        if status.get("state") in ACTIVE_STATES:
            continue
        if float(status.get("updated_at") or path.stat().st_mtime) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    groups = state_dir() / "groups"
    for path in groups.glob("g-*.*") if groups.is_dir() else []:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass
    return removed
