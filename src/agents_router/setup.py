"""`agt setup`: install the plugin into the host CLIs and register existing account folders.

Plugins use each host's own terminal commands (`claude plugin ...`,
`codex plugin ...`, `agy plugin ...`), so an agent can do the whole install
without slash commands. Codex native agents are installed separately in its
personal agents folder. Antigravity relays use a companion plugin in its global
customizations folder.

Claude Code gets the plugin in each of your profiles (`claude_profiles`): this
session's folder, your registered profiles with their own setup, and the
logged-in `~/.claude*` folders agt finds. Each runs with its own environment, so
`~/.claude` runs without CLAUDE_CONFIG_DIR. When the `claude` binary can't be
found, the plugin is declared in each profile's `settings.json` instead; Claude
Code installs it on its next start. Logins are never run and credential files
are never read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import time
from typing import Any

from agents_router import ui
from agents_router.config import NAME_RE, Account, Config, homes_dir, save_accounts
from agents_router.fsutil import copy_exclusive, is_link, no_window_kwargs, same_path, write_json, write_private
from agents_router.runtimes import get_runtime
from agents_router.runtimes.base import JobSpec, clean_environment

REPO = "Watcharagorn/agents-router"
REPO_URL = f"https://github.com/{REPO}"
MARKETPLACE = "agents-router"
PLUGIN = "agents-router"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
ANTIGRAVITY_RELAYS = "agents-router-relays"
HOSTS = ("claude", "codex", "antigravity")
LABELS = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}
SHORT = {"claude": "claude", "codex": "codex", "antigravity": "agy"}
RELOAD = {
    "claude": "Restart Claude Code (or type /reload-plugins in an open session) so the agents-router plugin loads.",
    "codex": "Start a new Codex session so the agents-router plugin loads.",
    "antigravity": "Start a new Antigravity session so the agents-router plugin loads.",
}
FALLBACK = {
    "claude": "claude mcp add --scope user agents-router -- agt mcp",
    "codex": "codex mcp add agents-router -- agt mcp",
    "antigravity": 'add {"mcpServers": {"agents-router": {"command": "agt", "args": ["mcp"]}}} to ~/.gemini/config/mcp_config.json',
}
# Files that mark a folder as one CLI's config folder. Only their existence is checked.
MARKERS = {
    "claude": (".claude.json", "projects", "settings.json", "statsig"),
    "codex": ("config.toml", "sessions", "auth.json", "version.json"),
}


@dataclass
class Report:
    dry_run: bool = False
    done: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked: list[tuple[str, str]] = field(default_factory=list)  # (what, how it stands): nothing done, nothing wrong
    # Claude Code profiles setup could install into: {"name", "folder", "this", "selected"}
    claude: list[dict[str, Any]] = field(default_factory=list)

    def add_todo(self, text: str) -> None:
        if text not in self.todo:
            self.todo.append(text)


def plugin_source() -> str:
    """Where the plugin comes from: this repository on GitHub, or `AGT_PLUGIN_SOURCE`.

    `AGT_PLUGIN_SOURCE` is a local clone (live checks in a sandbox install the branch you're on
    without pushing it) or another GitHub `owner/repo`.
    """

    value = os.environ.get("AGT_PLUGIN_SOURCE", "").strip()
    if not value:
        return REPO
    folder = Path(value).expanduser()
    return str(folder.resolve()) if folder.is_dir() else value


def _local_source() -> Path | None:
    source = Path(plugin_source())
    return source if source.is_absolute() and source.is_dir() else None


def _agy_source() -> str:
    source = plugin_source()
    return source if _local_source() else REPO_URL if source == REPO else f"https://github.com/{source}"


def run_command(argv: list[str], env: dict[str, str] | None, timeout: int = 300) -> tuple[int, str, str]:
    """Run one host command without a terminal. Returns exit code, stdout and stderr."""

    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, stdin=subprocess.DEVNULL, **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _run_shown(what: str, argv: list[str], env: dict[str, str] | None, shown: str, timeout: int = 300) -> tuple[int, str, str]:
    """`run_command` for a step setup or update reports: a person at a terminal sees it run (`what`, the command and
    its time, gone when it ends); everyone else gets a `$ command` line first, as a log."""

    if not ui.live_on(sys.stderr):
        print(f"$ {shown}", flush=True)
        return run_command(argv, env, timeout)
    words = " ".join(itertools.takewhile(lambda word: word.replace("-", "").isalpha(), shown.split()))
    with ui.spinner([what, "   ", ui.muted("running "), ui.code(words)]):
        return run_command(argv, env, timeout)


def _json(text: str) -> Any:
    """The JSON value in a command's stdout, ignoring any warning lines before it."""

    for start, char in enumerate(text):
        if char in "[{":
            try:
                return json.loads(text[start:])
            except ValueError:
                continue
    return None


def _last_line(stdout: str, stderr: str) -> str:
    lines = [line.strip() for line in (stderr + "\n" + stdout).splitlines() if line.strip() and not line.startswith("WARNING")]
    return lines[-1][:300] if lines else ""


def _items(data: Any) -> list[dict[str, Any]]:
    """Every dict in a JSON value (list outputs and {"installed": [...]} wrappers alike)."""

    found: list[dict[str, Any]] = []
    if isinstance(data, dict):
        found.append(data)
        for value in data.values():
            found += _items(value)
    elif isinstance(data, list):
        for value in data:
            found += _items(value)
    return found


def _find_plugin(data: Any, host: str) -> dict[str, Any] | None:
    for item in _items(data):
        if host == "antigravity":
            if str(item.get("name", "")).lower() == ANTIGRAVITY_RELAYS:
                continue  # The companion registers agents, not the main plugin's MCP server.
            names = [str(item.get(key) or "") for key in ("name", "id", "pluginId", "source", "url", "path", "installPath")]
            if any(PLUGIN in name.lower() for name in names) and ("name" in item or "id" in item):
                return item
        elif item.get("id") == PLUGIN_ID or item.get("pluginId") == PLUGIN_ID:
            return item
    return None


def _has_marketplace(data: Any) -> bool:
    return any(item.get("name") == MARKETPLACE for item in _items(data))


# Hosts -----------------------------------------------------------------------


