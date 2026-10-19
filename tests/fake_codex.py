"""Stand-in for `codex exec --json` used by the tests.

  FAKE_CODEX_LIMITED   comma-separated CODEX_HOME folder names that are "out of quota"
  FAKE_CODEX_SCENARIO  success (default) | recovered_errors | success_exit_1 | success_pause |
                      success_quota | success_auth | success_failed | capacity | capacity_no_session
  FAKE_CODEX_CAPACITY_FAILURES  number of capacity failures before success (default 100)
  FAKE_CLAUDE_RECORD   file to append {"argv", "env", "stdin"} records to (shared with fake_claude)
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
    account = Path(os.environ.get("CODEX_HOME", "default")).name
    limited = [item for item in os.environ.get("FAKE_CODEX_LIMITED", "").split(",") if item]
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "success")
    capacity = False
    if scenario in ("capacity", "capacity_no_session"):
        counter = Path(os.environ["CODEX_HOME"]) / "fake-capacity-attempts"
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        capacity = count < int(os.environ.get("FAKE_CODEX_CAPACITY_FAILURES", "100"))
        if capacity and scenario == "capacity_no_session":
            sys.stderr.write("Selected model is at capacity. Please try a different model.\n")
            return 1
    emit({"type": "thread.started", "thread_id": f"thread-{account}"})
    emit({"type": "turn.started"})
    if capacity:
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": "partial progress"}})
        emit({"type": "error", "message": "Selected model is at capacity. Please try a different model."})
        emit({"type": "turn.failed", "error": {"message": "Selected model is at capacity. Please try a different model."}})
        return 1
    if account in limited:
        emit({"type": "error", "message": "You've hit your usage limit. Try again at 3:05 PM."})
        emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 3:05 PM."}})
        return 1
    if scenario == "recovered_errors":
        emit({"type": "error", "message": "Reconnecting... 429 Too Many Requests, retrying"})
        emit({"type": "item.completed", "item": {"type": "error", "message": "model rerouted"}})
    emit({"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "ls"}})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": f"codex done on {account}"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
    if scenario == "success_pause":
        time.sleep(120)
    if scenario == "success_failed":
        emit({"type": "turn.failed", "error": {"message": "late turn failure"}})
        return 1
    if scenario in ("success_exit_1", "success_quota", "success_auth"):
        sys.stderr.write({"success_exit_1": "late CLI failure\n", "success_quota": "usage limit reached\n",
                          "success_auth": "not logged in\n"}[scenario])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
