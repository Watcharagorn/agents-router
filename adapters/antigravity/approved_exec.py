#!/usr/bin/env python
"""Execute one command only after consuming a signed reviewer approval."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys

from broker_security import (
    consume_approval,
    default_approval_dir,
    default_secret_path,
    is_inside,
    parse_request,
    read_request_bytes,
    request_digest,
    validate_approval_id,
)


SENSITIVE_ENV_EXACT = {
    "ANTHROPIC_API_KEY",
    "AZURE_CLIENT_SECRET",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GOOGLE_API_KEY",
    "NPM_TOKEN",
    "OPENAI_API_KEY",
    "PYPI_TOKEN",
}
SENSITIVE_ENV_PREFIXES = ("AWS_", "AZURE_", "GCP_", "GOOGLE_CLOUD_")
SENSITIVE_ENV_SUFFIXES = (
    "_API_KEY",
    "_ACCESS_TOKEN",
    "_AUTH_TOKEN",
    "_PASSWORD",
    "_PRIVATE_KEY",
    "_SECRET",
    "_TOKEN",
)


def sanitized_environment() -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in SENSITIVE_ENV_EXACT:
            continue
        if upper.startswith(SENSITIVE_ENV_PREFIXES) or upper.endswith(SENSITIVE_ENV_SUFFIXES):
            continue
        if upper.startswith("ANTIGRAVITY_BRIDGE_"):
            continue
        result[key] = value
    return result


def run_approved_command(command: str, cwd: str, timeout_seconds: int) -> int:
    kwargs = {
        "cwd": cwd,
        "shell": True,
        "env": sanitized_environment(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    try:
        return int(process.wait(timeout=timeout_seconds))
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=15,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        raise


def main() -> int:
    if len(sys.argv) != 2:
        print("reviewed-exec: expected exactly one command-request path", file=sys.stderr)
        return 64

    try:
        approval_id = validate_approval_id(sys.argv[1])
        approval = consume_approval(
            approval_id=approval_id,
            secret_path=default_secret_path(),
            approval_dir=default_approval_dir(),
        )
        project_root = Path(approval["project_root"]).resolve(strict=True)
        request_dir = Path(approval["request_dir"]).resolve(strict=True)
        request_path = (request_dir / f"{approval_id}.json").resolve()
        if not is_inside(request_path, request_dir) or request_path.parent != request_dir:
            raise ValueError("command request path is outside its approved request directory")
        raw = read_request_bytes(request_path)
        if request_digest(raw) != approval["request_sha256"]:
            raise ValueError("command request changed after review")
        request = parse_request(
            raw, project_root=project_root, request_dir=request_dir
        )
        request_path.unlink()
        return run_approved_command(
            request["command"],
            request["cwd"],
            request["timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        print("reviewed-exec: approved command timed out", file=sys.stderr)
        return 124
    except Exception as exc:
        print(f"reviewed-exec: denied ({type(exc).__name__})", file=sys.stderr)
        return 77


if __name__ == "__main__":
    raise SystemExit(main())