def detect_hosts(config: Config) -> list[str]:
    """Hosts whose CLI is installed (Claude Code also counts when only its config folder exists)."""

    found = []
    for host in HOSTS:
        if get_runtime(host).find_binary(config.runtime(host)):
            found.append(host)
        elif host == "claude" and config.runtime("claude").primary.is_dir():
            found.append(host)
    return found


def _host_env(config: Config, host: str) -> dict[str, str]:
    runtime_config = config.runtime(host)
    if host == "antigravity":
        return clean_environment()
    return get_runtime(host).account_env(runtime_config, runtime_config.primary_account())


def _plugin_state(host: str, command: list[str], env: dict[str, str]) -> dict[str, Any]:
    """What the host already has: installed (True/False/None if unknown), enabled, marketplace."""

    code, stdout, _ = run_command([*command, "plugin", "list", "--json"], env, timeout=120)
    data = _json(stdout) if code == 0 else None
    state: dict[str, Any] = {"installed": None, "enabled": None, "marketplace": None, "name": None}
    if data is not None:
        item = _find_plugin(data, host)
        state["installed"] = bool(item and item.get("installed", True))
        state["enabled"] = bool(item and item.get("enabled", True))
        state["name"] = item.get("name") if item else None
    if host in ("claude", "codex") and not state["installed"]:
        code, stdout, _ = run_command([*command, "plugin", "marketplace", "list", "--json"], env, timeout=120)
        data = _json(stdout) if code == 0 else None
        state["marketplace"] = _has_marketplace(data) if data is not None else None
    return state


def _plugin_steps(host: str, state: dict[str, Any]) -> list[list[str]]:
    if state["installed"] and state["enabled"]:
        return []
    if host == "claude":
        if state["installed"]:
            return [["plugin", "enable", PLUGIN_ID]]
        steps = [] if state["marketplace"] else [["plugin", "marketplace", "add", plugin_source()]]
        return steps + [["plugin", "install", PLUGIN_ID]]
    if host == "codex":
        # `codex plugin add` also re-enables a disabled plugin.
        steps = [] if state["marketplace"] or state["installed"] else [["plugin", "marketplace", "add", plugin_source()]]
        return steps + [["plugin", "add", PLUGIN_ID]]
    if state["installed"]:
        return [["plugin", "enable", str(state["name"] or PLUGIN)]]
    return [["plugin", "install", _agy_source()]]


def _install_codex_agents(primary: Path, report: Report) -> None:
    """Codex discovers personal TOML agents, but cannot register them from a plugin."""

    templates = files("agents_router").joinpath("templates", "codex-agents")
    for runtime in HOSTS:
        name = f"agt-{runtime}.toml"
        target = primary / "agents" / name
        try:
            content = templates.joinpath(name).read_bytes()
            if target.is_symlink():
                report.failed.append(f"Codex: won't replace the relay agent symlink at {target}.")
                continue
            previous = target.read_bytes() if target.exists() else None
            if previous == content:
                report.unchanged.append(f"Codex: relay agent {target.stem} is already up to date.")
                continue
            if report.dry_run:
                report.done.append(f"Codex: would install relay agent {target.stem} at {target}")
            else:
                backup = None
                # agt's own earlier version needs no backup; anything else might hold the user's edits.
                ours = previous is not None and previous.startswith(content.split(b"\n", 1)[0])
                if previous is not None and not ours:
                    # The backup must not end in .toml: Codex discovers those recursively.
                    backup = target.with_name(f"{name}.agents-router-backup-{time.time_ns()}")
                    copy_exclusive(target, backup)
                write_private(target, content)
                note = f"; backup at {backup}" if backup else ""
                report.done.append(f"Codex: installed relay agent {target.stem} at {target}{note}")
            report.add_todo(RELOAD["codex"])
        except OSError as exc:
            report.failed.append(f"Codex: couldn't install relay agent {target.stem}: {exc}")


def _install_antigravity_agents(config: Config, report: Report) -> None:
    """Use Antigravity's manual global plugin discovery, separate from Claude's agents."""

    from agents_router.runtimes.antigravity import gemini_root

    root = gemini_root(config.runtime("antigravity"))
    plugin = root / "config" / "plugins" / ANTIGRAVITY_RELAYS
    templates = files("agents_router").joinpath("templates", "antigravity-plugin")
    # Publish the manifest last so a new plugin never registers an incomplete set.
    names = [f"agents/agt-{runtime}.md" for runtime in HOSTS] + ["plugin.json"]
    try:
        for folder in (root / "config", plugin.parent, plugin, plugin / "agents"):
            if is_link(folder):
                report.failed.append(f"Antigravity: won't install relay agents through the folder link at {folder}.")
                return
        for name in names:
            target = plugin / name
            content = templates.joinpath(*name.split("/")).read_bytes()
            if is_link(target):
                report.failed.append(f"Antigravity: won't replace the relay agent link at {target}.")
                return
            previous = target.read_bytes() if target.exists() else None
            label = f"relay agent {target.stem}" if name.endswith(".md") else "relay plugin manifest"
            if previous == content:
                report.unchanged.append(f"Antigravity: {label} is already up to date.")
                continue
            if report.dry_run:
                report.done.append(f"Antigravity: would install {label} at {target}")
            else:
                backup = None
                # Markdown starts with ---, so include its identifying YAML comment.
                lines = 2 if name.endswith(".md") else 1
                ours = previous is not None and previous.splitlines()[:lines] == content.splitlines()[:lines]
                if previous is not None and not ours:
                    # Backups must not be Markdown files: the plugin scans those for agents.
                    backup = target.with_name(f"{target.name}.agents-router-backup-{time.time_ns()}")
                    copy_exclusive(target, backup)
                write_private(target, content)
                note = f"; backup at {backup}" if backup else ""
                report.done.append(f"Antigravity: installed {label} at {target}{note}")
            report.add_todo(RELOAD["antigravity"])
    except OSError as exc:
        report.failed.append(f"Antigravity: couldn't install relay agents at {plugin}: {exc}")


# Claude Code profiles ----------------------------------------------------------


def profile_name(account: Account) -> str:
    """The name a profile goes by: its account name, or its folder's label (`claude-ntb`) for the primary."""

    from agents_router import profiles

    return profiles.label(account.home) if account.name == "primary" else account.name


