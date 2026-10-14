"""Stand-in for `claude -p --output-format stream-json` used by the tests.

Behaviour is chosen by environment variables:
  FAKE_CLAUDE_LIMITED   comma-separated account folder names that are "out of quota"
  FAKE_CLAUDE_SCENARIO  success (default) | auth | slow | crash
  FAKE_CLAUDE_RECORD    file to append {"argv", "env", "stdin"} records to
"""

import json
import os
from pathlib import Path
import sys
import time


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "env": dict(os.environ), "stdin": prompt}) + "\n")

    account = Path(os.environ.get("CLAUDE_CONFIG_DIR", "default")).name
    session = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "resumed-session"
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "success")
    limited = [item for item in os.environ.get("FAKE_CLAUDE_LIMITED", "").split(",") if item]

    if scenario == "crash":
        sys.stderr.write("boom\n")
        return 3

    emit({
        "type": "system", "subtype": "init", "session_id": session, "model": "claude-haiku-5-5",
        "permissionMode": argv[argv.index("--permission-mode") + 1] if "--permission-mode" in argv else "default",
        "claude_code_version": "9.9.9", "apiKeySource": "none",
        "mcp_servers": [{"name": "shared-server", "status": "connected", "source": "user"}],
        "plugins": [{"name": "demo-plugin", "path": "x"}], "skills": ["alpha"], "agents": ["general-purpose"],
    })
    if scenario == "slow":
        time.sleep(120)
    if account in limited:
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": int(time.time()) + 3600, "rateLimitType": "five_hour",
            "utilization": 1.0, "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": int(time.time()) + 3600}},
        }, "session_id": session})
        emit({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
              "result": "You've hit your session limit · resets 3:45pm", "session_id": session})
        return 1
    if scenario == "badevent":
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": "2026-10-14T10:00:00Z",
              "unifiedWindows": {"five_hour": {"utilization": "n/a"}}}, "session_id": session})
        emit(["not", "an", "object"])
    if scenario == "auth":
        emit({"type": "result", "subtype": "success", "is_error": True,
              "result": "Invalid API key · Please run /login", "session_id": session})
        return 1
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {}},
        {"type": "text", "text": f"done on {account}"},
    ]}, "session_id": session})
    emit({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.25, "resetsAt": 1}, "seven_day": {"utilization": 0.5, "resetsAt": 2}},
    }, "session_id": session})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": f"done on {account}",
          "session_id": session, "total_cost_usd": 0.01, "num_turns": 1})
    return 0


if __name__ == "__main__":
    sys.exit(main())
