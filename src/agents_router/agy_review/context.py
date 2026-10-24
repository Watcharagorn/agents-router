"""The per-job context file: what the gate needs to know about one job.

Written by the caller (Agents Router) before it starts agy, read by every hook
call. Its path reaches the hook through AGY_REVIEW_CONTEXT in agy's
environment. Keys:

    version              1
    access               "read" or "edit"
    sandbox              true when agy runs with --sandbox
    workspaces           folders the job works in (reads and edits allowed)
    read_paths           extra files and folders it may read (instruction files, skills)
    protected_paths      folders it may never touch (beyond the built-in list)
    agent_home           the HOME agy runs with
    user_home            the user's real home folder
    trusted_mcp_servers  MCP servers whose tools need no review
    reviewer             {"kind": "agy"|"claude"|"off", "command": [...], "model": ..., "env": {...},
                          "unset_env": [...], "timeout": 75, "work_dir": ...}
    log, cache           files for the decision log and the decision cache
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERSION = 1
MAX_BYTES = 256 * 1024
ACCESS = ("read", "edit")


class ContextError(Exception):
    pass


def _strings(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContextError(f"{key} must be a list of strings")
    return value


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ContextError("unknown context version")
    if data.get("access") not in ACCESS:
        raise ContextError("access must be read or edit")
    if not _strings(data.get("workspaces"), "workspaces"):
        raise ContextError("workspaces is empty")
    for key in ("read_paths", "protected_paths", "trusted_mcp_servers"):
        _strings(data.get(key), key)
    reviewer = data.get("reviewer") or {"kind": "off"}
    if not isinstance(reviewer, dict) or reviewer.get("kind") not in ("agy", "claude", "off"):
        raise ContextError("reviewer.kind must be agy, claude or off")
    _strings(reviewer.get("command"), "reviewer.command")
    if not isinstance(reviewer.get("env") or {}, dict):
        raise ContextError("reviewer.env must be an object")
    return {**data, "reviewer": reviewer}


def load_context(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContextError(f"cannot read the context file: {type(exc).__name__}") from exc
    if len(raw) > MAX_BYTES:
        raise ContextError("the context file is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ContextError("the context file is not JSON") from exc
    return validate(data)


def write_context(path: Path, data: dict[str, Any]) -> Path:
    """Validate and write a context file (the caller picks a private folder)."""

    validate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