def this_profile(config: Config) -> Account:
    """The Claude Code folder of the session running agt, or the primary folder when no session does."""

    from agents_router.profiles import same_folder

    runtime_config = config.runtime("claude")
    if runtime_config.session_home is None:
        return runtime_config.primary_account()
    found = next((account for account in runtime_config.all_accounts() if runtime_config.is_session(account)), None)
    if found is not None:
        return found
    primary = runtime_config.primary_account()
    if same_folder(primary.home, runtime_config.session_home):
        return primary
    return Account(name="primary", home=runtime_config.session_home)


def claude_profiles(config: Config, *, unknown: bool = False, every: bool = False) -> list[Account]:
    """Every Claude Code folder of yours that gets the plugin, this one first.

    This session's folder (or the primary), the primary, your registered profiles with their own
    setup, then the profiles agt finds that are logged in. `unknown`: also found profiles whose
    login couldn't be checked (no `claude` command); `every` (update, doctor): found profiles
    whatever their login, and any `~/.claude*` folder that has the plugin. Folders with one login
    stay apart: each folder needs its own install. `[claude] ignore` leaves a profile out (never
    this session's own). Accounts agt made under `~/.agents-router/homes` aren't here; they get the
    plugin from the primary's setup.
    """

    from agents_router import profiles
    from agents_router.profiles import same_folder

    runtime_config = config.runtime("claude")
    this = this_profile(config)
    candidates = [runtime_config.primary_account()]
    candidates += [account for account in runtime_config.accounts if account.setup == "own"]
    candidates += [account for account in runtime_config.found_accounts()
                   if every or account.logged_in is True or (unknown and account.logged_in is None)]
    if every:  # a folder that has the plugin but no login on record still gets updates and checks
        candidates += [Account(name=profiles.label(folder), home=folder, setup="own", found=True)
                       for folder in profiles.candidate_folders() if has_plugin(folder)]
    chosen: list[Account] = [this]  # the session's own folder, even if `ignore` names it
    for account in candidates:
        if _ignored(runtime_config, account) or _under(account.home, homes_dir()):
            continue
        if not any(same_folder(account.home, other.home) for other in chosen):
            chosen.append(account)
    return chosen


def _ignored(runtime_config: Any, account: Account) -> bool:
    """Whether `[claude] ignore` names this profile, by name, folder label or folder."""

    from agents_router import profiles

    for item in runtime_config.ignore or []:
        if "/" in item or "\\" in item or item.startswith("~"):
            if profiles.same_folder(account.home, Path(item).expanduser()):
                return True
        elif item in (account.name, profiles.label(account.home)):
            return True
    return False


def pick_profiles(config: Config, selector: str | None, *, unknown: bool = False) -> list[Account]:
    """The profiles `--claude-profiles` names: `all` (the default), `this`, or names (`claude-ntb`, `ntb`)."""

    from agents_router.profiles import same_folder

    selector = (selector or "all").strip()
    if selector == "all":
        return claude_profiles(config, unknown=unknown)
    if selector == "this":
        return [this_profile(config)]
    every = claude_profiles(config, every=True)
    chosen: list[Account] = []
    for name in (part.strip() for part in selector.split(",")):
        if not name:
            continue
        if name == "this":
            account = this_profile(config)
        else:
            account = next((item for item in every if profile_name(item) == name), None) or next(
                (item for item in every if profile_name(item) in (f"claude-{name}", f"claude{name}")), None)
        if account is None:
            known = ", ".join(profile_name(item) for item in every)
            raise ValueError(f"--claude-profiles: no Claude profile named '{name}' here. Known: {known} (or all, this).")
        if not any(same_folder(account.home, other.home) for other in chosen):
            chosen.append(account)
    return chosen


def _reload_text(account: Account, this: bool) -> str:
    if this:
        return RELOAD["claude"]
    return (f"Start a new Claude Code session in {account.home} (or type /reload-plugins in one that's open) "
            "so the agents-router plugin loads there.")


def _install_claude(config: Config, report: Report, route: str, selector: str | None) -> None:
    """The plugin in each picked Claude Code profile, each with its own environment."""

    from agents_router.profiles import same_folder

    runtime_config = config.runtime("claude")
    command = get_runtime("claude").find_binary(runtime_config)
    declare = route == "settings" or (command is None and route == "auto")
    this = this_profile(config)
    session = runtime_config.session_home is not None  # a Claude Code session runs agt (else a terminal or another CLI)
    targets = pick_profiles(config, selector, unknown=declare)
    for account in claude_profiles(config, unknown=declare) + targets:
        if not any(same_folder(account.home, Path(item["folder"])) for item in report.claude):
            report.claude.append({"name": profile_name(account), "folder": str(account.home),
                                  "this": session and same_folder(account.home, this.home),
                                  "selected": any(same_folder(account.home, other.home) for other in targets)})
    several = len(targets) > 1
    for account in targets:
        is_this = same_folder(account.home, this.home)
        label = f"{LABELS['claude']} ({profile_name(account)})" if several else LABELS["claude"]
        if declare:
            _declare_in_settings(account.home, report, why=None if command else "the claude command wasn't found",
                                 label=label, reload=None if is_this else account.home)
            continue
        _install_claude_profile(config, account, report, route, command, label, is_this)


def _install_claude_profile(config: Config, account: Account, report: Report, route: str, command: list[str],
                            label: str, this: bool) -> None:
    env = get_runtime("claude").account_env(config.runtime("claude"), account)
    state = _plugin_state("claude", command, env)
    steps = _plugin_steps("claude", state)
    if not steps:
        report.unchanged.append(f"{label}: plugin {PLUGIN_ID} is already installed and enabled.")
        return
    for args in steps:
        shown = " ".join(["claude", *args])
        if report.dry_run:
            report.done.append(f"{label}: would run `{shown}`")
            continue
        code, stdout, stderr = _run_shown(label, [*command, *args], env, shown)
        if code != 0:
            detail = _last_line(stdout, stderr)
            if this and route == "auto":
                print(f"  failed (exit {code}): {detail}. Falling back to settings.json.", flush=True)
                _declare_in_settings(account.home, report, why=f"`{shown}` failed: {detail}", label=label)
                return
            report.failed.append(f"{label}: `{shown}` failed (exit {code}): {detail}")
            if this:
                report.add_todo(f"{LABELS['claude']}: install the plugin by hand, or register only the MCP server with "
                                f"`{FALLBACK['claude']}`.")
            else:
                # Another profile's settings.json isn't touched: its own policy (a work profile's managed
                # settings) may be what refused the plugin.
                report.add_todo(f"{LABELS['claude']}: the plugin isn't in {account.home} yet. Start Claude Code there and "
                                f"run `/plugin marketplace add {plugin_source()}`, then `/plugin install {PLUGIN_ID}`; "
                                "a work profile's managed settings may block plugins from outside.")
            return
        report.done.append(f"{label}: ran `{shown}`")
    if not report.dry_run and _plugin_state("claude", command, env)["installed"] is False:
        report.failed.append(f"{label}: the commands succeeded but `claude plugin list` doesn't show {PLUGIN}.")
        return
    report.add_todo(_reload_text(account, this))


