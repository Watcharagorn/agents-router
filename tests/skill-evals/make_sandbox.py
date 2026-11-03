"""Build the throwaway agt setup a skill-eval case runs in: `.agt-eval/` in the case's workspace.

Each case's place-files.sh runs this in the workspace after copying the case's files/ there:

    python3 make_sandbox.py jobs   # fake_claude, fake_codex, fake_agy, fake_kiro stand in for the CLIs
    python3 make_sandbox.py host   # fake_host stands in for them (agt setup: plugin installs, logins)

It copies agt's code from this clone and the fake CLIs into `.agt-eval/`, because the eval's
sandboxed shell can read the case workspace but not the clone, and writes `.agt-eval/env.json`:
the variables `bin/agt` sets for agt and nothing else, as tests/helpers.py does for the unit
tests. A case's files/ may already hold `.agt-eval/home/...` (a starting `~/.agt/settings.toml`);
those files are kept.

The accounts a case's roles name (`[roles.*] providers` in its user and workspace settings) are made
the way agt finds them on a real machine, logged in to the fake CLIs:
  claude:<name>        a Claude Code profile folder `~/.claude-<name>` (`~/.<name>` when the name starts
                       with `claude`) with an `oauthAccount` in its `.claude.json`: agt finds it itself
  codex:<name>         a `[[codex.accounts]]` table appended to `~/.agt/settings.toml`, as
                       `agt accounts add codex <name>` registers it, with its folder under AGT_HOME
                       (a case may register one itself, with no `home`, to have it without naming it in a role)
  <runtime>:primary    the runtime's own folder (`~/.claude`, `~/.codex`, `~/.gemini/antigravity-cli`, `~/.kiro`);
                       in jobs mode every runtime's, as on a machine with all four CLIs logged in
So a case stays self-describing: its settings name the accounts, and they exist. Nothing here touches
the real HOME, ~/.agt, ~/.claude* or ~/.codex.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

CLONE = Path(__file__).resolve().parents[2]
FAKES = ("fake_claude.py", "fake_codex.py", "fake_agy.py", "fake_kiro.py", "fake_host.py", "fake_launchd.py")
# jobs mode: fake_codex and fake_agy only run jobs, so the read-only login checks agt makes go here first
# (`fakes/logins.py codex|agy ...`): `codex login status` answers as fake_host does (the folder's `logged_in`
# file), `agy models` lists models when ~/.gemini/antigravity-cli has one (agt's login check for the Antigravity
# primary); the rest goes to the fake.
LOGINS = """import os, sys
here = os.path.dirname(os.path.abspath(__file__))
cli, args = sys.argv[1], sys.argv[2:]
if cli == "codex" and args[:2] == ["login", "status"]:
    os.execv(sys.executable, [sys.executable, os.path.join(here, "fake_host.py"), "codex", *args])
if cli == "agy" and args[:1] == ["models"]:
    if not os.path.exists(os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "logged_in")):
        sys.exit("Authentication required.")
    print("gemini-3.8-pro    Gemini 3.8 Pro\\ngemini-3.8-flash  Gemini 3.8 Flash")
    sys.exit(0)
fake = {"codex": "fake_codex.py", "agy": "fake_agy.py"}[cli]
os.execv(sys.executable, [sys.executable, os.path.join(here, fake), *args])
"""


def named_accounts(workspace: Path, home: Path, src: Path) -> set[tuple[str, str]]:
    """(runtime, account) for every provider the case's settings name; `primary` when a provider names none."""

    import tomllib  # here, not at the top: build() first says why Python older than 3.11 can't run agt

    sys.path.insert(0, str(src))
    from agents_router.roles import RoleError, parse_provider  # agt's own grammar, from the copied code

    found = set()
    for path in (home / ".agt" / "settings.toml", workspace / ".agt" / "settings.toml",
                 workspace / ".agt" / "settings.local.toml"):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue  # missing, or a case about a broken file: agt reports it, nothing to make
        tables = data.get("roles")
        for table in tables.values() if isinstance(tables, dict) else ():
            items = table.get("providers") if isinstance(table, dict) else None
            for text in items if isinstance(items, list) else ():
                try:
                    provider = parse_provider(text)
                except RoleError:
                    continue
                found.add((provider.runtime, provider.account or "primary"))
    return found


def make_accounts(workspace: Path, home: Path, src: Path, every_primary: bool) -> None:
    primaries = {"claude": home / ".claude", "codex": home / ".codex",
                 "antigravity": home / ".gemini" / "antigravity-cli", "kiro": home / ".kiro"}
    settings = home / ".agt" / "settings.toml"
    text = settings.read_text(encoding="utf-8") if settings.is_file() else ""
    registered = _codex_accounts(text)
    added = []
    named = named_accounts(workspace, home, src) | {("codex", name) for name in registered}
    if every_primary:
        named |= {(runtime, "primary") for runtime in primaries}
    for runtime, name in sorted(named):
        if name == "primary":
            primaries[runtime].mkdir(parents=True, exist_ok=True)
            (primaries[runtime] / "logged_in").touch()  # fake_claude, fake_host: this folder is logged in
        elif runtime == "claude":
            folder = home / (f".{name}" if name.startswith("claude") else f".claude-{name}")
            folder.mkdir(exist_ok=True)
            state = {"oauthAccount": {"accountUuid": f"uuid-{name}", "organizationUuid": f"org-{name}",
                                      "organizationType": "claude_max"}, "userID": f"eval-{name}"}
            (folder / ".claude.json").write_text(json.dumps(state), encoding="utf-8")
            (folder / "logged_in").touch()
        elif runtime == "codex":
            if registered.get(name):
                continue  # the case registers it with a folder of its own
            folder = home / ".agents-router" / "homes" / "codex" / name  # agt's default folder for it
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "logged_in").touch()  # fake_host (`codex login status`): this folder is logged in
            if name not in registered:
                added.append(f'\n[[codex.accounts]]\nname = "{name}"\nhome = {json.dumps(str(folder))}\nenabled = true\n')
        else:
            raise SystemExit(f"make_sandbox: {runtime}:{name}: the sandbox makes only {runtime}:primary")
    if added:  # the lines `agt accounts add codex <name>` writes, appended so the case's own lines keep their numbers
        settings.write_text(text + "".join(added), encoding="utf-8")


