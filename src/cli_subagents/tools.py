"""Tool definitions and handlers shared by the MCP server and the CLI."""

from __future__ import annotations

import time
from typing import Any

from cli_subagents import accounts, jobs
from cli_subagents.config import load_config
from cli_subagents.runtimes import AVAILABLE as RUNTIMES, get_runtime
from cli_subagents.runtimes.base import ACCESS_LEVELS

MAX_WAIT_SECONDS = 600


class ToolError(Exception):
    """A user-facing error returned to the host as a failed tool call."""


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


JOB_ID = {"type": "string", "description": "Job ID returned by subagent_start."}
WAIT = {
    "type": "integer",
    "minimum": 0,
    "maximum": MAX_WAIT_SECONDS,
    "description": "Wait up to this many seconds for the job to finish before returning.",
}

DEFINITIONS = [
    {
        "name": "subagent_start",
        "description": (
            "Start a subagent: another installed agent CLI works on one task in the background, "
            "under one of your accounts, with your shared MCP servers, plugins, skills and settings. "
            "Returns a job_id at once (or the result, if it finishes within wait_seconds). "
            "Give it a complete, self-contained task: goal, scope, files, and how to check the result."
        ),
        "inputSchema": _schema(
            {
                "prompt": {"type": "string", "description": "The full task for the subagent."},
                "cwd": {"type": "string", "description": "Absolute path of the project folder to work in."},
                "runtime": {"type": "string", "enum": list(RUNTIMES), "default": "claude"},
                "account": {"type": "string", "description": "Run on this account only. Omit to let the pool pick."},
                "model": {"type": "string", "description": "Model alias or ID, e.g. 'sonnet', 'opus', 'haiku'."},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
                "access": {
                    "type": "string",
                    "enum": list(ACCESS_LEVELS),
                    "default": "edit",
                    "description": "read: no edits (plan mode). edit: edits with the CLI's own safety checks (auto mode). full: skip all permission checks; only when the user asked for it.",
                },
                "max_turns": {"type": "integer", "minimum": 1},
                "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 21600, "default": 1800},
                "add_dirs": {"type": "array", "items": {"type": "string"}, "description": "Extra absolute folders the subagent may use."},
                "resume_job": {"type": "string", "description": "Continue the session of an earlier job (same account)."},
                "wait_seconds": WAIT,
            },
            ["prompt", "cwd"],
        ),
    },
    {
        "name": "subagent_status",
        "description": "State and progress of a job. Use wait_seconds instead of polling in a tight loop.",
        "inputSchema": _schema({"job_id": JOB_ID, "wait_seconds": WAIT}, ["job_id"]),
    },
    {
        "name": "subagent_result",
        "description": "Final answer, outcome, account, session and cost of a finished job.",
        "inputSchema": _schema(
            {"job_id": JOB_ID, "max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 20000}},
            ["job_id"],
        ),
    },
    {
        "name": "subagent_cancel",
        "description": "Stop a running job and every process it started.",
        "inputSchema": _schema({"job_id": JOB_ID}, ["job_id"]),
    },
    {
        "name": "subagent_list",
        "description": "Recent jobs, newest first.",
        "inputSchema": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, []),
    },
    {
        "name": "subagent_accounts",
        "description": "Installed runtimes, their accounts, login state, last known usage and cooldowns. Makes no model calls.",
        "inputSchema": _schema({"runtime": {"type": "string", "enum": list(RUNTIMES)}}, []),
    },
]


def definitions() -> list[dict[str, Any]]:
    return DEFINITIONS


def startup() -> None:
    try:
        jobs.clean_old_jobs(load_config().job_retention_days)
    except Exception:
        pass


def summarize(status: dict[str, Any]) -> dict[str, Any]:
    """The parts of a job status a host needs, without internal noise."""

    keys = (
        "job_id", "runtime", "state", "outcome", "message", "account", "session_id",
        "cwd", "progress", "cost_usd", "warnings",
    )
    summary = {key: status.get(key) for key in keys if status.get(key) not in (None, [], {})}
    init = status.get("init") or {}
    if init:
        summary["worker"] = {
            "model": init.get("model"),
            "permission_mode": init.get("permission_mode"),
            "mcp_servers": len(init.get("mcp_servers") or []),
            "plugins": len(init.get("plugins") or []),
            "skills": len(init.get("skills") or []),
        }
    attempts = status.get("attempts") or []
    if len(attempts) > 1:
        summary["attempts"] = [
            {key: attempt.get(key) for key in ("account", "outcome", "message")} for attempt in attempts
        ]
    started = status.get("started_at") or status.get("created_at")
    if started:
        end = status.get("finished_at") or time.time()
        summary["elapsed_seconds"] = round(float(end) - float(started), 1)
    return summary