def install_plugin(config: Config, host: str, report: Report, claude_route: str = "auto",
                   claude_profiles: str | None = "all") -> None:
    if host == "claude":
        _install_claude(config, report, claude_route, claude_profiles)
        return
    label = LABELS[host]
    runtime_config = config.runtime(host)
    command = get_runtime(host).find_binary(runtime_config)
    if command is None:
        report.failed.append(f"{label}: the {SHORT[host]} command wasn't found, so the plugin wasn't installed.")
        return
    env = _host_env(config, host)
    state = _plugin_state(host, command, env)
    steps = _plugin_steps(host, state)
    if not steps:
        report.unchanged.append(f"{label}: plugin {PLUGIN_ID} is already installed and enabled.")
        if host == "codex":
            _install_codex_agents(runtime_config.primary, report)
        elif host == "antigravity":
            _install_antigravity_agents(config, report)
        return
    for args in steps:
        shown = " ".join([SHORT[host], *args])
        if report.dry_run:
            report.done.append(f"{label}: would run `{shown}`")
            continue
        code, stdout, stderr = _run_shown(label, [*command, *args], env, shown)
        if code != 0:
            detail = _last_line(stdout, stderr)
            report.failed.append(f"{label}: `{shown}` failed (exit {code}): {detail}")
            report.add_todo(f"{label}: install the plugin by hand, or register only the MCP server with `{FALLBACK[host]}`.")
            return
        report.done.append(f"{label}: ran `{shown}`")
    if report.dry_run:
        if host == "codex":
            _install_codex_agents(runtime_config.primary, report)
        elif host == "antigravity":
            _install_antigravity_agents(config, report)
        report.add_todo(RELOAD[host])
        return
    after = _plugin_state(host, command, env)
    if after["installed"] is False:
        report.failed.append(f"{label}: the commands succeeded but `{SHORT[host]} plugin list` doesn't show {PLUGIN}.")
        return
    if host == "codex":
        _install_codex_agents(runtime_config.primary, report)
    elif host == "antigravity":
        _install_antigravity_agents(config, report)
    report.add_todo(RELOAD[host])


