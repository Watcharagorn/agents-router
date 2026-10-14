"""Stand-in for `codex exec --json` used by the tests.

  FAKE_CODEX_LIMITED   comma-separated CODEX_HOME folder names that are "out of quota"
  FAKE_CLAUDE_RECORD   file to append {"argv", "env", "stdin"} records to (shared with fake_claude)
"""

import json
import os
from pathlib import Path
import sys


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
    emit({"type": "thread.started", "thread_id": f"thread-{account}"})
    emit({"type": "turn.started"})
    if account in limited:
        emit({"type": "error", "message": "You've hit your usage limit. Try again at 3:05 PM."})
        emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 3:05 PM."}})
        return 1
    emit({"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "ls"}})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": f"codex done on {account}"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
