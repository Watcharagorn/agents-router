"""Stand-in for the plugin and login-status commands of `claude`, `codex` and `agy`.

The first argument picks the CLI (claude, codex or agy). State lives in a JSON file
in the CLI's config folder: CLAUDE_CONFIG_DIR, CODEX_HOME, or FAKE_AGY_HOME for agy.
A file named `logged_in` in that folder means the folder is logged in.

  FAKE_HOST_FAIL   a subcommand that exits 1 (e.g. "install" or "add")
  FAKE_HOST_LOG    file to append each argv to, one JSON list per line
"""

import json
import os
from pathlib import Path
import sys

REPO = "Watcharagorn/agents-router"
PLUGIN_ID = "agents-router@agents-router"


def main():
    kind, argv = sys.argv[1], sys.argv[2:]
    folder = Path({"claude": os.environ.get("CLAUDE_CONFIG_DIR", ""), "codex": os.environ.get("CODEX_HOME", ""),
                   "agy": os.environ.get("FAKE_AGY_HOME", "")}[kind] or "unset-home")
    log = os.environ.get("FAKE_HOST_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, "argv": argv, "folder": str(folder)}) + "\n")
    state_path = folder / "fake_plugins.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"marketplaces": [], "plugins": {}}

    def save():
        folder.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")

    if argv[:1] == ["--version"]:
        print(f"fake-{kind} 1.0")
        return 0
    if kind == "claude" and argv[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": (folder / "logged_in").exists(), "authMethod": "claude.ai"}))
        return 0
    if kind == "codex" and argv[:2] == ["login", "status"]:
        if (folder / "logged_in").exists():
            print("Logged in using ChatGPT")
            return 0
        print("Not logged in")
        return 1
    if argv[:1] != ["plugin"]:
        print(f"fake {kind}: unsupported {argv}", file=sys.stderr)
        return 2
    sub = argv[1:]
    if sub and sub[0] == os.environ.get("FAKE_HOST_FAIL"):
        print(f"error: {sub[0]} failed on purpose", file=sys.stderr)
        return 1
    if sub[:2] == ["marketplace", "list"]:
        items = [{"name": name} for name in state["marketplaces"]]
        print(json.dumps({"marketplaces": items} if kind == "codex" else items))
        return 0
    if sub[:2] == ["marketplace", "add"]:
        if sub[2] != REPO:
            return 1
        if "agents-router" not in state["marketplaces"]:
            state["marketplaces"].append("agents-router")
        save()
        print("Added marketplace agents-router")
        return 0
    if sub[:2] in (["marketplace", "update"], ["marketplace", "upgrade"]):
        return 0 if sub[2] in state["marketplaces"] else 1
    if sub[:1] == ["update"]:
        return 0 if sub[1] in state["plugins"] else 1
    if sub[:1] == ["list"]:
        if kind == "claude":
            print(json.dumps([{"id": key, "enabled": value} for key, value in state["plugins"].items()]))
        elif kind == "codex":
            installed = [{"pluginId": key, "name": key.split("@")[0], "installed": True, "enabled": value}
                         for key, value in state["plugins"].items()]
            print("WARNING: a warning line before the JSON")
            print(json.dumps({"installed": installed, "available": []}))
        else:
            print(json.dumps([{"name": "agents-router", "enabled": value} for value in state["plugins"].values()]))
        return 0
    if sub[:1] in (["install"], ["add"]):
        target = sub[1]
        if kind == "agy":
            if target != f"https://github.com/{REPO}":
                return 1
            state["plugins"]["agents-router"] = True
        else:
            if target != PLUGIN_ID or "agents-router" not in state["marketplaces"]:
                print(f"Plugin {target} not found", file=sys.stderr)
                return 1
            state["plugins"][PLUGIN_ID] = True
        save()
        print("Installed")
        return 0
    if sub[:1] == ["enable"]:
        key = sub[1]
        if key not in state["plugins"]:
            return 1
        state["plugins"][key] = True
        save()
        return 0
    print(f"fake {kind}: unsupported {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
