"""Stand-in for the plugin and login-status commands of `claude`, `codex` and `agy`.

The first argument picks the CLI (claude, codex or agy). State lives in a JSON file
in the CLI's config folder: CLAUDE_CONFIG_DIR, CODEX_HOME, or FAKE_AGY_HOME for agy.
A file named `logged_in` in that folder means the folder is logged in.

  FAKE_HOST_FAIL   a subcommand that exits 1 (e.g. "install", "add" or "add-json")

`claude mcp add-json|remove --scope user` edit `mcpServers` in `.claude.json`.
  FAKE_HOST_LOG    file to append each argv to, one JSON list per line
  FAKE_HOST_FAIL_ONCE  a file; while it exists, the next `claude mcp add-json` fails and removes it
"""

import json
import os
from pathlib import Path
import sys

REPO = "Watcharagorn/agents-router"
PLUGIN_ID = "agents-router@agents-router"


def main():
    kind, argv = sys.argv[1], sys.argv[2:]
    # Unset, each CLI's own default under HOME (a throwaway folder in the tests), never the working directory.
    folder = Path({"claude": os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude",
                   "codex": os.environ.get("CODEX_HOME") or Path.home() / ".codex",
                   "agy": os.environ.get("FAKE_AGY_HOME") or Path.home() / ".gemini"}[kind])
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
    if kind == "claude" and argv[:1] == ["mcp"]:
        return claude_mcp(argv[1:])
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


def claude_mcp(argv):
    """`claude mcp add-json|remove --scope user`, in `.claude.json` where Claude Code keeps it.

    With CLAUDE_CONFIG_DIR that's the folder's own `.claude.json`; without it, `~/.claude.json`.
    """

    config = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(config) / ".claude.json" if config else Path.home() / ".claude.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    command, rest = argv[0], [arg for arg in argv[1:] if arg not in ("--scope", "-s", "user")]
    once = os.environ.get("FAKE_HOST_FAIL_ONCE")  # a file: while it's there, add-json fails once and removes it
    if command == os.environ.get("FAKE_HOST_FAIL") or (command == "add-json" and once and Path(once).exists()):
        if once:
            Path(once).unlink(missing_ok=True)
        print(f"error: {command} failed on purpose", file=sys.stderr)
        return 1
    servers = data.setdefault("mcpServers", {})
    if command == "add-json":
        name, body = rest
        if name in servers:
            print(f"MCP server {name} already exists in user config", file=sys.stderr)
            return 1
        entry = json.loads(body)
        servers[name] = {"type": "stdio", **entry} if "command" in entry else entry  # Claude adds the type
    elif command == "remove":
        if rest[0] not in servers:
            print(f"No user-scoped MCP server found with name: {rest[0]}", file=sys.stderr)
            return 1
        del servers[rest[0]]
    else:
        print(f"fake claude: unsupported mcp {argv}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
