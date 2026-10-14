"""Job records on disk: create, start, inspect, wait, cancel, list, clean up.

One folder per job under `~/.cli-subagents/jobs/<job_id>/`:

    spec.json      what to run (written once)
    status.json    live state, written atomically by the runner
    events.jsonl   raw events from the worker CLI
    result.md      final answer text
    runner.log     runner stdout/stderr (diagnostics)
    cancel         marker file; its presence asks the runner to stop
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import time
from typing import Any

from cli_subagents.config import jobs_dir, load_config
from cli_subagents.fsutil import (
    kill_tree,
    pid_alive,
    python_executable,
    read_json,
    spawn_detached,
    write_json,
    write_text,
)
from cli_subagents.runtimes.base import ACCESS_LEVELS, CANCELLED, LOST, JobSpec

ACTIVE_STATES = ("queued", "starting", "running")
JOB_ID_CHARS = set("0123456789abcdefghijklmnopqrstuvwxyz-")


def job_dir(job_id: str) -> Path:
    if not job_id or set(job_id) - JOB_ID_CHARS or len(job_id) > 64:
        raise ValueError(f"invalid job id: {job_id!r}")
    return jobs_dir() / job_id


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def create_job(
    *,
    runtime: str,
    prompt: str,
    cwd: str,
    account: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    access: str = "edit",
    max_turns: int | None = None,
    timeout_seconds: int = 1800,
    add_dirs: list[str] | None = None,
    resume_job: str | None = None,
    start: bool = True,
) -> dict[str, Any]:
    """Validate a request, write the job record and start its runner."""

    config = load_config()
    config.runtime(runtime)
    depth = int(os.environ.get("CLI_SUBAGENTS_DEPTH", "0") or 0)
    if depth >= config.max_depth:
        raise ValueError(
            "this session is itself a CLI Subagents worker; nested delegation is disabled "
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
    if resume_job:
        previous = read_status(resume_job)
        resume_session = previous.get("session_id")
        if not resume_session:
            raise ValueError(f"job {resume_job} has no session to continue")
        if previous.get("runtime") != runtime:
            raise ValueError("resume_job belongs to a different runtime")
        # A session lives in one account's config folder, so stay on that account.
        account = previous.get("account") or account

    spec = JobSpec(
        job_id=new_job_id(),
        runtime=runtime,
        prompt=prompt,
        cwd=str(path.resolve()),
        account=account or None,
        model=model or None,
        effort=effort or None,
        access=access,
        max_turns=int(max_turns) if max_turns else None,
        timeout_seconds=timeout_seconds,
        add_dirs=directories,
        resume_session=resume_session,
        depth=depth + 1,
        created=time.time(),
    )
    folder = job_dir(spec.job_id)
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "spec.json", spec.to_dict())
    status = {
        "job_id": spec.job_id,
        "runtime": runtime,
        "state": "queued",
        "outcome": None,
        "cwd": spec.cwd,
        "account": None,
        "created_at": spec.created,
        "updated_at": spec.created,
        "attempts": [],
        "warnings": [],
    }
    write_json(folder / "status.json", status)
    if start:
        pid = spawn_detached(
            [python_executable(), "-m", "cli_subagents", "_runner", spec.job_id],
            folder / "runner.log",
            env=_runner_env(),
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
    return status


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
    text = path.read_text(encoding="utf-8") if path.exists() else ""
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
    for pid in (status.get("child_pid"), status.get("runner_pid") or _runner_pid(folder)):
        if pid and pid_alive(int(pid)):
            kill_tree(int(pid))
    return update_status(job_id, state="done", outcome=CANCELLED, message="cancelled", finished_at=time.time())


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    folder = jobs_dir()
    if not folder.is_dir():
        return []
    ids = sorted((path.name for path in folder.iterdir() if path.is_dir()), reverse=True)[:limit]
    jobs = []
    for job_id in ids:
        try:
            status = read_status(job_id)
        except ValueError:
            continue
        jobs.append(
            {key: status.get(key) for key in ("job_id", "runtime", "state", "outcome", "account", "created_at", "cwd")}
        )
    return jobs


def clean_old_jobs(days: int) -> int:
    folder = jobs_dir()
    if not folder.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in folder.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        status = read_json(path / "status.json", {}) or {}
        if status.get("state") in ACTIVE_STATES:
            continue
        if float(status.get("updated_at") or path.stat().st_mtime) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed
