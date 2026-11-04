"""Stand-in for `kiro-cli` used by the tests (agt must never run the real one, and never `kiro-cli login`).

  chat --no-interactive ...  reads the task on stdin and prints a plain-text answer, or with
                             `--output-format stream-json` the JSON Lines events of the engine it runs
                             (`--v3`: `sess_<id>` sessions and `[INFO]` lines on stderr; `--v2` or none
                             below 3.0: plain-uuid sessions and `metadata` events). `--resume-id ID` keeps
                             ID in every event, and an engine refuses the other engine's session ids
  chat --help                lists chat's flags, FAKE_KIRO_FLAGS among them
  whoami --format json       logged in while FAKE_KIRO_LOGGED_OUT isn't set
  --version                  prints `kiro-cli <FAKE_KIRO_VERSION>`

  FAKE_KIRO_SCENARIO   success (default) | limit | auth | mcp | silent | json-answer | help-fails | help-hangs; with
                       stream-json also truncated (finalTextTruncated) | run-error (runFinished status
                       "error", exit 0) | no-finish (no runFinished, exit 1)
  FAKE_KIRO_DELAY      seconds a run waits before its answer (stream-json: before its runFinished): a slow job
  FAKE_KIRO_VERSION    the version `--version` prints (default 2.30.0)
  FAKE_KIRO_FLAGS      comma-separated flags out of --v2,--v3,--output-format that `chat --help` lists and
                       `chat` takes (default none, like a kiro-cli older than the V3 engine); "kiro-2.37.1" and
                       "kiro-3.0.0" in FAKE_KIRO_PRESET set version and flags together
  FAKE_KIRO_ARGV_LOG   file to append every argv to, one JSON list per line (probes included)
  FAKE_CLAUDE_RECORD   file to append {"argv", "env", "stdin"} records of chat runs to (shared with the other fakes)
"""

import json
import os
import sys
import time
import uuid

ENGINE_FLAGS = ("--v2", "--v3", "--output-format")
PRESETS = {"kiro-2.37.1": ("2.37.1", "--v2,--v3,--output-format"), "kiro-3.0.0": ("3.0.0", "--v2,--v3,--output-format")}


def advertised():
    version, flags = PRESETS.get(os.environ.get("FAKE_KIRO_PRESET", ""), (None, None))
    version = os.environ.get("FAKE_KIRO_VERSION") or version or "2.30.0"
    flags = os.environ.get("FAKE_KIRO_FLAGS", flags or "")
    return version, [flag.strip() for flag in flags.split(",") if flag.strip()]


def main():
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_KIRO_ARGV_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    version, flags = advertised()
    scenario = os.environ.get("FAKE_KIRO_SCENARIO", "success")
    if argv[:1] == ["--version"]:
        print(f"kiro-cli {version}")
        return 0
    if argv[:1] == ["chat"] and ("--help" in argv or "-h" in argv):
        if scenario == "help-fails":
            print("error: something broke", file=sys.stderr)
            return 1
        if scenario == "help-hangs":
            time.sleep(30)
        print("Usage: kiro-cli chat [OPTIONS] [INPUT]\n\nOptions:")
        print("      --no-interactive        Print the first response to STDOUT without interactive mode")
        print("      --trust-all-tools       Allows the model to use any tool without confirmation")
        print("      --trust-tools <TOOLS>   Trust only this set of tools")
        print("      --agent-engine <ENGINE> The agent engine (v1, v2 or v3)")
        texts = {"--v2": "Launch the pre-3.0 default agent", "--v3": "Launch the next generation Kiro agent",
                 "--output-format <FORMAT>": "text or stream-json"}
        for flag in flags:
            shown = "--output-format <FORMAT>" if flag == "--output-format" else flag
            print(f"      {shown:<24}{texts.get(shown, '')}")
        return 0
    if argv[:1] == ["whoami"]:
        if os.environ.get("FAKE_KIRO_LOGGED_OUT"):
            print("Not logged in. Run kiro-cli login to sign in.", file=sys.stderr)
            return 1
        print(json.dumps({"accountType": "BuilderId", "email": "dev@example.com"}))
        return 0
    if argv[:1] in (["login"], ["logout"]):
        print("fake kiro-cli: tests must never log in or out", file=sys.stderr)
        return 99
    if argv[:1] != ["chat"] or "--no-interactive" not in argv:
        print(f"fake kiro-cli: unsupported {argv}", file=sys.stderr)
        return 2
    unknown = [arg for arg in argv if arg.split("=")[0] in ENGINE_FLAGS and arg.split("=")[0] not in flags]
    if unknown:
        print(f"error: unexpected argument '{unknown[0]}' found", file=sys.stderr)
        return 2
    prompt = sys.stdin.read()
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "env": dict(os.environ), "stdin": prompt}) + "\n")
    if scenario == "limit":
        print("You have reached the usage limit for this month.", file=sys.stderr)
        return 1
    if scenario == "auth":
        print("error: Not logged in. Run kiro-cli login or set KIRO_API_KEY.", file=sys.stderr)
        return 1
    if scenario == "mcp":
        print("error: MCP server notes failed to start", file=sys.stderr)
        return 3
    if scenario == "silent":
        return 0
    task = prompt.strip().splitlines()[-1] if prompt.strip() else ""
    if "--output-format" in argv and argv[argv.index("--output-format") + 1:][:1] == ["stream-json"]:
        return stream(argv, version, scenario, task)
    if scenario == "json-answer":
        print(json.dumps({"type": "record", "data": {"value": 7}}))
        return 0
    print("Looked at it.", flush=True)
    time.sleep(float(os.environ.get("FAKE_KIRO_DELAY") or 0))
    print(f"Done: {task}")
    return 0


