"""Stand-in for `agy --output-format stream-json --print=...` used by the tests.

  FAKE_AGY_SCENARIO  success (default) | quota
"""

import json
import os
import sys


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "env": dict(os.environ), "stdin": ""}) + "\n")
    conversation = argv[argv.index("--conversation") + 1] if "--conversation" in argv else "conv-1"
    emit({"event": "init", "conversation_id": conversation, "init": {"model": "gemini-test", "permission_mode": "accept-edits", "tools": ["a", "b"]}})
    if os.environ.get("FAKE_AGY_SCENARIO") == "quota":
        emit({"event": "result", "result": {"conversation_id": conversation, "status": "ERROR", "error": "Baseline model quota reached"}})
        return 1
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 1, "state": "ACTIVE", "step_type": "tool_call"}})
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 2, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "gemini "}})
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 2, "state": "DONE", "step_type": "agent_response", "text_delta": "done"}})
    emit({"event": "result", "result": {"conversation_id": conversation, "status": "SUCCESS", "response": "gemini done\n", "num_turns": 1}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