def _wait(arguments: dict[str, Any]) -> float:
    return float(min(MAX_WAIT_SECONDS, max(0, int(arguments.get("wait_seconds") or 0))))


def call(name: str, arguments: dict[str, Any]) -> Any:
    try:
        return _call(name, arguments)
    except ToolError:
        raise
    except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # bad argument types and the like
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def _call(name: str, arguments: dict[str, Any]) -> Any:
    if name == "subagent_start":
        status = jobs.create_job(
            runtime=str(arguments.get("runtime") or "claude"),
            prompt=str(arguments.get("prompt") or ""),
            cwd=str(arguments.get("cwd") or ""),
            account=arguments.get("account"),
            model=arguments.get("model"),
            effort=arguments.get("effort"),
            access=str(arguments.get("access") or "edit"),
            max_turns=arguments.get("max_turns"),
            timeout_seconds=int(arguments.get("timeout_seconds") or 1800),
            add_dirs=list(arguments.get("add_dirs") or []),
            resume_job=arguments.get("resume_job"),
        )
        wait = _wait(arguments)
        if wait:
            status = jobs.wait_for(status["job_id"], wait)
            if status.get("state") == "done":
                return _result(status["job_id"], 20000)
        return summarize(jobs.read_status(status["job_id"]))
    if name == "subagent_status":
        return summarize(jobs.wait_for(_job_id(arguments), _wait(arguments)))
    if name == "subagent_result":
        return _result(_job_id(arguments), int(arguments.get("max_chars") or 20000))
    if name == "subagent_cancel":
        return summarize(jobs.cancel(_job_id(arguments)))
    if name == "subagent_list":
        return {"jobs": jobs.list_jobs(int(arguments.get("limit") or 20))}
    if name == "subagent_accounts":
        selected = arguments.get("runtime")
        return {"runtimes": [account_report(runtime) for runtime in RUNTIMES if not selected or runtime == selected]}
    raise ToolError(f"unknown tool: {name}")


def _job_id(arguments: dict[str, Any]) -> str:
    job_id = str(arguments.get("job_id") or "")
    if not job_id:
        raise ToolError("job_id is required")
    return job_id


def _result(job_id: str, max_chars: int) -> dict[str, Any]:
    data = jobs.read_result(job_id, max(500, min(200000, max_chars)))
    summary = summarize(data)
    if data.get("state") != "done":
        summary["note"] = "The job is still running; this is not a final result."
    summary["result"] = data.get("result")
    if data.get("result_truncated"):
        summary["result_truncated"] = True
        summary["result_path"] = data.get("result_path")
    return summary


def account_report(runtime_name: str) -> dict[str, Any]:
    config = load_config().runtime(runtime_name)
    runtime = get_runtime(runtime_name)
    binary = runtime.find_binary(config)
    report: dict[str, Any] = {
        "runtime": runtime_name,
        "binary": binary[-1] if binary else None,
        "version": runtime.version(config) if binary else None,
        "primary_setup": str(config.primary),
        "failover": config.failover,
        "accounts": [],
    }
    usage = accounts.load_usage(runtime_name)
    now = time.time()
    for account in config.worker_accounts():
        record = usage.get(account.name) or {}
        entry: dict[str, Any] = {"name": account.name, "home": str(account.home)}
        if binary:
            entry["auth"] = runtime.auth_status(config, account)
        until = accounts.cooldown_until(record, now)
        if until:
            entry["cooldown_until"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(until))
        windows = record.get("windows") or {}
        if windows:
            entry["usage"] = {
                name: round(float(window["utilization"]) * 100)
                for name, window in windows.items()
                if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float))
            }
        entry["busy_slots"] = accounts.busy_slots(config, account)
        report["accounts"].append(entry)
    return report