def emit(kind, data):
    print(json.dumps({"type": kind, "data": data}), flush=True)


def stream(argv, version, scenario, task):
    """The probed stream-json shapes (kiro-cli 2.37.1), trimmed, with placeholder ids and file names."""

    engine = "v3" if "--v3" in argv else "v2" if "--v2" in argv else ("v3" if int(version.split(".")[0]) >= 3 else "v2")
    resume = argv[argv.index("--resume-id") + 1] if "--resume-id" in argv else None
    if resume and resume.startswith("sess_") != (engine == "v3"):
        print(f"error: no session {resume} found", file=sys.stderr)
        return 1
    session = resume or (f"sess_{uuid.uuid4()}" if engine == "v3" else str(uuid.uuid4()))
    if engine == "v3":
        for line in ("[INFO] kiro_agent::engine: starting the V3 agent", "[INFO] kiro_agent::mcp: 0 servers"):
            print(f"2026-11-04T10:00:00Z {line}", file=sys.stderr, flush=True)

    def update(**fields):
        emit("sessionUpdate", {"sessionId": session, "update": fields})

    emit("runStarted", {"payloadSchema": "acp", "acpProtocolVersion": 1, "engine": engine})
    update(sessionUpdate="available_commands_update", availableCommands=[])
    update(sessionUpdate="agent_message_chunk", content={"type": "text", "text": "Looking at it."})
    update(sessionUpdate="tool_call", toolCallId="toolu_1", title="Read File", kind="read")
    update(sessionUpdate="tool_call_update", toolCallId="toolu_1", status="completed")
    update(sessionUpdate="agent_message_chunk", content={"type": "text", "text": "Done: "})
    update(sessionUpdate="agent_message_chunk", content={"type": "text", "text": task})
    update(sessionUpdate="session_info_update", _meta={"kiro": {"kind": "turn_completion"}})
    if engine == "v2":
        emit("metadata", {"sessionId": session, "meteringUsage": [{"value": 0.08, "unit": "credit"}]})
    time.sleep(float(os.environ.get("FAKE_KIRO_DELAY") or 0))
    if scenario == "no-finish":
        print("error: the connection to Kiro was reset", file=sys.stderr)
        return 1
    final = {"sessionId": session, "status": "success", "stopReason": "end_turn", "finalText": f"Done: {task}",
             "finalTextTruncated": False}
    if scenario == "truncated":
        final.update(finalText="Done: ", finalTextTruncated=True)
    if scenario == "run-error":
        final.update(status="error", stopReason="error", finalText="")
    emit("runFinished", final)
    return 0


if __name__ == "__main__":
    sys.exit(main())
