#!/usr/bin/env python
"""Fail-closed Antigravity PreToolUse review gate."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from broker_security import (
    is_inside as broker_is_inside,
    issue_approval,
    parse_request as parse_command_request,
    read_request_bytes,
)
from reviewer_client import review_action

MAX_HOOK_INPUT_BYTES = 256 * 1024
MAX_REASON_CHARS = 500
MAX_CONFIG_B64_CHARS = 16_384

READ_ONLY_TOOLS = {
    "find_by_name",
    "grep_search",
    "list_dir",
    "search_files",
    "view_file",
    "read_file",
    "read_many_files",
}

WRITE_TOOLS = {
    "create",
    "write_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "mkdir",
}

TOOL_PATH_ARGUMENTS = {
    "absolutepath",
    "directorypath",
    "searchdirectory",
    "searchpath",
    "targetfile",
    "path",
    "paths",
}

SENSITIVE_MARKERS = (
    "/.ssh", "/.aws", "/.azure", "/.gcloud", "/.gnupg",
    "/appdata/roaming/microsoft/credentials/",
    "/appdata/local/microsoft/credentials/",
    "/google/chrome/user data/", "/microsoft/edge/user data/",
    "/mozilla/firefox/profiles/", "/1password/", "/bitwarden/",
    "/.config/gh/", "/appdata/roaming/github cli/",
    "/appdata/local/codex/antigravity-bridge/",
)

PROTECTED_PATH_COMPONENTS = {".agents", "_agents", ".gemini", ".codex", ".git"}
PROTECTED_PATH_PREFIXES = (".codex-agy-executor-", ".codex-agy-reviewer-")

HARD_DENY_COMMAND_MARKERS = (
    "invoke-expression", "iex ", "-encodedcommand", "frombase64string",
    "format-volume", "clear-disk", "diskpart", "bcdedit", "cipher /w",
    "disable-windowsoptionalfeature", "set-mppreference -disablerealtimemonitoring",
    "netsh advfirewall set allprofiles state off", "reg delete",
    "git reset --hard", "git clean -fd", "rm -rf /", "remove-item c:/ -recurse",
    "schtasks /create", "new-service", "sc.exe create",
)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def action_digest(tool_call: dict[str, Any]) -> str:
    canonical = json.dumps(tool_call, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_hook_input(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_HOOK_INPUT_BYTES:
        raise ValueError("hook input is empty or exceeds the size limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("hook input must be a JSON object")
    tool_call = value.get("toolCall")
    if not isinstance(tool_call, dict):
        raise ValueError("toolCall must be an object")
    if not isinstance(tool_call.get("name"), str) or not tool_call["name"]:
        raise ValueError("toolCall.name must be a non-empty string")
    if not isinstance(tool_call.get("args", {}), dict):
        raise ValueError("toolCall.args must be an object")
    return value


def deterministic_decision(
    payload: dict[str, Any], project_root: Path
) -> tuple[str, str] | None:
    """Return an immediate decision, or None when model review is required."""

    tool_call = payload["toolCall"]
    name = tool_call["name"].lower()
    args_text = json.dumps(tool_call.get("args", {}), ensure_ascii=False).lower()
    normalized_args = args_text.replace("\\\\", "/").replace("\\", "/")

    protected_markers = (
        "/.agents/",
        "/_agents/",
        "/.gemini/",
        "/.codex/",
        "/.git/",
        "/.codex-agy-executor-",
        "/.codex-agy-reviewer-",
    )
    if any(marker in normalized_args for marker in protected_markers):
        return "deny", "Action targets agent or reviewer configuration."

    if any(marker in normalized_args for marker in SENSITIVE_MARKERS):
        return "deny", "Action targets a credential, browser, or password-manager store."

    secret_suffixes = (".env", ".pem", ".key", "secrets.", "/.secrets/")
    if any(marker in normalized_args for marker in secret_suffixes):
        return "deny", "Action targets a secret-bearing file class."

    if name in {"run_command", "command", "powershell", "bash", "shell"}:
        if any(marker in normalized_args for marker in HARD_DENY_COMMAND_MARKERS):
            return "deny", "Command matches a deterministic destructive or security-weakening deny rule."

    if name in READ_ONLY_TOOLS:
        candidate_strings = _path_arguments(tool_call.get("args", {}))
        if not candidate_strings:
            return "deny", "Read-only tool call has no recognized path argument."
        for candidate in candidate_strings:
            path = _resolve_tool_path(candidate, project_root)
            denial = _path_denial_reason(path)
            if denial:
                return "deny", denial
            if not _is_inside(path, project_root):
                return "deny", "Read target is outside the active project workspace."
        return "allow", "Read-only action is confined to the active project workspace."

    if name in WRITE_TOOLS:
        candidate_strings = _path_arguments(tool_call.get("args", {}))
        if not candidate_strings:
            return "deny", "File-write tool call has no recognized path argument."
        for candidate in candidate_strings:
            path = _resolve_tool_path(candidate, project_root)
            denial = _path_denial_reason(path)
            if denial:
                return "deny", denial
            if not _is_inside(path, project_root):
                return "deny", "Write target is outside the active project workspace."
        return "allow", "Ordinary file edit is confined to the active project workspace."

    return None


def _path_arguments(args: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key, value in args.items():
        if key.lower() not in TOOL_PATH_ARGUMENTS:
            continue
        if isinstance(value, str):
            paths.append(value)
        elif isinstance(value, list):
            paths.extend(item for item in value if isinstance(item, str))
    return paths


def _resolve_tool_path(value: str, project_root: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def _is_exact_request_write(
    tool_call: dict[str, Any], *, request_path: Path, project_root: Path
) -> bool:
    if tool_call.get("name", "").lower() not in WRITE_TOOLS:
        return False
    candidates = _path_arguments(tool_call.get("args", {}))
    if len(candidates) != 1:
        return False
    return _resolve_tool_path(candidates[0], project_root) == request_path


def _path_denial_reason(path: Path) -> str | None:
    """Inspect the resolved target so junctions/symlinks cannot hide protected paths."""

    parts = tuple(part.casefold() for part in path.parts)
    if any(part in PROTECTED_PATH_COMPONENTS for part in parts) or any(
        part.startswith(PROTECTED_PATH_PREFIXES) for part in parts
    ):
        return "Action targets agent or reviewer configuration."
    normalized = path.as_posix().casefold()
    if any(marker in normalized for marker in SENSITIVE_MARKERS):
        return "Action targets a credential, browser, or password-manager store."
    name = path.name.casefold()
    if (
        name == ".env"
        or name.startswith(".env.")
        or name.endswith((".pem", ".key"))
        or name.startswith("secrets.")
        or ".secrets" in parts
    ):
        return "Action targets a secret-bearing file class."
    return None


def _command_line(tool_call: dict[str, Any]) -> str | None:
    if tool_call.get("name", "").lower() not in {
        "run_command",
        "command",
        "powershell",
        "bash",
        "shell",
    }:
        return None
    for key, value in tool_call.get("args", {}).items():
        if key.lower() in {"commandline", "command", "cmd"} and isinstance(value, str):
            return value
    return None


def _parse_broker_invocation(command: str, broker_path: Path) -> str | None:
    broker_text = str(broker_path.resolve()).replace("\\", "/")
    broker = re.escape(broker_text)
    normalized_command = command.replace("\\", "/")
    match = re.fullmatch(
        rf'\s*"?{broker}"?\s+([0-9a-f]{{32}})\s*',
        normalized_command,
        flags=re.IGNORECASE,
    )
    return match.group(1).lower() if match else None


def _load_broker_request(
    tool_call: dict[str, Any], config: dict[str, str], project_root: Path
) -> tuple[bytes, dict[str, Any], dict[str, Any]] | None:
    command = _command_line(tool_call)
    if command is None:
        return None
    broker_path = Path(config["broker_path"]).resolve(strict=True)
    approval_id = _parse_broker_invocation(command, broker_path)
    if approval_id is None:
        normalized_broker = str(broker_path).replace("\\", "/").casefold()
        if normalized_broker in command.replace("\\", "/").casefold():
            raise ValueError("reviewed command broker invocation has an invalid shape")
        return None
    if approval_id != config["approval_id"]:
        raise ValueError("reviewed command broker approval id mismatch")
    request_dir = Path(config["request_dir"]).resolve(strict=True)
    request_path = (request_dir / f"{approval_id}.json").resolve()
    if request_path.parent != request_dir or not broker_is_inside(request_path, request_dir):
        raise ValueError("command request is outside the active request directory")
    raw = read_request_bytes(request_path)
    parsed = parse_command_request(
        raw,
        project_root=project_root,
        request_dir=request_dir,
    )
    synthetic = {
        "name": "run_command",
        "args": {
            "CommandLine": parsed["command"],
            "Cwd": parsed["cwd"],
            "WaitMsBeforeAsync": 30_000,
            "RunPersistent": False,
        },
    }
    return raw, parsed, synthetic


def emit(decision: str, reason: str) -> int:
    if decision not in {"allow", "deny", "ask", "force_ask"}:
        decision = "deny"
        reason = "Invalid internal gate decision."
    output = {"decision": decision, "reason": reason[:MAX_REASON_CHARS]}
    sys.stdout.write(json.dumps(output, separators=(",", ":")))
    return 0


def _write_audit(audit_dir: Path, payload: dict[str, Any], result: dict[str, str]) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    digest = action_digest(payload["toolCall"])
    record = {
        "time_ns": time.time_ns(),
        "action_sha256": digest,
        "tool_name": payload["toolCall"]["name"],
        **result,
    }
    path = audit_dir / f"{record['time_ns']}-{digest[:16]}.json"
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("gate", "deny-reviewer-tools"))
    parser.add_argument("--config-b64")
    args = parser.parse_args()

    if args.mode == "deny-reviewer-tools":
        return emit("deny", "Reviewer sessions have no execution tools.")

    try:
        payload = parse_hook_input(sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1))
        if not args.config_b64 or len(args.config_b64) > MAX_CONFIG_B64_CHARS:
            raise ValueError("encoded reviewer configuration is missing or oversized")
        config = json.loads(base64.b64decode(args.config_b64, altchars=b"-_", validate=True))
        expected_config = {
            "project_root",
            "objective_file",
            "agy",
            "audit_dir",
            "approval_dir",
            "approval_id",
            "broker_path",
            "request_dir",
            "secret_path",
        }
        if not isinstance(config, dict) or set(config) != expected_config:
            raise ValueError("encoded reviewer configuration has the wrong fields")
        if not all(isinstance(config[key], str) and config[key] for key in expected_config):
            raise ValueError("encoded reviewer configuration has invalid values")
        root = Path(config["project_root"]).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("encoded project root is not a directory")
        objective_path = Path(config["objective_file"]).resolve(strict=True)
        request_dir = Path(config["request_dir"]).resolve(strict=True)
        broker_path = Path(config["broker_path"]).resolve(strict=True)
        agy_path = Path(config["agy"]).resolve(strict=True)
        secret_path = Path(config["secret_path"]).resolve(strict=True)
        approval_dir = Path(config["approval_dir"]).resolve()
        audit_dir = Path(config["audit_dir"]).resolve()
        if not objective_path.is_file() or not broker_is_inside(objective_path, root):
            raise ValueError("encoded objective file is outside the project")
        if not request_dir.is_dir() or not broker_is_inside(request_dir, root):
            raise ValueError("encoded request directory is outside the project")
        if not broker_path.is_file() or not agy_path.is_file() or not secret_path.is_file():
            raise ValueError("encoded executable or broker secret is missing")
        if broker_is_inside(secret_path, root):
            raise ValueError("broker secret cannot be stored inside the project")
        if broker_is_inside(approval_dir, root) or broker_is_inside(audit_dir, root):
            raise ValueError("approval and audit state must remain outside the project")
        expected_request_path = (request_dir / f"{config['approval_id']}.json").resolve()
        if expected_request_path.parent != request_dir:
            raise ValueError("encoded command request path is invalid")
        if _is_exact_request_write(
            payload["toolCall"],
            request_path=expected_request_path,
            project_root=root,
        ):
            return emit(
                "allow",
                "Write is limited to the current one-use command request file.",
            )
        broker_request = _load_broker_request(payload["toolCall"], config, root)
        review_payload = payload
        if broker_request is not None:
            raw_request, parsed_request, synthetic_call = broker_request
            review_payload = {**payload, "toolCall": synthetic_call}
            decision = deterministic_decision(review_payload, root)
        else:
            decision = deterministic_decision(payload, root)
            if _command_line(payload["toolCall"]) is not None and decision is None:
                return emit(
                    "deny",
                    "Direct terminal commands are disabled in headless mode; use the reviewed command broker.",
                )
        if decision is None:
            objective = objective_path.read_text(encoding="utf-8")[:16_000]
            result = review_action(
                agy_cmd=config["agy"],
                objective=objective,
                tool_call=review_payload["toolCall"],
                project_root=root,
            )
            _write_audit(audit_dir, review_payload, result)
            if result["decision"] == "allow" and broker_request is not None:
                issue_approval(
                    approval_id=config["approval_id"],
                    raw_request=raw_request,
                    parsed_request=parsed_request,
                    secret_path=secret_path,
                    approval_dir=approval_dir,
                )
            return emit(result["decision"], result["reason"])
        return emit(*decision)
    except Exception as exc:
        return emit("deny", f"Review gate failed closed: {type(exc).__name__}.")


if __name__ == "__main__":
    raise SystemExit(main())
