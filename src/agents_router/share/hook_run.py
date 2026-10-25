"""Run a shared hook for another CLI: `agt share hook-run <name> --to <cli> --hash <id>`.

agt's copy of a hook in another CLI calls the launcher, which runs this file with
`python -I`: the standard library only, never the rest of agt, so it starts fast.
It reads the hook as agt saved it (`state/share/hooks/<name>.json`), turns the
target CLI's payload into the source CLI's, runs the hook's handlers, combines
their answers (deny > defer > ask > allow) and answers in the target's terms.

It never blocks by mistake: in a worker, when the hook or one of its scripts
changed since you approved it (the identity is worked out again here, from the
scripts as they are now), and on any failure, it gives the target's neutral
answer: nothing for Claude Code and Codex, `{"decision": "allow"}` for
Antigravity, which hands the call back to Antigravity's own permission policy.
It never answers `auto_approve`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any

NEUTRAL = {"antigravity": {"decision": "allow"}}
DEFAULT_TIMEOUT = 60
RANK = {"deny": 4, "defer": 3, "ask": 2, "allow": 1}
# A Claude or Codex tool name, and Antigravity's, with the argument that carries it.
SHELL = {"claude": ("Bash", "command"), "codex": ("Bash", "command"), "antigravity": ("run_command", "CommandLine")}


# The identity (hooks.py uses the same function) ------------------------------------------


def words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _file_word(word: str) -> str | None:
    """A word that is a file: its real path and content hash (the same script from two folders is the same word)."""

    path = os.path.expanduser(word)
    if not os.path.isabs(path) or not os.path.isfile(path):
        return None
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
    return f"<file {os.path.realpath(path)} {digest}>"


def identity(event: str, matcher: str, handlers: list[dict[str, Any]], folder: str) -> str:
    """The hook's identity: its event, matcher and handlers, the folder's own path as a placeholder, and every
    word that is a file by its real path and content hash (so a changed script is a changed hook)."""

    parts = []
    for handler in handlers:
        command = str(handler.get("command") or "")
        shown = []
        for word in words(command):
            as_file = _file_word(word)
            shown.append(as_file or (word.replace(folder, "<folder>") if folder else word))
        parts.append([str(handler.get("type") or "command"), shown])
    return hashlib.sha256(json.dumps([event, matcher, parts], sort_keys=True).encode("utf-8")).hexdigest()


# Payloads ----------------------------------------------------------------------------------


def common(payload: dict[str, Any], target: str, event: str) -> dict[str, Any]:
    """The target CLI's payload in Claude's shape (Codex's is the same)."""

    if target != "antigravity":
        return payload
    call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    result: dict[str, Any] = {"hook_event_name": event, "cwd": str(args.get("Cwd") or os.getcwd())}
    if call.get("name") == "run_command":
        result.update(tool_name="Bash", tool_input={"command": str(args.get("CommandLine") or args.get("Command") or "")})
    elif call:
        result.update(tool_name=str(call.get("name")), tool_input=args)
    return result


def for_source(shared: dict[str, Any], source: str) -> dict[str, Any]:
    """The payload the source CLI's handlers expect."""

    if source != "antigravity":
        return shared
    if shared.get("tool_name") == "Bash":
        return {"toolCall": {"name": "run_command", "args": {"CommandLine": shared.get("tool_input", {}).get("command", ""),
                                                             "Cwd": shared.get("cwd", "")}}}
    return {"toolCall": {"name": shared.get("tool_name", ""), "args": shared.get("tool_input", {})}}


# Running and combining -----------------------------------------------------------------------


def _answer(source: str, code: int, out: str, err: str, event: str) -> dict[str, Any]:
    """One handler's answer: decision (or None), reason, updatedInput, additionalContext, systemMessage."""

    found: dict[str, Any] = {"decision": None}
    if code == 2 and source != "antigravity":
        return {"decision": "deny", "reason": err.strip() or "a shared hook blocked it"}
    if code != 0:
        return found  # a failing handler is no opinion (Claude's default: don't block)
    text = out.strip()
    data = None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        if text and event in ("SessionStart", "UserPromptSubmit"):
            found["context"] = text  # Claude adds plain stdout of these events as context
        return found
    if source == "antigravity":
        word = data.get("decision")
        found["decision"] = {"auto_approve": "allow", "deny": "deny", "ask": "ask"}.get(word)
        found["reason"] = data.get("reason")
        return found
    specific = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else {}
    decision = specific.get("permissionDecision")
    if data.get("decision") == "block" or data.get("continue") is False:
        decision = "deny"
    found["decision"] = decision if decision in RANK else None
    found["reason"] = specific.get("permissionDecisionReason") or data.get("reason") or data.get("stopReason")
    if isinstance(specific.get("updatedInput"), dict):
        found["updated"] = specific["updatedInput"]
    if specific.get("additionalContext"):
        found["context"] = str(specific["additionalContext"])
    if data.get("systemMessage"):
        found["message"] = str(data["systemMessage"])
    return found


def run_all(spec: dict[str, Any], payload: dict[str, Any], deadline: float) -> list[dict[str, Any]]:
    env = dict(os.environ)
    folder = spec.get("folder") or ""
    if spec.get("cli") == "claude" and folder and not spec.get("default_folder"):
        env["CLAUDE_CONFIG_DIR"] = folder
    elif spec.get("cli") == "claude":
        env.pop("CLAUDE_CONFIG_DIR", None)
    env.setdefault("CLAUDE_PROJECT_DIR", str(payload.get("cwd") or os.getcwd()))
    data = json.dumps(for_source(payload, spec.get("cli", "claude"))).encode("utf-8")
    answers = []
    for handler in spec.get("handlers") or []:
        left = deadline - time.monotonic() - 1  # stop a slow handler about a second before the target gives up
        timeout = min(float(handler.get("timeout") or DEFAULT_TIMEOUT), left)
        if timeout <= 0:
            break
        try:
            result = subprocess.run(["/bin/sh", "-c", str(handler.get("command") or "")], input=data, capture_output=True,
                                    timeout=timeout, env=env, cwd=str(payload.get("cwd") or os.getcwd()))
        except (OSError, subprocess.TimeoutExpired):
            answers.append({"decision": None})
            continue
        answers.append(_answer(spec.get("cli", "claude"), result.returncode, result.stdout.decode("utf-8", "replace"),
                               result.stderr.decode("utf-8", "replace"), spec.get("event", "")))
    return answers


def combine(answers: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [answer for answer in answers if answer.get("decision")]
    top = max(decided, key=lambda answer: RANK[answer["decision"]], default=None)
    result: dict[str, Any] = {"decision": top["decision"] if top else None, "reason": top.get("reason") if top else None}
    if result["decision"] == "allow":
        updated = [answer["updated"] for answer in decided if answer.get("updated")]
        if updated:
            result["updated"] = updated[-1]
    context = [answer["context"] for answer in answers if answer.get("context")]
    if context:
        result["context"] = "\n".join(context)
    messages = [answer["message"] for answer in answers if answer.get("message")]
    if messages:
        result["message"] = "\n".join(messages)
    return result


# Answering in the target's terms --------------------------------------------------------------


def emit(result: dict[str, Any], target: str, event: str, original: dict[str, Any]) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) for the target CLI."""

    decision = result.get("decision")
    if decision == "defer":
        decision = None
    if target == "antigravity":
        if decision == "deny":
            return 0, json.dumps({"decision": "deny", "reason": result.get("reason") or "a shared hook blocked it"}), ""
        return 0, json.dumps(NEUTRAL["antigravity"]), ""  # allow, ask and a rewrite all go to agy's own policy
    if decision == "deny":
        return 2, "", str(result.get("reason") or "a shared hook blocked it")
    specific: dict[str, Any] = {}
    if event in ("PreToolUse", "PermissionRequest") and decision in ("allow", "ask"):
        if target == "codex" and decision == "ask":
            pass  # Codex has no ask: no decision, and the group's allow and rewrite go with it
        else:
            specific["permissionDecision"] = decision
            if result.get("reason"):
                specific["permissionDecisionReason"] = result["reason"]
            if decision == "allow" and result.get("updated"):
                updated = dict(result["updated"])
                if original.get("tool_name") == "Bash" and "command" in updated:
                    updated = {**(original.get("tool_input") or {}), **updated}
                specific["updatedInput"] = updated
    if result.get("context"):
        specific["additionalContext"] = result["context"]
    out: dict[str, Any] = {}
    if specific:
        out["hookSpecificOutput"] = {"hookEventName": event, **specific}
    if result.get("message"):
        out["systemMessage"] = result["message"]
    return 0, json.dumps(out) if out else "", ""


# The entry point -------------------------------------------------------------------------------


def _neutral(target: str) -> int:
    if target in NEUTRAL:
        sys.stdout.write(json.dumps(NEUTRAL[target]) + "\n")
    return 0


def spec_path(home: str, name: str) -> Path:
    return Path(home) / "state" / "share" / "hooks" / f"{name}.json"


def main(argv: list[str]) -> int:
    name = argv[0] if argv else ""
    target = argv[argv.index("--to") + 1] if "--to" in argv[:-1] else "claude"
    wanted = argv[argv.index("--hash") + 1] if "--hash" in argv[:-1] else ""
    try:
        if int(os.environ.get("AGT_DEPTH") or 0) > 0 or os.environ.get("AGT_JOB_ID"):
            return _neutral(target)  # workers run behind their own gate, never your hooks
        home = os.environ.get("AGT_HOME") or str(Path.home() / ".agents-router")
        spec = json.loads(spec_path(home, os.path.basename(name)).read_text(encoding="utf-8"))
        event = spec["event"]
        now = identity(event, spec["matcher"], spec["handlers"], spec.get("folder") or "")
        if not wanted or wanted not in (spec.get("approved") or []) or not now.startswith(wanted):
            return _neutral(target)  # changed since you approved it (or its scripts did): it waits for you
        started = time.monotonic()
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        shared = common(payload if isinstance(payload, dict) else {}, target, event)
        answers = run_all(spec, shared, started + float(spec.get("deadline") or DEFAULT_TIMEOUT))
        code, out, err = emit(combine(answers), target, event, shared)
    except Exception:
        return _neutral(target)  # never block on agt's own failure
    if out:
        sys.stdout.write(out + "\n")
    if err:
        sys.stderr.write(err + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