def _declare_in_settings(primary: Path, report: Report, why: str | None, label: str = LABELS["claude"],
                         reload: Path | None = None) -> None:
    """Declare the marketplace and enable the plugin in one Claude Code profile's user settings.

    Claude Code installs a plugin that is enabled in settings from a known marketplace
    on its next start. Every other key is kept; the old file is backed up first.
    `reload`: the folder of a profile other than this one, named in what's left for you.
    """

    path = primary / "settings.json"
    where = f" in {reload}" if reload else ""
    target = path.resolve() if path.is_symlink() else path  # keep a dotfiles symlink in place
    data: Any = {}
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            report.failed.append(f"{label}: can't read {path} as JSON ({exc}); it was left unchanged.")
            report.add_todo(f"{label}: install the plugin with `claude plugin install {PLUGIN} --marketplace {REPO}` or `/plugin install {PLUGIN_ID}`.")
            return
        if not isinstance(data, dict):
            report.failed.append(f"{label}: {path} isn't a JSON object; it was left unchanged.")
            return
    markets = data.get("extraKnownMarketplaces")
    plugins = data.get("enabledPlugins")
    if (markets is not None and not isinstance(markets, dict)) or (plugins is not None and not isinstance(plugins, dict)):
        report.failed.append(f"{label}: unexpected extraKnownMarketplaces or enabledPlugins in {path}; it was left unchanged.")
        return
    has_market = isinstance(markets, dict) and MARKETPLACE in markets
    enabled = isinstance(plugins, dict) and plugins.get(PLUGIN_ID) is True
    reason = f" ({why})" if why else ""
    if has_market and enabled:
        report.unchanged.append(f"{label}: {path} already declares the marketplace and enables {PLUGIN_ID}.")
        if why:
            report.add_todo(f"Restart Claude Code{where} if it is running, so it installs the {PLUGIN} plugin declared in its settings.")
        return
    if report.dry_run:
        report.done.append(f"{label}: would add the {MARKETPLACE} marketplace and enable {PLUGIN_ID} in {path}{reason}")
        report.add_todo(f"Restart Claude Code{where} so it installs the agents-router plugin from its settings.")
        return
    local = _local_source()
    source = {"source": "directory", "path": str(local)} if local else {"source": "github", "repo": plugin_source()}
    merged = dict(data)
    merged["extraKnownMarketplaces"] = dict(markets or {})
    merged["extraKnownMarketplaces"].setdefault(MARKETPLACE, {"source": source})
    merged["enabledPlugins"] = dict(plugins or {})
    merged["enabledPlugins"][PLUGIN_ID] = True
    backup = None
    if target.exists():
        backup = target.with_name(f"{target.name}.agents-router-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, backup)
    write_json(target, merged)
    note = f"; backup at {backup}" if backup else ""
    report.done.append(f"{label}: added the {MARKETPLACE} marketplace and enabled {PLUGIN_ID} in {path}{reason}{note}")
    report.add_todo(f"Restart Claude Code{where} so it installs the agents-router plugin from its settings (a restart, not /reload-plugins).")


# Accounts --------------------------------------------------------------------


def register_account(config: Config, runtime: str, name: str, home: Path) -> tuple[str, Account]:
    """Add an account with its own folder to the config (not saved). Returns ("added" | "exists", account).

    Raises ValueError when it can't be added.
    """

    if not NAME_RE.match(name) or name == "primary":
        raise ValueError("Give the account a short name: letters, digits, dot, dash or underscore ('primary' is reserved).")
    if runtime == "kiro":
        raise ValueError("Kiro keeps one login on this machine (outside ~/.kiro), so agt can't keep a second one apart. "
                         "Kiro jobs run on your login; see docs/kiro.md.")
    runtime_config = config.runtime(runtime)
    # An existing Claude Code profile (any folder outside agt's own) keeps its own setup: agt links nothing into it.
    own = runtime == "claude" and not _under(home, homes_dir()) and _profile(home, runtime_config.primary)
    if same_path(home, runtime_config.primary.expanduser().resolve()):
        raise ValueError(f"{home} is the primary {runtime} setup. Extra accounts need their own folder.")
    if runtime == "antigravity":
        # An Antigravity account folder stands in for a home folder (it gets its own .gemini).
        primary_root = runtime_config.primary.expanduser().resolve()
        if any(same_path(home, folder) for folder in (Path.home(), primary_root.parent, primary_root.parent.parent)):
            raise ValueError(f"{home} is your own home or .gemini folder. An extra Antigravity account needs its own folder.")
    existing = runtime_config.find(name)
    if existing is not None:
        if same_path(existing.home, home):
            return "exists", existing
        raise ValueError(f"{runtime} account '{name}' already exists with folder {existing.home}.")
    for account in runtime_config.accounts:
        if same_path(account.home, home):
            return "exists", account
    account = Account(name=name, home=home, setup="own" if own else "linked")
    runtime_config.accounts.append(account)
    runtime_config._found = None  # the profile is registered now, not found
    return "added", account


def _profile(home: Path, primary: Path) -> bool:
    """Whether a folder is a Claude Code profile you already use (logged in before, the default, or your primary)."""

    from agents_router import profiles

    return (profiles.is_default(home) or profiles.same_folder(home, primary)
            or bool(profiles.account_info(home)["login"]))


def _under(path: Path, folder: Path) -> bool:
    try:
        Path(path).expanduser().resolve().relative_to(Path(folder).expanduser().resolve())
        return True
    except (ValueError, OSError):
        return False


def guess_runtime(folder: Path) -> str:
    """claude, codex or antigravity, from marker files in the folder, then from its name."""

    if (folder / ".gemini" / "antigravity-cli").is_dir():
        return "antigravity"
    scores = {runtime: sum((folder / marker).exists() for marker in markers) for runtime, markers in MARKERS.items()}
    if scores["codex"] > scores["claude"]:
        return "codex"
    if scores["claude"] > scores["codex"]:
        return "claude"
    return "codex" if "codex" in folder.name.lower() else "claude"


def parse_account(value: str) -> tuple[str | None, str, Path]:
    """`[runtime:]name=folder` -> (runtime or None, name, folder)."""

    left, sep, folder = value.partition("=")
    if not sep or not left or not folder:
        raise ValueError(f"--account takes NAME=FOLDER (or codex:NAME=FOLDER), got '{value}'")
    runtime, colon, name = left.rpartition(":")
    if colon and runtime not in HOSTS:
        raise ValueError(f"unknown runtime '{runtime}' in --account '{value}' (use claude, codex or antigravity)")
    return (runtime or None), name, Path(os.path.expandvars(folder)).expanduser().resolve()


def _probe_spec(runtime: str) -> JobSpec:
    return JobSpec(job_id="probe", runtime=runtime, prompt="", cwd=str(Path.cwd().resolve()))


def setup_accounts(config: Config, values: list[str], report: Report) -> None:
    changed = False
    to_check: list[tuple[str, Account]] = []
    for value in values:
        try:
            runtime, name, folder = parse_account(value)
        except ValueError as exc:
            report.failed.append(str(exc))
            continue
        runtime = runtime or guess_runtime(folder)
        if not folder.is_dir():
            report.failed.append(
                f"Account '{name}': folder {folder} doesn't exist. To make a new account, run `agt accounts add {name}`"
                + ("" if runtime == "claude" else f" --runtime {runtime}") + " and log in."
            )
            continue
        try:
            status, account = register_account(config, runtime, name, folder)
        except ValueError as exc:
            report.failed.append(f"Account '{name}': {exc}")
            continue
        alias = f" (already registered as '{account.name}')" if account.name != name else ""
        if report.dry_run:
            verb = "would register and sync" if status == "added" else "already registered; would sync"
            report.done.append(f"{runtime} account '{name}' ({folder}): {verb}{alias}")
            continue
        changed = changed or status == "added"
        to_check.append((runtime, account))
        if status == "added":
            report.done.append(f"{runtime} account '{account.name}' ({folder}): registered")
        else:
            report.unchanged.append(f"{runtime} account '{account.name}' ({folder}): already registered{alias}")
    if changed:
        save_accounts(config)
    for runtime, account in to_check:
        runtime_config = config.runtime(runtime)
        adapter = get_runtime(runtime)
        try:
            setup = adapter.prepare(runtime_config, account, _probe_spec(runtime))
        except Exception as exc:  # a sync problem shouldn't hide the rest of the summary
            report.failed.append(f"{runtime} account '{account.name}': syncing the shared setup failed: {exc}")
            continue
        report.done.append(f"{runtime} account '{account.name}': shared setup synced")
        if getattr(adapter, "add_note", None):
            report.notes.append(f"{runtime} account '{account.name}': {adapter.add_note}")
        for message in (setup.get("conflicts") or []) + (setup.get("warnings") or []):
            report.notes.append(f"{runtime} account '{account.name}': {message}")
        if adapter.find_binary(runtime_config) is None:
            continue
        if not (adapter.auth_status(runtime_config, account) or {}).get("loggedIn"):
            flag = "" if runtime == "claude" else f" --runtime {runtime}"
            report.add_todo(f"Log in {runtime} account '{account.name}' in a browser: `agt accounts login {account.name}{flag}`.")


# Main ------------------------------------------------------------------------


def _check_primary_login(config: Config, host: str, report: Report) -> None:
    if host == "antigravity":
        return
    runtime_config = config.runtime(host)
    adapter = get_runtime(host)
    if adapter.find_binary(runtime_config) is None:
        return
    status = adapter.auth_status(runtime_config, runtime_config.primary_account()) or {}
    if not status.get("loggedIn"):
        how = "run `claude` and use /login" if host == "claude" else "run `codex login`"
        report.add_todo(f"{LABELS[host]} reports no login for {runtime_config.primary}. If you use it there, {how}.")


def _check_kiro_login(config: Config, report: Report) -> None:
    """Kiro isn't a host, but agt runs Kiro jobs. When you use Kiro (its `~/.kiro` folder is there) and its CLI is
    installed, a missing login is one more thing for you to do. `kiro-cli whoami` only reads; agt never logs in."""

    runtime_config = config.runtime("kiro")
    if not runtime_config.primary.is_dir():
        return
    adapter = get_runtime("kiro")
    if adapter.find_binary(runtime_config) is None:
        return
    if not (adapter.auth_status(runtime_config, runtime_config.primary_account()) or {}).get("loggedIn"):
        report.add_todo("Kiro CLI reports no login. If you want agt to run Kiro jobs, run `kiro-cli login`; if a job "
                        "still fails with a login error, set `KIRO_API_KEY` in the environment agt runs in.")


def _check_agt_on_path(report: Report) -> None:
    if shutil.which("agt"):
        return
    scripts = sysconfig.get_path("scripts")
    report.add_todo(
        "The plugin starts `agt`, but it isn't on PATH. Run `uv tool update-shell` (or `pipx ensurepath`), or add "
        f"{scripts} to PATH, then restart your terminal and agent."
    )


def upstream_installs(config: Config) -> list[str]:
    """Signs of upstream CLI Subagents (`csa`), which would register a second MCP server and hooks.

    Only looks; agt never reads or changes the old install.
    """

    found = []
    command = shutil.which("csa")
    if command:
        found.append(f"upstream CLI Subagents' `csa` is on PATH ({command})")
    old_home = Path.home() / ".cli-subagents"
    if old_home.is_dir():
        found.append(f"upstream CLI Subagents' folder {old_home} exists")
    for account in config.runtime("claude").listed_accounts():
        path = Path(account.home) / "settings.json"
        try:
            plugins = json.loads(path.read_text(encoding="utf-8")).get("enabledPlugins")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(plugins, dict) and any(str(key).startswith("cli-subagents@") and value for key, value in plugins.items()):
            found.append(f"upstream CLI Subagents' plugin is enabled in {path}")
    return found


def run_setup(config: Config, hosts: list[str], accounts: list[str], dry_run: bool = False,
              claude_route: str = "auto", claude_profiles: str | None = "all") -> tuple[int, Report]:
    report = Report(dry_run=dry_run)
    if not hosts or "auto" in hosts:
        detected = detect_hosts(config)
        hosts = [host for host in HOSTS if host in detected or host in hosts]
        if not hosts:
            report.failed.append(
                "No Claude Code, Codex or Antigravity install was found. Install one, or register the MCP server "
                "in your client by hand: command `agt`, argument `mcp`."
            )
    # Accounts first: a Claude profile named with --account gets the plugin in this same run.
    setup_accounts(config, accounts, report)
    for host in hosts:
        install_plugin(config, host, report, claude_route, claude_profiles)
        _check_primary_login(config, host, report)
    _check_kiro_login(config, report)
    _check_agt_on_path(report)
    if "antigravity" in hosts:
        report.add_todo("If you haven't signed in to Antigravity yet, run `agy` once and sign in.")
    return (1 if report.failed else 0), report


REPORT_LABELS = ("Claude Code", "Codex", "Antigravity")
RAN = re.compile(r"^(?:would run|ran) `(.+)`$")
RELAY = re.compile(r"^(would install|installed) relay agent (\S+) at (\S+)(.*)$")


def report_rows(items: list[str], mark: ui.Span, ran_as: str | None = None) -> list[list[ui.Line]]:
    """One row per CLI: its plugin commands under its name, its relay agents on one line. `ran_as`: the commands a
    CLI ran (shown as they ran, above the report) become this one row."""

    rows: list[list[ui.Line]] = []
    said: set[str] = set()
    relays: dict[tuple[str, str, str], list[str]] = {}
    last = None
    for item in items:
        label, sep, rest = item.partition(": ")
        if not sep or label.split(" (", 1)[0] not in REPORT_LABELS:  # "Claude Code (claude-ntb)": one profile
            label, rest = "", item
        relay = RELAY.match(rest)
        if relay:
            key = (label, relay.group(1), ui.short_path(Path(relay.group(3)).parent))
            if key not in relays:
                relays[key] = []
                rows.append([mark, ui.muted(label), key])  # filled in below, once every agent is known
            relays[key].append(relay.group(2) + relay.group(4))
            continue
        ran = RAN.match(rest)
        if ran and ran_as and rest.startswith("ran "):
            if label in said:
                continue
            said.add(label)
            ran = None
            rest = ran_as
        text: ui.Line = ui.muted(ran.group(1)) if ran else ui.inline(ui.tilde(rest))
        if not label:  # a line that isn't about one CLI takes the label's place
            rows.append([mark, text])
        else:
            rows.append(["", "", text] if label == last else [mark, ui.muted(label), text])
        last = label
    for row in rows:
        if len(row) == 3 and isinstance(row[2], tuple) and not isinstance(row[2], ui.Span):  # a relay group's key
            label, verb, folder = row[2]
            names = relays[row[2]]
            row[2] = f"{'relay agents' if len(names) > 1 else 'relay agent'} {', '.join(names)} in {folder}"
    return rows


def still_needed(todo: list[str], heading: str = "Still needed from you") -> list[ui.Line]:
    """What's left for you (logins and restarts), numbered, with home paths written as ~."""

    if not todo:
        return []
    return ["", ui.bold(heading), *(["  ", f"{n}. ", *ui.inline(ui.tilde(item))] for n, item in enumerate(todo, 1))]


def report_lines(report: Report, name: str = "setup", *, todo: bool = True, footer: bool = True) -> list[ui.Line]:
    if report.dry_run:
        outcome: ui.Line = ui.muted("dry run, nothing changed")
    elif report.failed:
        outcome = ui.error(f"{len(report.failed)} failed")
    else:
        outcome = ui.ok("✓ done")
    lines: list[ui.Line] = [[ui.bold(f"agt {name}"), "  ", outcome]]
    ran_as = "installed the agents-router plugin" if name == "setup" and not report.dry_run else None
    rows = report_rows(report.done, ui.ok("+"), ran_as) + report_rows(report.unchanged, ui.muted("·")) \
        + report_rows(report.failed, ui.Span("error-mark", "✗"))
    if rows:
        lines += ["", *ui.columns(rows, gap=2)]
    if report.checked:
        lines += ["", *ui.columns([[ui.muted(what), ui.inline(how)] for what, how in report.checked], gap=3)]
    if report.notes:
        lines += ["", ui.bold("Notes"), *(["  ", *ui.inline(ui.tilde(note))] for note in report.notes)]
    if todo:
        lines += still_needed(report.todo)
    if footer:
        lines += ["", [ui.muted("Check everything with "), ui.code("agt doctor"), ui.muted(".")]]
        if name == "setup":
            lines.append([ui.muted("Optional: delegation rules for your agent. See "), ui.code("agt rules"),
                          ui.muted("; install with "), ui.code("agt rules --install"), ui.muted(".")])
    return lines


def format_report(report: Report, name: str = "setup") -> str:
    return "\n".join(ui.plain(line) for line in report_lines(report, name))


# Update ----------------------------------------------------------------------

PACKAGE_SOURCE = f"git+{REPO_URL}"
MANUAL_PACKAGE = (
    f"`pipx install --force {PACKAGE_SOURCE}`, `uv tool install --force {PACKAGE_SOURCE}` "
    f"or `python -m pip install --user --force-reinstall {PACKAGE_SOURCE}`, whichever installed it"
)


def recorded_source(direct_url: str | None) -> str | None:
    """Where agt was installed from, as pip, pipx and uv take it: `git+<url>`, a folder, or an archive URL.

    It comes from the install's `direct_url.json`; a git install is taken again from its branch, not the
    commit it was at. None when the install didn't record one (a package index).
    """

    try:
        info = json.loads(direct_url) if direct_url else {}
    except ValueError:
        return None
    url = info.get("url") if isinstance(info, dict) else None
    if not isinstance(url, str) or not url:
        return None
    vcs = info.get("vcs_info")
    if isinstance(vcs, dict):
        branch = vcs.get("requested_revision")
        return f"{vcs.get('vcs') or 'git'}+{url}" + (f"@{branch}" if branch and branch != vcs.get("commit_id") else "")
    if url.startswith("file://") and isinstance(info.get("dir_info"), dict):
        from urllib.request import url2pathname

        return url2pathname(url[len("file://"):])
    return url


def detect_install(prefix: str, location: str | None, direct_url: str | None, user_site: str | None,
                   executable: str) -> tuple[str, list[str] | None]:
    """How this package was installed, and the command that reinstalls it from where it came from.

    `prefix` is sys.prefix, `location` the folder that holds the installed package
    (None when it isn't installed, e.g. run from a checkout via PYTHONPATH). The
    source is the one the install recorded (a git URL, a folder or an archive),
    else this repository on GitHub.
    """

    if location is None:
        return "unknown", None
    try:
        info = json.loads(direct_url) if direct_url else {}
    except ValueError:
        info = {}
    if isinstance(info, dict) and (info.get("dir_info") or {}).get("editable"):
        return "editable", None
    source = recorded_source(direct_url) or PACKAGE_SOURCE
    parts = [part.lower() for part in Path(prefix).parts]
    if "venvs" in parts and any("pipx" in part for part in parts):
        return "pipx", [shutil.which("pipx") or "pipx", "install", "--force", source]
    if "uv" in parts and "tools" in parts:
        return "uv", [shutil.which("uv") or "uv", "tool", "install", "--force", "--compile-bytecode", source]
    if user_site and Path(location).resolve().is_relative_to(Path(user_site).resolve()):
        return "pip-user", [executable, "-m", "pip", "install", "--user", "--force-reinstall", "--no-deps", source]
    return "pip", [executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", source]


def install_source() -> str:
    """The reinstall source of the running agt, for messages: what `agt update` would install from."""

    import importlib.metadata

    try:
        return recorded_source(importlib.metadata.distribution("agents-router").read_text("direct_url.json")) or PACKAGE_SOURCE
    except importlib.metadata.PackageNotFoundError:
        return PACKAGE_SOURCE


def _install_info() -> tuple[str, list[str] | None]:
    import importlib.metadata
    import site
    import sys

    running = Path(__file__).resolve().parent
    if (running.parent.parent / "pyproject.toml").is_file() and (running.parent.parent / ".claude-plugin").is_dir():
        return "checkout", None  # src/agents_router in a clone of the repository
    try:
        dist = importlib.metadata.distribution("agents-router")
        location = str(dist.locate_file(""))
        direct_url = dist.read_text("direct_url.json")
        if not same_path(Path(dist.locate_file("agents_router")).resolve(), running):
            return "unknown", None  # the installed copy isn't the one running
    except importlib.metadata.PackageNotFoundError:
        location = direct_url = None
    try:
        user_site = site.getusersitepackages()
    except Exception:
        user_site = None
    return detect_install(sys.prefix, location, direct_url, user_site, sys.executable)


def _replaces_running_program(method: str) -> bool:
    """Windows can't replace a running .exe, so agt can't reinstall itself from agt.exe or a tool venv."""

    import sys

    if os.name != "nt":
        return False
    return method in ("pipx", "uv") or Path(sys.argv[0]).stem.lower() == "agt"


def update_package(report: Report) -> None:
    method, argv = _install_info()
    if method in ("editable", "checkout"):
        report.unchanged.append("agt: runs from a checkout of the repository; update it with `git pull` there.")
        return
    if argv is None:
        report.failed.append(f"agt: couldn't tell how the package was installed. Reinstall it with {MANUAL_PACKAGE}.")
        return
    shown = " ".join([Path(argv[0]).stem, *argv[1:]])
    if report.dry_run:
        report.done.append(f"agt ({method} install): would run `{shown}`")
        return
    if _replaces_running_program(method):
        report.add_todo(f"Update the agt package now with `{shown}` (Windows can't replace agt while it's running).")
        return
    code, stdout, stderr = _run_shown("updating agt", argv, None, shown, timeout=600)
    if code != 0:
        report.failed.append(f"agt: `{shown}` failed (exit {code}): {_last_line(stdout, stderr)}")
        return
    report.done.append(f"agt: reinstalled with `{shown}`")  # from its install source, which isn't always GitHub


def _update_steps(host: str, state: dict[str, Any]) -> list[list[str]]:
    if host == "claude":
        return [["plugin", "marketplace", "update", MARKETPLACE], ["plugin", "update", PLUGIN_ID]]
    if host == "codex":
        # A local marketplace (a clone added by path) is read in place: there's nothing to fetch, and
        # `codex plugin marketplace upgrade` refuses it ("not configured as a Git marketplace").
        fetch = [] if state.get("local") else [["plugin", "marketplace", "upgrade", MARKETPLACE]]
        return fetch + [["plugin", "add", PLUGIN_ID]]
    # agy has no update command; installing from the URL again replaces the plugin.
    return [["plugin", "install", _agy_source()]]


def _settings_declare_plugin(primary: Path) -> bool:
    try:
        data = json.loads((primary / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("enabledPlugins"), dict) and PLUGIN_ID in data["enabledPlugins"]


def installed_version(folder: Path) -> str | None:
    """The agents-router plugin version Claude Code installed in a profile, from `plugins/installed_plugins.json`."""

    try:
        data = json.loads((Path(folder) / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = (data.get("plugins") or {}).get(PLUGIN_ID) if isinstance(data, dict) else None
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return None
    entries = [entry for entry in entries if isinstance(entry, dict)]
    entry = next((entry for entry in entries if entry.get("scope") == "user"), entries[0] if entries else None)
    version = entry.get("version") if entry else None
    return str(version) if version else None


def has_plugin(folder: Path) -> bool:
    """Whether a Claude Code profile has the plugin, by its files only (no `claude` call)."""

    return installed_version(folder) is not None or _settings_declare_plugin(folder)


def _update_claude(config: Config, report: Report) -> bool:
    """Update the plugin in every Claude Code profile that has it, logged in or not. True when one has it."""

    from agents_router.profiles import same_folder

    runtime_config = config.runtime("claude")
    command = get_runtime("claude").find_binary(runtime_config)
    this = this_profile(config)
    having: list[tuple[Account, dict[str, str], str]] = []  # (profile, its environment, how it has the plugin)
    for account in claude_profiles(config, every=True):
        env = get_runtime("claude").account_env(runtime_config, account)
        if command is not None and _plugin_state("claude", command, env)["installed"]:
            having.append((account, env, "installed"))
        elif _settings_declare_plugin(account.home):
            having.append((account, env, "declared" if command is not None else "settings"))
    several = len(having) > 1
    for account, env, how in having:
        is_this = same_folder(account.home, this.home)
        label = f"{LABELS['claude']} ({profile_name(account)})" if several else LABELS["claude"]
        where = "" if is_this else f" in {account.home}"
        if how == "settings":
            report.unchanged.append(f"{label}: the plugin is declared in {account.home / 'settings.json'}; a restart refreshes it.")
            report.add_todo(f"Restart Claude Code{where} so it refreshes the agents-router plugin.")
            continue
        if how == "declared":
            report.unchanged.append(f"{label}: the plugin is declared in settings.json but not installed yet; a restart installs it.")
            report.add_todo(f"Restart Claude Code{where} so it installs the agents-router plugin.")
            continue
        for args in _update_steps("claude", {}):
            shown = " ".join(["claude", *args])
            if report.dry_run:
                report.done.append(f"{label}: would run `{shown}`")
                continue
            code, stdout, stderr = _run_shown(label, [*command, *args], env, shown)
            if code != 0:
                report.failed.append(f"{label}: `{shown}` failed (exit {code}): {_last_line(stdout, stderr)}")
                break
            report.done.append(f"{label}: ran `{shown}`")
        else:
            report.add_todo(_reload_text(account, is_this))
    return bool(having)


def _codex_local_marketplace(command: list[str], env: dict[str, str]) -> bool:
    """Whether Codex's agents-router marketplace is a local folder rather than a Git repository."""

    code, stdout, _ = run_command([*command, "plugin", "marketplace", "list", "--json"], env, timeout=120)
    data = _json(stdout) if code == 0 else None
    for item in _items(data) if data is not None else []:
        if item.get("name") == MARKETPLACE:
            source = item.get("marketplaceSource")
            return isinstance(source, dict) and source.get("sourceType") == "local"
    return False


def update_plugin(config: Config, host: str, report: Report) -> bool:
    """Update the plugin in one host. Returns True when the host has it."""

    if host == "claude":
        return _update_claude(config, report)
    label = LABELS[host]
    runtime_config = config.runtime(host)
    command = get_runtime(host).find_binary(runtime_config)
    if command is None:
        return False
    env = _host_env(config, host)
    state = _plugin_state(host, command, env)
    if not state["installed"]:
        return False
    if host == "codex":
        state["local"] = _codex_local_marketplace(command, env)
    for args in _update_steps(host, state):
        shown = " ".join([SHORT[host], *args])
        if report.dry_run:
            report.done.append(f"{label}: would run `{shown}`")
            continue
        code, stdout, stderr = _run_shown(label, [*command, *args], env, shown)
        if code != 0:
            report.failed.append(f"{label}: `{shown}` failed (exit {code}): {_last_line(stdout, stderr)}")
            return True
        report.done.append(f"{label}: ran `{shown}`")
    if host == "codex":
        _install_codex_agents(runtime_config.primary, report)
    elif host == "antigravity":
        _install_antigravity_agents(config, report)
    report.add_todo(RELOAD[host])
    return True


def run_update(config: Config, hosts: list[str], dry_run: bool = False, package: bool = True) -> tuple[int, Report]:
    report = Report(dry_run=dry_run)
    if package:
        update_package(report)
    candidates = HOSTS if not hosts or "auto" in hosts else [host for host in HOSTS if host in hosts]
    found = [host for host in candidates if update_plugin(config, host, report)]
    if not found:
        report.checked.append(("Plugin", "no host has it yet; `agt setup` installs it"))
    return (1 if report.failed else 0), report
