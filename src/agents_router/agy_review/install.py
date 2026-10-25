"""Wiring for agy: the hook command, the hooks.json entry and the settings the gate relies on."""

from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

HOOK_SET = "agents-router-review"
SHARE_SET = "agents-router-share"  # share/hooks.py's set of copies of your other CLIs' hooks
HOOK_TIMEOUT = 90  # seconds; agy's default of 30 is too short for a cold reviewer call
# The gate only answers auto_approve or deny. With "request-review" a hook that doesn't
# load leaves agy's headless default in place (commands, MCP calls and outside reads are
# refused), never "run everything" as "always-proceed" or "proceed-in-sandbox" would.
TOOL_PERMISSION = "request-review"


def entry_script() -> Path:
    return Path(__file__).with_name("run_hook.py").resolve()


def _short_path(path: str) -> str:
    """The Windows 8.3 form of a path (no spaces), or the path unchanged."""

    if os.name != "nt" or not any(char.isspace() for char in path):
        return path
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)):
            return buffer.value
    except (OSError, AttributeError):
        pass
    return path


def hook_command(python: str | None = None) -> str:
    """`<python> -I <run_hook.py>`, with no spaces or quotes (agy mangles quoted hook executables on Windows)."""

    parts = [_short_path(str(Path(python or sys.executable))), "-I", _short_path(str(entry_script()))]
    for part in parts:
        if any(char.isspace() for char in part) or '"' in part:
            raise ValueError(f"the hook command would need quotes ({part}); agy can't run such a hook")
    return subprocess.list2cmdline(parts)


def merged_hooks(user_hooks: Any, command: str, timeout: int = HOOK_TIMEOUT) -> dict[str, Any]:
    """The user's hooks.json content plus the review gate as its own hook set."""

    data = copy.deepcopy(user_hooks) if isinstance(user_hooks, dict) else {}
    data.pop(HOOK_SET, None)
    data.pop(SHARE_SET, None)  # agt share's copies of your hooks: in a worker the review gate is the only gate
    data[HOOK_SET] = {
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": timeout}]}]
    }
    return data


def gated_settings(user_settings: Any) -> dict[str, Any]:
    """The user's settings.json content with the permission mode the gate relies on."""

    data = copy.deepcopy(user_settings) if isinstance(user_settings, dict) else {}
    data["toolPermission"] = TOOL_PERMISSION
    return data
