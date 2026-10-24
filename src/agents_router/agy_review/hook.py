"""The PreToolUse hook: read agy's JSON on stdin, print a decision on stdout, always exit 0.

Outside a reviewed job (no AGY_REVIEW_CONTEXT in the environment) it answers
"allow", which hands the call back to agy's own permission policy, so a
`agt launch` session in the same folder behaves as usual. Inside a reviewed job
it only ever answers "auto_approve" or "deny": a plain "allow" would let agy's
headless mode refuse the call and end the run without an answer. Any error,
timeout or unreadable reviewer answer becomes a denial.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import secrets
import sys
import time
from typing import Any

from . import reviewer, rules, store
from .context import ContextError, load_context

MAX_INPUT = 512 * 1024
MAX_REASON = 600
GATE_LABEL = "Agents Router review"


def _answer(decision: str, reason: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"decision": decision}
    if reason:
        out["reason"] = reason[:MAX_REASON]
    return out


def _deny(reason: str) -> dict[str, Any]:
    return _answer("deny", f"{GATE_LABEL}: denied, {reason}. Don't retry the same action; find another way or say in your answer what you couldn't do.")


def parse_payload(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("hook input is empty or too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("toolCall"), dict):
        raise ValueError("hook input has no toolCall")
    call = payload["toolCall"]
    if not isinstance(call.get("name"), str) or not call["name"]:
        raise ValueError("toolCall.name is missing")
    if not isinstance(call.get("args", {}), dict):
        raise ValueError("toolCall.args is not an object")
    return payload


def evaluate(payload: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(answer for agy, log record) for one tool call."""

    call = payload["toolCall"]
    name = call["name"]
    args = call.get("args") or {}
    record: dict[str, Any] = {
        "id": secrets.token_hex(4), "time": time.time(), "tool": name, "summary": rules.summarize(call),
        "step": payload.get("stepIdx"),
    }
    verdict = rules.decide(call, payload, context)
    if verdict.action == "approve":
        record.update(decision="approve", source="rule", reason=verdict.reason, quiet=verdict.quiet)
        return _answer("auto_approve", f"{GATE_LABEL}: {verdict.reason}"), record
    if verdict.action == "deny":
        record.update(decision="deny", source="rule", reason=verdict.reason)
        return _deny(verdict.reason), record

    record["why"] = verdict.why
    config = dict(context.get("reviewer") or {})
    if str(config.get("kind") or "off") == "off":
        record.update(decision="deny", source="rule", reason=f"no reviewer is configured, and {verdict.why}")
        return _deny(record["reason"]), record
    cache_path = Path(context["cache"]) if context.get("cache") else None
    key = store.action_key(name, args)
    cached = store.cache_get(cache_path, key) if cache_path else None
    if cached:
        record.update(decision=cached["decision"], source="cache", reason=cached.get("reason", ""), risk=cached.get("risk"))
    else:
        started = time.monotonic()
        request = {
            "tool": name, "args": args, "access": context.get("access"), "platform": platform.system(),
            "workspaces": context.get("workspaces"), "sandboxed": bool(context.get("sandbox")) and not rules._bypass(args)
            if name in rules.COMMAND_TOOLS else None, "why": verdict.why,
        }
        try:
            answer = reviewer.review(config, request)
        except reviewer.ReviewError as exc:
            record.update(decision="deny", source="error", reason=str(exc), seconds=round(time.monotonic() - started, 1))
            return _deny(f"the reviewer could not decide ({exc})"), record
        decision = "approve" if answer["decision"] == "allow" else "deny"
        record.update(decision=decision, source="reviewer", reason=answer["reason"], risk=answer["risk"],
                      seconds=round(time.monotonic() - started, 1), reviewer=config.get("kind"), model=config.get("model"))
        if cache_path:
            try:
                store.cache_put(cache_path, key, {"decision": decision, "reason": answer["reason"], "risk": answer["risk"]})
            except (OSError, TimeoutError):
                pass
    if record["decision"] == "approve":
        return _answer("auto_approve", f"{GATE_LABEL}: approved by the reviewer ({record.get('risk')} risk)"), record
    return _deny(f"the reviewer said: {record['reason']}"), record


def run(raw: bytes, env: dict[str, str]) -> dict[str, Any]:
    """The whole hook, minus stdin and stdout. Never raises."""

    if env.get(reviewer.ROLE_ENV) == "reviewer":
        return _deny("the reviewer has no tools; answer with the JSON object only")
    context_path = env.get(reviewer.CONTEXT_ENV)
    if not context_path:
        return _answer("allow")
    record: dict[str, Any] | None = None
    context: dict[str, Any] = {}
    try:
        context = load_context(Path(context_path))
        payload = parse_payload(raw)
        answer, record = evaluate(payload, context)
    except (ContextError, ValueError, KeyError, TypeError, OSError) as exc:
        answer = _deny(f"the review gate failed and fails closed ({type(exc).__name__})")
        record = {"id": secrets.token_hex(4), "time": time.time(), "tool": "?", "decision": "deny", "source": "error",
                  "reason": f"{type(exc).__name__}: {exc}"[:300]}
    except Exception as exc:  # anything unexpected still ends in a denial
        answer = _deny(f"the review gate failed and fails closed ({type(exc).__name__})")
        record = {"id": secrets.token_hex(4), "time": time.time(), "tool": "?", "decision": "deny", "source": "error",
                  "reason": f"{type(exc).__name__}: {exc}"[:300]}
    if record is not None and context.get("log"):
        try:
            store.log_append(Path(context["log"]), record)
        except Exception:
            pass
    return answer


def main(argv: list[str] | None = None) -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    except Exception:
        raw = b""
    try:
        answer = run(raw, dict(os.environ))
    except Exception:
        answer = _deny("the review gate failed and fails closed")
    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()
    return 0
