"""Stand-in for `agy --output-format stream-json --print=...` used by the tests.

  FAKE_AGY_SCENARIO  success (default) | quota | login_prompt
  FAKE_AGY_LIMITED   comma-separated account folder names (HOME basenames) that report a quota error

Like agy, it treats SSH_CONNECTION as "store the login in a file under HOME": then a
missing `.gemini/antigravity-cli/antigravity-oauth-token` under HOME (USERPROFILE on
Windows) makes it print the headless login prompt on stderr and wait, as agy does.
"""

import json
import os
from pathlib import Path
import sys
import time

TOKEN = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def wait_for_login():
    sys.stderr.write("Authentication required. Please visit the URL to log in:\n")
    sys.stderr.write("https://accounts.google.com/o/oauth2/auth?fake=1\n")
    sys.stderr.flush()
    time.sleep(60)  # agy waits for a code until --print-timeout
    return 1


def main():
    argv = sys.argv[1:]
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "env": dict(os.environ), "stdin": "", "cwd": os.getcwd()}) + "\n")
    home = Path(os.environ.get("USERPROFILE" if os.name == "nt" else "HOME") or os.environ.get("HOME") or ".")
    if os.environ.get("FAKE_AGY_SCENARIO") == "login_prompt":
        return wait_for_login()
    if os.environ.get("SSH_CONNECTION") and not (home / TOKEN).is_file():
        return wait_for_login()
    conversation = argv[argv.index("--conversation") + 1] if "--conversation" in argv else "conv-1"
    emit({"event": "init", "conversation_id": conversation, "init": {"model": "gemini-test", "permission_mode": "accept-edits", "tools": ["a", "b"]}})
    limited = [name for name in (os.environ.get("FAKE_AGY_LIMITED") or "").split(",") if name]
    if os.environ.get("FAKE_AGY_SCENARIO") == "quota" or home.name in limited:
        emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "half done on " + home.name}})
        emit({"event": "result", "result": {"conversation_id": conversation, "status": "ERROR", "error": "Baseline model quota reached"}})
        return 1
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 1, "state": "ACTIVE", "step_type": "tool_call"}})
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 2, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "gemini "}})
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 2, "state": "DONE", "step_type": "agent_response", "text_delta": "done"}})
    emit({"event": "result", "result": {"conversation_id": conversation, "status": "SUCCESS", "response": "gemini done\n", "num_turns": 1}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
