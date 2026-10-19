"""Stand-in for `agy --output-format stream-json --print=...` used by the tests.

  FAKE_AGY_SCENARIO  success (default) | quota | login_prompt | denied | success_exit_1 |
                    success_pause | success_quota | success_auth |
                    early_auth (stdout events, then the login prompt; keeps running)
  FAKE_AGY_LIMITED   comma-separated account folder names (HOME basenames) that report a quota error
  FAKE_AGY_TOOLS     JSON list of tool calls ({"name", "args"}) the "model" makes. Like agy, the fake
                     runs every PreToolUse hook in HOME/.gemini/config/hooks.json for each call and
                     lists the decisions in its answer ("<name>: <decision>").
  FAKE_AGY_REVIEW    the answer the fake gives as a reviewer (a plan-mode run whose prompt starts like
                     the review gate's prompt); default: allow, low risk.

Like agy, it treats SSH_CONNECTION as "store the login in a file under HOME": then a
missing `.gemini/antigravity-cli/antigravity-oauth-token` under HOME (USERPROFILE on
Windows) makes it print the headless login prompt on stderr and wait, as agy does.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

TOKEN = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"
REVIEW_PROMPT = "You are a security reviewer"


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def wait_for_login():
    sys.stderr.write("Authentication required. Please visit the URL to log in:\n")
    sys.stderr.write("https://accounts.google.com/o/oauth2/auth?fake=1\n")
    sys.stderr.flush()
    time.sleep(60)  # agy waits for a code until --print-timeout
    return 1


def run_hooks(home, call, conversation, step):
    """Run each PreToolUse hook command like agy does; return the last decision."""

    path = home / ".gemini" / "config" / "hooks.json"
    try:
        sets = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "no-hooks"
    payload = {
        "toolCall": call, "stepIdx": step, "conversationId": conversation, "workspacePaths": [os.getcwd().replace("\\", "/")],
        "artifactDirectoryPath": str(home / ".gemini" / "antigravity-cli" / "brain" / conversation).replace("\\", "/"),
        "transcriptPath": "", "modelName": "gemini-test",
    }
    decision = "allow"
    for hook_set in sets.values():
        for entry in (hook_set or {}).get("PreToolUse") or []:
            for hook in entry.get("hooks") or []:
                try:
                    done = subprocess.run(
                        hook["command"], shell=True, input=json.dumps(payload), capture_output=True, text=True,
                        timeout=hook.get("timeout", 30), cwd=str(path.parent),
                    )
                    answer = json.loads(done.stdout) if done.returncode == 0 else {"decision": "hook-error"}
                except (subprocess.TimeoutExpired, ValueError):
                    answer = {"decision": "hook-error"}
                decision = answer.get("decision") or "?"
                if decision == "deny":
                    return decision
    return decision


def main():
    argv = sys.argv[1:]
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    prompt = next((item[len("--print="):] for item in argv if item.startswith("--print=")), "")
    reviewer = prompt.startswith(REVIEW_PROMPT)
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "env": dict(os.environ), "stdin": "", "cwd": os.getcwd(), "reviewer": reviewer}) + "\n")
    home = Path(os.environ.get("USERPROFILE" if os.name == "nt" else "HOME") or os.environ.get("HOME") or ".")
    if os.environ.get("FAKE_AGY_SCENARIO") == "login_prompt":
        return wait_for_login()
    scenario = os.environ.get("FAKE_AGY_SCENARIO")
    if scenario in ("early_auth",):
        # Output already written, then the missing-login prompt on stderr.
        emit({"event": "init", "conversation_id": "conv-early", "init": {"model": "gemini-test", "tools": []}})
        emit({"event": "step_update", "step_update": {"conversation_id": "conv-early", "step_index": 90, "state": "DONE", "step_type": "agent_response", "text_delta": "partial done"}})
        return wait_for_login()
    if os.environ.get("SSH_CONNECTION") and not (home / TOKEN).is_file():
        return wait_for_login()
    conversation = argv[argv.index("--conversation") + 1] if "--conversation" in argv else "conv-1"
    emit({"event": "init", "conversation_id": conversation, "init": {"model": "gemini-test", "permission_mode": "request-review", "tools": ["a", "b"]}})
    if reviewer:
        answer = os.environ.get("FAKE_AGY_REVIEW") or '{"decision":"allow","risk":"low","reason":"fake reviewer says fine"}'
        emit({"event": "result", "result": {"conversation_id": "review-1", "status": "SUCCESS", "response": answer + "\n", "num_turns": 1}})
        return 0
    limited = [name for name in (os.environ.get("FAKE_AGY_LIMITED") or "").split(",") if name]
    if os.environ.get("FAKE_AGY_SCENARIO") == "quota" or home.name in limited:
        emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "half done on " + home.name}})
        emit({"event": "result", "result": {"conversation_id": conversation, "status": "ERROR", "error": "Baseline model quota reached"}})
        return 1
    if os.environ.get("FAKE_AGY_SCENARIO") == "denied":
        sys.stderr.write('jetski: no output produced - a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied.\n')
        emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 2, "state": "ERROR", "step_type": "tool", "tool_name": "run_command"}})
        emit({"event": "result", "result": {"conversation_id": conversation, "status": "SUCCESS", "response": "", "denied_actions": [{"action": "command", "display_name": "RunCommand"}]}})
        return 0
    decisions = []
    for step, call in enumerate(json.loads(os.environ.get("FAKE_AGY_TOOLS") or "[]"), start=2):
        emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": step, "state": "ACTIVE", "step_type": "tool", "tool_name": call["name"], "tool_info": {"name": call["name"], "parameters": call.get("args") or {}}}})
        decisions.append(f"{call['name']}: {run_hooks(home, call, conversation, step)}")
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 1, "state": "ACTIVE", "step_type": "tool_call"}})
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 90, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "gemini "}})
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 90, "state": "DONE", "step_type": "agent_response", "text_delta": "done"}})
    response = "gemini done\n" + "".join(line + "\n" for line in decisions)
    emit({"event": "result", "result": {"conversation_id": conversation, "status": "SUCCESS", "response": response, "num_turns": 1}})
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "success")
    if scenario == "success_pause":
        time.sleep(120)
    if scenario in ("success_exit_1", "success_quota", "success_auth"):
        sys.stderr.write({"success_exit_1": "late CLI failure\n", "success_quota": "quota reached\n",
                          "success_auth": "Unauthenticated\n"}[scenario])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
