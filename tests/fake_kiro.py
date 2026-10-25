"""Stand-in for `kiro-cli` used by the tests (agt must never run the real one, and never `kiro-cli login`).

  chat --no-interactive ...  reads the task on stdin and prints a plain-text answer
  whoami --format json       logged in while FAKE_KIRO_LOGGED_OUT isn't set
  --version                  prints a version

  FAKE_KIRO_SCENARIO   success (default) | limit | auth | mcp | silent
  FAKE_CLAUDE_RECORD   file to append {"argv", "env", "stdin"} records to (shared with the other fakes)
"""

import json
import os
import sys


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print("kiro-cli 2.30.0")
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
    prompt = sys.stdin.read()
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "env": dict(os.environ), "stdin": prompt}) + "\n")
    scenario = os.environ.get("FAKE_KIRO_SCENARIO", "success")
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
    print("Looked at it.")
    print(f"Done: {task}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