def _codex_accounts(text: str) -> dict[str, str]:
    """name -> home ("" for agt's default folder) of the `[[codex.accounts]]` the case's settings register."""

    import tomllib

    try:
        items = tomllib.loads(text).get("codex", {}).get("accounts", [])
    except tomllib.TOMLDecodeError:
        return {}
    return {str(item["name"]): str(item.get("home") or "") for item in items if isinstance(item, dict) and "name" in item}


def build(workspace: Path, mode: str) -> Path:
    if sys.version_info < (3, 11):
        raise SystemExit(f"make_sandbox: agt needs Python 3.11 or later, not {sys.version.split()[0]}")
    root = workspace / ".agt-eval"
    home = root / "home"
    for folder in (home / ".agt", home / ".claude", home / ".codex", home / ".gemini", home / ".agents-router",
                   root / "records", root / "fakes"):
        folder.mkdir(parents=True, exist_ok=True)
    # The run can read only its workspace, so its `agt` is this copy: runs put `.agt-eval/bin` first on PATH.
    (root / "bin").mkdir(exist_ok=True)
    shutil.copy2(CLONE / "tests" / "skill-evals" / "bin" / "agt", root / "bin" / "agt")
    shutil.copytree(CLONE / "src" / "agents_router", root / "src" / "agents_router", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in FAKES:
        shutil.copy2(CLONE / "tests" / name, root / "fakes" / name)
    shutil.copytree(CLONE / ".claude-plugin", root / "plugin" / ".claude-plugin", dirs_exist_ok=True)

    python = sys.executable
    fake = lambda name, *args: json.dumps([python, str(root / "fakes" / name), *args])
    if mode == "host":
        for folder in (home / ".claude", home / ".codex", home / ".gemini"):
            (folder / "logged_in").touch()  # fake_host: this folder is logged in
        commands = {"AGT_CLAUDE_COMMAND": fake("fake_host.py", "claude"), "AGT_CODEX_COMMAND": fake("fake_host.py", "codex"),
                    "AGT_AGY_COMMAND": fake("fake_host.py", "agy"), "AGT_KIRO_COMMAND": fake("fake_host.py", "kiro"),
                    # installing agt's plugin records this clone's version, as a real install would
                    "FAKE_HOST_PLUGIN_VERSION": json.loads((CLONE / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]}
    elif mode == "jobs":
        (root / "fakes" / "logins.py").write_text(LOGINS, encoding="utf-8")
        commands = {"AGT_CLAUDE_COMMAND": fake("fake_claude.py"), "AGT_CODEX_COMMAND": fake("logins.py", "codex"),
                    "AGT_AGY_COMMAND": fake("logins.py", "agy"), "AGT_KIRO_COMMAND": fake("fake_kiro.py")}
    else:
        raise SystemExit(f"make_sandbox: mode is jobs or host, not {mode!r}")
    make_accounts(workspace, home, root / "src", every_primary=mode == "jobs")
    records = root / "records"
    env = {
        "HOME": str(home), "USERPROFILE": str(home),
        "AGT_HOME": str(home / ".agents-router"), "AGT_CONFIG_DIR": str(home / ".agt"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"), "FAKE_AGY_HOME": str(home / ".gemini"),
        "AGT_PLUGIN_SOURCE": str(root / "plugin"),
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
        "FAKE_HOST_LOG": str(records / "host.jsonl"), "FAKE_CLAUDE_RECORD": str(records / "workers.jsonl"),
        # never the real launchd, notifications or background runs
        "AGT_LAUNCHCTL": fake("fake_launchd.py", "launchctl"), "AGT_OSASCRIPT": fake("fake_launchd.py", "osascript"),
        "FAKE_LAUNCHCTL_STATE": str(records / "launchctl-state.json"), "FAKE_LAUNCHCTL_RECORD": str(records / "launchctl.jsonl"),
        "FAKE_OSASCRIPT_RECORD": str(records / "osascript.jsonl"), "AGT_SPAWN_RECORD": str(records / "spawned.jsonl"),
        **commands,
    }
    (root / "env.json").write_text(json.dumps({"python": python, "src": str(root / "src"), "env": env}, indent=2) + "\n",
                                   encoding="utf-8")
    return root


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_sandbox.py jobs|host   (run in the case workspace)")
    print(f"agt eval sandbox: {build(Path.cwd(), sys.argv[1])}")
