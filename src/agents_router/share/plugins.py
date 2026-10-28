"""Sharing plugins between Claude Code and Codex, each installed with the CLI's own commands.

Each user-wide plugin is one item, `plugins/<name>@<marketplace>`, decided by the same 3-way engine as agents
and skills: a plugin you install in one CLI is installed in your other Claude Code and Codex folders, one you
uninstall where agt installed it stops going there, and uninstalling the one you installed yourself waits for
`agt share rm plugins/<id>`. Whether it's on or off stays each CLI's own. Off unless you turn it on (`agt share
kinds add plugins` shows what it would install, and installs only after you say yes).

Before a plugin goes to a CLI, agt checks that the CLI can add its marketplace and load its parts. Each plugin
and CLI ends one of three ways:

- it goes: the sync runs `plugin marketplace add` when the CLI doesn't know the marketplace yet, then `claude
  plugin install` or `codex plugin add`, and checks `plugin list` shows it;
- it stays put, and `agt share status` says why: the CLI can't add its marketplace, would load nothing of it,
  or the plugin comes with Codex. Kiro and Antigravity install no plugins;
- it waits for your answer, as an attention item with the commands that answer it: the CLI would load only part
  of it, would run its hooks without asking, or already has it for a project. `agt share pick plugins/<id> --in
  <label>` installs it as it is now (a later version that changes what's missing asks again); `--not-in
  <label>` keeps it out.

What each CLI loads from the other's plugins (Claude Code 2.1 and Codex 0.164, checked in a sandbox):

| Part | Claude Code | Codex |
|---|---|---|
| skills | yes | yes |
| commands | yes | as skills |
| agents, interface mods (`modules` in `hooks/hooks.json`), LSP servers, output styles, monitors, themes, channels, `userConfig` | yes | no |
| hooks | yes, without asking | asks you first (its docs) |
| MCP servers | expands `${CLAUDE_PLUGIN_ROOT}`; starts them in your session's folder | doesn't expand it; starts them in the plugin's folder; no `headersHelper` or SSE |
| apps (`.app.json`) | no | yes |

Marketplaces: Claude Code adds a folder or a Git repo that has `.claude-plugin/marketplace.json`; Codex adds a
folder or a Git repo that has that file or its own `.agents/plugins/marketplace.json`.

What agt reads:

| CLI | Plugins | Marketplaces |
|---|---|---|
| Claude Code | `plugins/installed_plugins.json` (user scope) with `enabledPlugins` in the folder's `settings.json` | `plugins/known_marketplaces.json` |
| Codex | `[plugins."<id>"]` in `config.toml`, files in `plugins/cache` | `[marketplaces.<name>]` in `config.toml` |

agt's own plugin is left to `agt setup`. A failed install is remembered and not tried again until the plugin's
version changes (`agt share pick plugins/<id> --in <label>` tries now).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any

from agents_router import fsutil, ui
from agents_router.share import engine, mcp, providers
from agents_router.share.state import State, share_dir

KIND = "plugins"
OWN = "agents-router"
NATIVE = ("claude", "codex")  # CLIs that install the same plugin packages
CLI_NAMES = {"claude": "Claude Code", "codex": "Codex", "kiro": "Kiro", "antigravity": "Antigravity"}
# Parts only one CLI loads, as a message names them.
CLAUDE_ONLY = {"agents": "agents", "modules": "interface mods", "lsp": "LSP servers", "output-styles": "output styles",
               "monitors": "monitors", "themes": "themes", "channels": "channels",
               "user-config": "install settings (userConfig)"}
CODEX_ONLY = {"apps": "apps"}
LOADS = {"claude": {"skills", "commands", "hooks", "mcp", *CLAUDE_ONLY}, "codex": {"skills", "commands", "hooks", "mcp", "apps"}}
CLAUDE_VARIABLE_RE = re.compile(r"\$\{(CLAUDE_PLUGIN_ROOT|CLAUDE_PLUGIN_DATA|user_config\.[^}]+)\}")
CODEX_VARIABLE_RE = re.compile(r"\$\{?(CODEX_[A-Z0-9_]+)")
CODEX_SOURCES = ("github", "git", "directory", "local")  # what `codex plugin marketplace add` takes
MARKETPLACE_FILES = {"claude": (".claude-plugin/marketplace.json",),
                     "codex": (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json")}


# Reading ---------------------------------------------------------------------------------


@dataclass
class Market:
    name: str
    source: str | None  # what `plugin marketplace add` takes, when agt knows it
    kind: str = ""  # its source type: github, git, directory or url (Claude Code); local or git (Codex)
    folder: Path | None = None  # its copy on this machine
    bundled: bool = False  # Codex's own (it comes with the Codex app)


@dataclass
class Plugin:
    id: str  # name@marketplace
    enabled: bool | None  # True on, False turned off on purpose, None installed but not listed as either
    version: str = ""
    root: Path | None = None  # its installed files
    market: Market | None = None

    @property
    def name(self) -> str:
        return self.id.partition("@")[0]

    @property
    def marketplace(self) -> str:
        return self.id.partition("@")[2]

    @property
    def source(self) -> str | None:
        return self.market.source if self.market else None


@dataclass
class Holding:
    """One CLI folder's plugins and marketplaces."""

    plugins: dict[str, Plugin] = field(default_factory=dict)  # user-wide, agt's own left out
    narrow: dict[str, str] = field(default_factory=dict)  # id -> scope, for one installed only for a project
    markets: dict[str, Market] = field(default_factory=dict)
    problem: str | None = None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load(path: Path) -> tuple[Any, str | None]:
    """A JSON file the CLI keeps (None when there's none), or why it can't be read: Claude Code rewrites these while
    it installs, and a half-written file must never read as "no plugins"."""

    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"{path}: can't be read ({exc})"


def _claude_market(name: str, entry: Any) -> Market:
    inner = entry.get("source") if isinstance(entry, dict) and isinstance(entry.get("source"), dict) else entry
    kind, source = "", None
    if isinstance(inner, str):
        source = inner or None
    elif isinstance(inner, dict):
        kind = str(inner.get("source") or "")
        source = next((inner[key] for key in ("repo", "url", "path") if isinstance(inner.get(key), str) and inner[key]), None)
    folder = entry.get("installLocation") if isinstance(entry, dict) else None
    return Market(name, source, kind, Path(folder) if isinstance(folder, str) and folder else None)


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _newest(folder: Path) -> Path | None:
    try:
        found = [entry for entry in folder.iterdir() if entry.is_dir()]
    except OSError:
        return None
    return max(found, key=lambda entry: entry.stat().st_mtime) if found else None


def read(provider: providers.Provider) -> Holding:
    """The folder's plugins and marketplaces (Claude Code and Codex)."""

    folder, holding = provider.folder, Holding()
    if provider.cli == "claude":
        loaded = [_load(path) for path in (folder / "settings.json", folder / "plugins" / "installed_plugins.json",
                                           folder / "plugins" / "known_marketplaces.json")]
        problem = next((problem for _, problem in loaded if problem), None)
        if problem:
            return Holding(problem=problem)
        settings, installed, markets = (data for data, _ in loaded)
        enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
        installed = installed.get("plugins") if isinstance(installed, dict) else None
        holding.markets = {name: _claude_market(name, entry) for name, entry in (markets or {}).items()} \
            if isinstance(markets, dict) else {}
        for plugin_id, entries in (installed or {}).items():
            entries = [entry for entry in entries or [] if isinstance(entry, dict)]
            users = [entry for entry in entries if entry.get("scope", "user") == "user"]
            if users:
                path = users[0].get("installPath")
                holding.plugins[plugin_id] = Plugin(plugin_id, None, str(users[0].get("version") or ""),
                                                    Path(path) if isinstance(path, str) and path else None)
            elif entries:
                holding.narrow[plugin_id] = str(entries[0].get("scope"))
        for plugin_id, on in (enabled or {}).items() if isinstance(enabled, dict) else []:
            if isinstance(on, bool) and plugin_id in holding.plugins:
                holding.plugins[plugin_id].enabled = on
    elif provider.cli == "codex":
        try:
            config = tomllib.loads((folder / "config.toml").read_text(encoding="utf-8"))
        except FileNotFoundError:
            config = {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return Holding(problem=f"{folder / 'config.toml'}: can't be read ({exc})")
        markets = config.get("marketplaces") if isinstance(config.get("marketplaces"), dict) else {}
        for name, table in markets.items():
            if not isinstance(table, dict):
                continue
            kind, source = str(table.get("source_type") or ""), table.get("source")
            source = source if isinstance(source, str) and source else None
            local = Path(source) if kind == "local" and source else folder / ".tmp" / "marketplaces" / name
            bundled = kind == "local" and source is not None and (
                _inside(local, folder / ".tmp") or "codex-runtimes" in local.parts)
            holding.markets[name] = Market(name, source, kind, local, bundled)
        plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
        for plugin_id, table in plugins.items():
            if isinstance(table, dict) and "@" in plugin_id:
                on = table.get("enabled", True)
                name, _, market = plugin_id.partition("@")
                holding.plugins[plugin_id] = Plugin(plugin_id, on if isinstance(on, bool) else None,
                                                    str(table.get("version") or ""),
                                                    _newest(folder / "plugins" / "cache" / market / name))
    for plugin in holding.plugins.values():
        plugin.market = holding.markets.get(plugin.marketplace)
        if not plugin.version and plugin.root is not None and provider.cli == "codex":
            plugin.version = plugin.root.name
    holding.plugins = {plugin_id: plugin for plugin_id, plugin in holding.plugins.items() if plugin.name != OWN}
    return holding


def inventory(provider: providers.Provider) -> dict[str, Plugin]:
    """The provider's user plugins: on, turned off, or installed (Claude Code and Codex)."""

    return read(provider).plugins


# What a plugin holds ---------------------------------------------------------------------


@dataclass
class Parts:
    found: dict[str, list[str]] = field(default_factory=dict)  # part -> names (hooks: the events Claude Code runs)
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)  # its MCP servers
    problem: str | None = None  # why agt can't tell
    codex_hooks: list[str] = field(default_factory=list)  # the events Codex runs (its manifest can say none)


def _paths(root: Path, value: Any, default: str) -> list[Path]:
    values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    return [root / default, *(root / str(item) for item in values if isinstance(item, str))]


def _names(paths: list[Path], pattern: str) -> list[str]:
    found: set[str] = set()
    for path in paths:
        if path.is_file():
            found.add(path.stem)
        elif path.is_dir():
            found |= {entry.parent.name if entry.name == "SKILL.md" else entry.stem for entry in path.glob(pattern)}
    return sorted(found)


def _servers(root: Path, value: Any) -> dict[str, dict[str, Any]]:
    data = value if isinstance(value, dict) else _read_json(root / value) if isinstance(value, str) else \
        _read_json(root / ".mcp.json")
    servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
    return {name: server for name, server in servers.items() if isinstance(server, dict)} if isinstance(servers, dict) else {}


def parts(root: Path | None) -> Parts:
    """What the plugin's files hold, from either CLI's manifest and the folders both look in."""

    if root is None or not root.is_dir():
        return Parts(problem="agt can't see its installed files")
    claude = _read_json(root / ".claude-plugin" / "plugin.json") or {}
    codex = _read_json(root / ".codex-plugin" / "plugin.json") or {}
    claude, codex = (claude if isinstance(claude, dict) else {}), (codex if isinstance(codex, dict) else {})
    found = {
        "skills": _names(_paths(root, claude.get("skills"), "skills") + _paths(root, codex.get("skills"), "skills"), "*/SKILL.md"),
        "commands": _names(_paths(root, claude.get("commands"), "commands"), "*.md"),
        "agents": _names(_paths(root, claude.get("agents"), "agents"), "*.md"),
        "output-styles": _names(_paths(root, claude.get("outputStyles"), "output-styles"), "*.md"),
    }
    events, modules = _hooks(root, claude.get("hooks") or "hooks/hooks.json")
    found["hooks"] = events
    found["modules"] = [f"{modules} module{'s' if modules != 1 else ''}"] if modules else []
    # Codex reads the portable plugin.json's `extensions.com.openai` first, then its own manifest; either one's
    # `hooks` replaces the default hooks file (`"hooks": []` means none).
    portable = _read_json(root / "plugin.json")
    openai = (portable.get("extensions") or {}).get("com.openai") if isinstance(portable, dict) and \
        isinstance(portable.get("extensions"), dict) else None
    overlay = openai if isinstance(openai, dict) else codex
    codex_hooks = _hooks(root, overlay["hooks"] if "hooks" in overlay else "hooks/hooks.json")[0]
    lsp = claude.get("lspServers") or ".lsp.json"
    lsp = lsp if isinstance(lsp, dict) else _read_json(root / lsp) if isinstance(lsp, str) else None
    found["lsp"] = sorted(lsp) if isinstance(lsp, dict) else []
    for part, key in (("monitors", "monitors"), ("themes", "themes"), ("channels", "channels"), ("user-config", "userConfig")):
        value = claude.get(key)
        found[part] = sorted(value) if isinstance(value, dict) else [str(item) for item in value] \
            if isinstance(value, list) else [key] if value else []
    found["apps"] = ["apps"] if codex.get("apps") or (root / ".app.json").exists() else []
    servers = {**_servers(root, codex.get("mcpServers")), **_servers(root, claude.get("mcpServers"))}
    found["mcp"] = sorted(servers)
    return Parts({part: names for part, names in found.items() if names}, servers, codex_hooks=codex_hooks)


def _hooks(root: Path, declared: Any) -> tuple[list[str], int]:
    """The hook events in a manifest's `hooks` (paths or inline objects), and how many mod modules they hold."""

    events: set[str] = set()
    modules = 0
    for hooks in declared if isinstance(declared, list) else [declared]:
        hooks = hooks if isinstance(hooks, dict) else _read_json(root / hooks) if isinstance(hooks, str) else None
        if not isinstance(hooks, dict):
            continue
        modules += len(hooks["modules"]) if isinstance(hooks.get("modules"), list) else 0  # a Claude Code mod's code
        inner = hooks["hooks"] if "hooks" in hooks else {} if "modules" in hooks else hooks
        events |= set(inner) if isinstance(inner, dict) else set()
    return sorted(events), modules


def _counted(found: dict[str, list[str]], labels: dict[str, str]) -> str:
    shown = []
    for part, label in labels.items():
        names = found.get(part) or []
        if names:
            listed = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
            shown.append(f"{label} ({listed})")
    return ", ".join(shown)


def useless(found: Parts, cli: str) -> str | None:
    """Why a `cli` folder would load nothing of the plugin (so it stays put), or None."""

    loads = LOADS[cli] - ({"hooks"} if cli == "codex" and not found.codex_hooks else set())
    if found.problem or not found.found or set(found.found) & loads:
        return None
    return f"{CLI_NAMES[cli]} would load nothing of it: it has only {_counted(found.found, {**CLAUDE_ONLY, **CODEX_ONLY})}"


def check(found: Parts, source_cli: str, cli: str) -> tuple[list[str], list[str]]:
    """(what waits for your answer, notes) for a plugin from a `source_cli` folder installed in a `cli` folder."""

    if source_cli == cli:
        return [], []
    name = CLI_NAMES[cli]
    if found.problem:
        return [f"{found.problem}, so it can't tell what {name} would load"], []
    asks, notes = [], []
    missing = _counted(found.found, CLAUDE_ONLY if cli == "codex" else CODEX_ONLY)
    if missing:
        asks.append(f"{name} doesn't load its {missing}")
    for server_name, server in sorted(found.servers.items()):
        text = json.dumps(server)
        if cli == "codex" and (match := CLAUDE_VARIABLE_RE.search(text)):
            asks.append(f"its MCP server {server_name} uses ${{{match.group(1)}}}, which Codex doesn't expand, "
                        "so it won't start there")
        elif cli == "codex" and server.get("headersHelper"):
            asks.append(f"its MCP server {server_name} signs in through a headersHelper command, which only Claude Code runs")
        elif cli == "codex" and (shape := mcp.normalize(server)) is not None and (why := mcp.blocked(shape, "codex")):
            asks.append(f"its MCP server {server_name} won't work in Codex: {why}")
        elif cli == "claude" and (match := CODEX_VARIABLE_RE.search(text)):
            asks.append(f"its MCP server {server_name} needs ${match.group(1)}, which only Codex sets")
        elif cli == "claude" and server.get("cwd") and not os.path.isabs(str(server["cwd"])):
            asks.append(f"its MCP server {server_name} starts in a folder relative to the plugin, and Claude Code "
                        "starts plugin servers in your session's folder")
    hooks = found.found.get("hooks") if cli == "claude" else found.codex_hooks
    if hooks and cli == "claude":
        asks.append(f"Claude Code runs its hooks ({', '.join(hooks)}) without asking, where Codex asks you first")
    elif hooks:
        notes.append(f"Codex asks you before it runs its hooks ({', '.join(hooks)})")
    if cli == "codex" and found.found.get("commands"):
        count = len(found.found["commands"])
        notes.append(f"Codex turns its {count} command{'s' if count != 1 else ''} into skills")
    return asks, notes


def logins(found: Parts) -> list[str]:
    """Notes for its URL servers without headers: one that logs in (OAuth) needs that login once in each folder."""

    return [f"its MCP server {name} may need a login (OAuth): log in from that CLI once"
            for name, server in sorted(found.servers.items())
            if (server.get("url") or server.get("serverUrl")) and not server.get("headers") and not server.get("headersHelper")]


def reach(market: Market | None, marketplace: str, source_cli: str, cli: str) -> str | None:
    """Why a `cli` folder can't add the plugin's marketplace, or None."""

    if market is None or not market.source:
        return f"agt doesn't know where the {marketplace} marketplace comes from"
    if source_cli == cli:
        return None
    if cli == "codex" and market.kind not in CODEX_SOURCES:
        return f"Codex adds a marketplace only from a folder or a Git repo, and {marketplace} comes from a {market.kind} source"
    if market.folder is not None and market.folder.is_dir() and \
            not any((market.folder / name).is_file() for name in MARKETPLACE_FILES[cli]):
        return f"the {marketplace} marketplace has no {MARKETPLACE_FILES[cli][0]}, which {CLI_NAMES[cli]} needs"
    return None


def signature(asks: list[str]) -> str:
    """What an approval holds: the answers it was given, so a change that adds to them asks again."""

    return hashlib.sha256("\n".join(sorted(asks)).encode("utf-8")).hexdigest()[:12]


def version_of(plugin_id: str) -> str:
    """One version per plugin: each CLI keeps its own copy up to date, so only where it is counts."""

    return hashlib.sha256(plugin_id.encode("utf-8")).hexdigest()


# The plan --------------------------------------------------------------------------------


@dataclass
class Entry:
    id: str
    copies: dict[str, Plugin] = field(default_factory=dict)  # label -> the plugin there
    decision: engine.Decision | None = None
    held: str | None = None  # why it doesn't go anywhere
    stays: dict[str, str] = field(default_factory=dict)  # label -> why it can't go there
    asks: dict[str, list[str]] = field(default_factory=dict)  # label -> what waits for your answer there
    approved: dict[str, list[str]] = field(default_factory=dict)  # label -> what you said yes to
    notes: dict[str, list[str]] = field(default_factory=dict)  # label -> what to know about the install there
    failing: dict[str, str] = field(default_factory=dict)  # label -> the last install's failure at this version
    sources: dict[str, str] = field(default_factory=dict)  # label -> the copy whose marketplace it installs from
    accept: dict[str, str] = field(default_factory=dict)  # label -> the marketplace command's sha256 you approved

    @property
    def key(self) -> str:
        return f"{KIND}/{self.id}"

    @property
    def name(self) -> str:
        return self.id.partition("@")[0]

    @property
    def writes(self) -> list[str]:
        if self.held or self.decision is None or self.decision.action != engine.WRITE:
            return []
        return [label for label in self.decision.fills if label in self.sources]


@dataclass
class Plan:
    entries: list[Entry] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    by_label: dict[str, providers.Provider] = field(default_factory=dict)  # the Claude Code and Codex folders
    holdings: dict[str, Holding] = field(default_factory=dict)
    others: list[str] = field(default_factory=list)  # folders of CLIs that install no plugins
    later: set[str] = field(default_factory=set)  # labels whose installs wait for the next run
    removing: dict[str, set[str] | None] = field(default_factory=dict)  # id -> labels to uninstall it from (None: all)

    @property
    def writes(self) -> list[tuple[Entry, str]]:
        return [(entry, label) for entry in self.entries for label in entry.writes if label not in self.later]

    @property
    def waiting(self) -> list[tuple[Entry, str]]:
        return [(entry, label) for entry in self.entries for label in sorted(entry.asks)]

    @property
    def pending(self) -> list[Entry]:
        """Plugins uninstalled where you installed them; `agt share rm plugins/<id>` uninstalls the rest."""

        return [entry for entry in self.entries if entry.decision and entry.decision.action == engine.PENDING_DELETE]

    @property
    def conflicts(self) -> list[Entry]:
        return []  # a plugin has one version: it's there or it isn't

    def notes(self, restart: bool = True, done: set[tuple[str, str]] | None = None) -> list[str]:
        """What to know about the installs. `restart`: the sentence on loading them, which a person's report says
        in its Next line instead. `done`: (plugin id, label) of the installs that went in, when a report tells only
        about those."""

        writes = [(entry, label) for entry, label in self.writes if done is None or (entry.id, label) in done]
        found = [f"{entry.name} in {label}: {note}" for entry, label in writes for note in entry.notes.get(label, [])]
        found += [f"{entry.name} in {label}: you said yes to this: {'; '.join(entry.approved[label])}"
                  for entry, label in writes if label in entry.approved]
        if writes and restart:
            found.append("Restart a CLI that got a plugin (or run /reload-plugins in Claude Code) to load it.")
        return found

    def summary(self) -> str:
        if not self.entries and not self.problems and not any(holding.narrow for holding in self.holdings.values()):
            return "Plugins: none in the folders sharing."
        lines = []
        if self.writes:
            lines.append(f"Plugins: {len(self.writes)} to install ("
                         + ", ".join(f"{entry.name} in {label}" for entry, label in self.writes) + ").")
        waiting = sorted({label for entry in self.entries for label in entry.writes if label in self.later})
        if waiting:
            lines.append(f"  Installs in {', '.join(waiting)} wait for the background watcher or `agt share sync`.")
        if self.pending:
            lines.append(f"Plugins: {', '.join(entry.name for entry in self.pending)} uninstalled where you installed "
                         "it; `agt share rm plugins/<id>` uninstalls agt's copies too.")
        if not lines:
            shared = [entry for entry in self.entries if not entry.held and entry.decision]
            lines.append(f"Plugins: {len(shared)} where they go.")
        for entry, label in self.waiting:
            lines.append(f"  {entry.name} waits for your answer before it goes to {label}: {'; '.join(entry.asks[label])}. "
                         f"`agt share pick {entry.key} --in {label}` installs it as it is; `--not-in {label}` keeps it out.")
        held = [f"{entry.name} ({entry.held})" for entry in self.entries if entry.held]
        if held:
            lines.append("  Staying where they are: " + "; ".join(held) + ".")
        lines += [f"  {entry.name} not in {label}: {why}." for entry in self.entries if not entry.held
                  for label, why in sorted(entry.stays.items())]
        lines += [f"  {entry.name} in {label}: the last install failed ({message}); agt tries again when its version "
                  f"changes, or now with `agt share pick {entry.key} --in {label}`."
                  for entry in self.entries for label, message in sorted(entry.failing.items())]
        for label, holding in sorted(self.holdings.items()):
            if holding.narrow:
                names = sorted(plugin_id.partition("@")[0] for plugin_id in holding.narrow)
                lines.append(f"  Installed only for a project in {label}, so not shared: {len(names)} ("
                             + ", ".join(names[:5]) + (", …" if len(names) > 5 else "") + ").")
        if self.others:
            lines.append(f"  {', '.join(self.others)}: {'they install' if len(self.others) > 1 else 'it installs'} no "
                         "plugins; plugins go between Claude Code and Codex folders only.")
        lines += [f"  {note}" for note in self.notes()]
        lines += [f"  {problem}" for problem in self.problems]
        return "\n".join(lines)

    def digest_content(self) -> list[Any]:
        return sorted([entry.id, entry.decision.action if entry.decision else "",
                       sorted(label for label in entry.writes if label not in self.later), sorted(entry.asks),
                       sorted(entry.copies)] for entry in self.entries)


def failures_path() -> Path:
    return share_dir() / "plugins.json"


def failures() -> dict[str, dict[str, str]]:
    data = fsutil.read_json(failures_path(), {})
    return data if isinstance(data, dict) else {}


def agt_made(entry: Entry, current: State) -> set[str]:
    """The CLIs where agt installed this plugin and it's still there: the copies a pick or `disable` may uninstall."""

    record = current.items.get(entry.key) or {}
    return {label for label in record.get("made") or [] if label in entry.copies}


def removal_only(plan: Plan, removing: dict[str, set[str]]) -> Plan:
    """A plan that only uninstalls these plugins from these CLIs (`disable --remove-copies`)."""

    entries = [dataclasses.replace(entry, decision=None) for entry in plan.entries if removing.get(entry.id)]
    return dataclasses.replace(plan, entries=entries, removing={plugin_id: labels for plugin_id, labels in removing.items() if labels})


def _source_for(entry: Entry, cli: str, by_label: dict[str, providers.Provider]) -> str | None:
    """The copy to install from: one in the same CLI when there is one (it loads every part), else any that's on."""

    on = [label for label in sorted(entry.copies) if entry.copies[label].enabled is not False]
    return next((label for label in on if by_label[label].cli == cli), on[0] if on else None)


def build(included: list[providers.Provider], current: State, options: Any, approvals: dict[str, dict[str, str]],
          *, later: frozenset[str] = frozenset()) -> Plan:
    """The plugins in these folders and what a run does with each.

    `options` is the share plan's (picks); `approvals` holds your yes to a plugin that waits for your answer
    (`id -> label -> signature`). Installs in the CLIs in `later` wait for the next run that allows them.
    """

    native = [provider for provider in included if provider.cli in NATIVE]
    plan = Plan(by_label={provider.label: provider for provider in native},
                others=[provider.label for provider in included if provider.cli not in NATIVE],
                later={provider.label for provider in native if provider.cli in later})
    entries: dict[str, Entry] = {}
    unreadable = set()
    for provider in native:
        holding = plan.holdings[provider.label] = read(provider)
        if holding.problem:
            plan.problems.append(holding.problem)
            unreadable.add(provider.label)  # nothing is installed there, and its plugins aren't missing
        for plugin_id, plugin in holding.plugins.items():
            entries.setdefault(plugin_id, Entry(plugin_id)).copies[provider.label] = plugin
    failed = failures()
    for plugin_id in sorted(entries):
        entry = entries[plugin_id]
        plan.entries.append(entry)
        key = entry.key
        if key in options.off:
            entry.held = "you turned it off"
        elif not any(plugin.enabled is True for plugin in entry.copies.values()):
            entry.held = f"it isn't on in {', '.join(sorted(entry.copies))}"
        elif any(plugin.market is not None and plugin.market.bundled for plugin in entry.copies.values()):
            entry.held = f"it comes with Codex ({entry.id.partition('@')[2]} is Codex's own marketplace)"
        if entry.held:
            entry.decision = engine.Decision(engine.NOTHING, reason=entry.held)
            continue
        allowed = set(options.only[key]) if key in options.only else set(plan.by_label)
        allowed -= {label for picked, label in options.skip if picked == key}
        targets: set[str] = set()
        for label, provider in plan.by_label.items():
            if label in entry.copies or label not in allowed or label in unreadable:
                continue
            source_label = _source_for(entry, provider.cli, plan.by_label)
            if source_label is None:
                continue
            source, source_cli = entry.copies[source_label], plan.by_label[source_label].cli
            why = reach(source.market, source.marketplace, source_cli, provider.cli)
            if why:
                entry.stays[label] = why
                continue
            found = parts(source.root)
            why = useless(found, provider.cli) if source_cli != provider.cli else None
            if why:
                entry.stays[label] = why
                continue
            asks, notes = check(found, source_cli, provider.cli)
            scope = plan.holdings[label].narrow.get(plugin_id)
            if scope:
                asks.insert(0, f"it's installed there for a project ({scope} scope), and a user-wide install loads it "
                               "in every project")
            record = failed.get(f"{plugin_id}:{label}")
            if record and record.get("version") == source.version:
                if record.get("command_sha"):
                    asks.append(f"installing it runs a command its marketplace declares: {record.get('command', '')}")
                    entry.accept[label] = record["command_sha"]
                else:
                    entry.failing[label] = record.get("message") or "it failed before"
                    continue
            if asks:
                if (approvals.get(plugin_id) or {}).get(label) != signature(asks):
                    entry.asks[label] = asks
                    continue
                entry.approved[label] = asks
            entry.notes[label] = notes + logins(found)
            entry.sources[label] = source_label
            targets.add(label)
        left_out = {label for label in entry.copies if label not in allowed}  # copies outside your pick stay as they are
        version = version_of(plugin_id)
        copies = [engine.Copy(label, version) for label in entry.copies if label not in left_out]
        record = current.items.get(key) or {}
        last = engine.Last(version=record["version"], copies=dict(record.get("copies") or {}),
                           made=set(record.get("made") or []), stopped=set(record.get("stopped") or [])) \
            if record.get("version") else None
        entry.decision = engine.decide(copies, targets | (set(entry.copies) - left_out), last, left_out | unreadable)
    return plan


# Installing and uninstalling ---------------------------------------------------------------


@dataclass
class Written:
    name: str  # the plugin id
    label: str
    path: Path  # the CLI folder
    action: str  # copy (installed) or remove (uninstalled)
    ok: bool
    message: str = ""


def _env(provider: providers.Provider) -> dict[str, str]:
    from agents_router.runtimes.claude import _default_dir

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # a private marketplace fails fast instead of waiting for a password
    if provider.cli == "claude":
        env.pop("CLAUDE_CONFIG_DIR", None)
        if not fsutil.same_path(provider.folder, _default_dir()):
            env["CLAUDE_CONFIG_DIR"] = str(provider.folder)
    elif provider.cli == "codex":
        env["CODEX_HOME"] = str(provider.folder)
    return env


def steps(plugin: Plugin, cli: str, markets: dict[str, Market], accept: str | None = None) -> list[list[str]]:
    """The commands (without the binary) that install `plugin` in a `cli` folder that knows `markets`."""

    add = [] if plugin.marketplace in markets else [["plugin", "marketplace", "add", str(plugin.source)]]
    if cli == "claude":
        return add + [["plugin", "install", plugin.id, "--json", *(["--accept-command", accept] if accept else [])]]
    return add + [["plugin", "add", plugin.id]]


def _listed(binary: list[str], env: dict[str, str], plugin_id: str) -> bool | None:
    """Whether `plugin list` shows the plugin (None when agt can't tell)."""

    from agents_router import setup

    code, stdout, _ = setup.run_command([*binary, "plugin", "list", "--json"], env, timeout=120)
    listed = setup._json(stdout) if code == 0 else None
    if listed is None:
        return None
    return any(item.get("id") == plugin_id or item.get("pluginId") == plugin_id for item in setup._items(listed))


def _shown_command(stdout: str) -> dict[str, str] | None:
    """The command a marketplace declares for this install, which a person confirms (`claude plugin install --json`)."""

    from agents_router import setup

    for item in setup._items(setup._json(stdout)):
        shown = item.get("shownCommand")
        if isinstance(shown, dict) and isinstance(shown.get("sha256"), str):
            return {"sha": shown["sha256"], "command": str(shown.get("command") or shown.get("text") or "")[:200]}
    return None


def _doing(cli: str, step: list[str]) -> str:
    """The command's words without its arguments, for the step that runs: `codex plugin marketplace add`."""

    return " ".join([cli, *itertools.takewhile(str.isalpha, step)])


def run_steps(plugin: Plugin, label: str, provider: providers.Provider, commands: list[list[str]],
              display: ui.Steps | None = None) -> tuple[bool, list[str]]:
    """Run the install commands with the CLI's own binary, then check `plugin list` shows it. (ok, lines)"""

    from agents_router import setup
    from agents_router.config import load_config
    from agents_router.runtimes import get_runtime

    binary = get_runtime(provider.cli).find_binary(load_config().runtime(provider.cli))
    if not binary:
        return False, [f"the {provider.cli} command isn't installed"]
    env = _env(provider)
    lines = []
    for step in commands:
        shown = " ".join([provider.cli, *step])
        if display is not None:
            display.now(_doing(provider.cli, step))
        code, stdout, stderr = setup.run_command([*binary, *step], env, timeout=600)
        if code != 0:
            command = _shown_command(stdout) if step[1] == "install" else None
            if command:
                message = f"installing it runs a command its marketplace declares: {command['command']}"
                _remember(plugin, label, message, command)
            else:
                message = f"`{shown}` failed (exit {code}): {setup._last_line(stdout, stderr)}"
                _remember(plugin, label, message)
            return False, lines + [message]
        lines.append(f"ran `{shown}`")
    if display is not None:
        display.now(f"{provider.cli} plugin list")
    if _listed(binary, env, plugin.id) is False:
        message = f"the commands worked, but `{provider.cli} plugin list` doesn't show {plugin.id}"
        _remember(plugin, label, message)
        return False, lines + [message]
    _forget(plugin.id, label)
    return True, lines


def uninstall(plugin_id: str, provider: providers.Provider, display: ui.Steps | None = None) -> tuple[bool, str]:
    """`claude plugin uninstall --scope user` or `codex plugin remove`, then check `plugin list` no longer shows it."""

    from agents_router import setup
    from agents_router.config import load_config
    from agents_router.runtimes import get_runtime

    binary = get_runtime(provider.cli).find_binary(load_config().runtime(provider.cli))
    if not binary:
        return False, f"the {provider.cli} command isn't installed"
    env = _env(provider)
    step = ["plugin", "uninstall", plugin_id, "--scope", "user"] if provider.cli == "claude" else ["plugin", "remove", plugin_id]
    if display is not None:
        display.now(_doing(provider.cli, step))
    code, stdout, stderr = setup.run_command([*binary, *step], env, timeout=300)
    if code != 0:
        return False, f"`{' '.join([provider.cli, *step])}` failed (exit {code}): {setup._last_line(stdout, stderr)}"
    if display is not None:
        display.now(f"{provider.cli} plugin list")
    if _listed(binary, env, plugin_id):
        return False, f"`{' '.join([provider.cli, *step])}` worked, but `{provider.cli} plugin list` still shows it"
    return True, ""


def _remember(plugin: Plugin, label: str, message: str, command: dict[str, str] | None = None) -> None:
    data = failures()
    data[f"{plugin.id}:{label}"] = {"version": plugin.version, "message": message[:300],
                                    **({"command": command["command"], "command_sha": command["sha"]} if command else {})}
    fsutil.make_private_dir(failures_path().parent)
    fsutil.write_json(failures_path(), data)


def _forget(plugin_id: str, label: str) -> None:
    data = failures()
    if data.pop(f"{plugin_id}:{label}", None) is not None:
        fsutil.write_json(failures_path(), data)


def _removing(plan: Plan, entry: Entry) -> list[str]:
    labels = plan.removing[entry.id]
    return sorted(set(entry.copies) if labels is None else labels & set(entry.copies))


def counts(plan: Plan) -> tuple[int, int]:
    """How many installs and uninstalls `apply` runs."""

    return (sum(len([label for label in entry.writes if label not in plan.later]) for entry in plan.entries
                if entry.id not in plan.removing or entry.decision is not None),
            sum(len(_removing(plan, entry)) for entry in plan.entries if entry.id in plan.removing))


def apply(plan: Plan, current: State, display: ui.Steps | None = None) -> list[Written]:
    """Install or uninstall each plugin where it goes, and record it. Call it holding the share lock.

    A plugin's record moves on only when all of its installs worked, so after a failure the next run that may
    try again (a new version, or `plugins apply`) installs it then.
    """

    done: list[Written] = []
    for entry in plan.entries:
        decision = entry.decision
        record = dict(current.items.get(entry.key) or {})
        copies = {label: version_of(entry.id) for label in entry.copies}
        if entry.id in plan.removing:
            # `agt share rm` (every copy) or a pick that leaves CLIs out (the copies agt made there).
            labels = plan.removing[entry.id]
            results = []
            for label in _removing(plan, entry):
                if display is not None:
                    display.start(entry.key, ui.muted(label))
                ok, message = uninstall(entry.id, plan.by_label[label], display)
                if display is not None:
                    display.finish(ok, ui.Span("muted", "-"))
                results.append(Written(entry.id, label, plan.by_label[label].folder, "remove", ok, message))
            done += results
            if labels is None:
                if all(result.ok for result in results):
                    current.items.pop(entry.key, None)
                continue
            removed = {result.label for result in results if result.ok}
            for label in removed:
                copies.pop(label, None)
            record["copies"] = {label: version for label, version in record.get("copies", {}).items() if label not in removed}
            record["made"] = sorted(set(record.get("made") or []) - removed)
            record["stopped"] = sorted(set(record.get("stopped") or []) | removed)
        if decision is None or entry.held:
            if record.get("version"):
                current.items[entry.key] = record
            continue
        if decision.action == engine.NOTHING and decision.version:
            record.update(version=decision.version, copies={**record.get("copies", {}), **copies})
        elif decision.action == engine.STOP:
            record["stopped"] = sorted(set(record.get("stopped") or []) | set(decision.stopped))
            record["copies"] = {label: version for label, version in record.get("copies", {}).items()
                                if label not in decision.stopped}
        elif decision.action == engine.WRITE:
            results = []
            for label in [label for label in entry.writes if label not in plan.later]:
                provider, source = plan.by_label[label], entry.copies[entry.sources[label]]
                if display is not None:
                    display.start(entry.key, ui.muted(label))
                commands = steps(source, provider.cli, plan.holdings[label].markets, entry.accept.get(label))
                ok, lines = run_steps(source, label, provider, commands, display)
                if display is not None:
                    display.finish(ok)
                message = f"installed from {source.marketplace}" if ok else lines[-1]
                results.append(Written(entry.id, label, provider.folder, "copy", ok, message))
            done += results
            if not all(result.ok for result in results):
                continue
            written = {result.label for result in results}
            record.update(version=decision.version,
                          copies={**record.get("copies", {}), **copies, **{label: decision.version for label in written}},
                          made=sorted(set(record.get("made") or []) | written))
        if record.get("version"):
            current.items[entry.key] = record
    return done


def attention(plan: Plan) -> list[dict[str, Any]]:
    found = []
    for entry, label in plan.waiting:
        found.append({"id": f"plugin:{entry.key}:{label}", "kind": "plugin", "item": entry.key, "who": "choice",
                      "message": f"plugin {entry.name} waits for your answer before it goes to {label}: "
                                 + "; ".join(entry.asks[label]),
                      "fix": [f"agt share pick {entry.key} --in {label}", f"agt share pick {entry.key} --not-in {label}"],
                      "label": label, "why": "; ".join(entry.asks[label])})
    for entry in plan.entries:
        for label, message in sorted(entry.failing.items()):
            found.append({"id": f"failing:{entry.key}:{label}", "kind": "failing", "item": entry.key, "who": "action",
                          "message": f"agt couldn't install plugin {entry.name} in {label}: {message}",
                          "fix": [f"agt share pick {entry.key} --in {label}", f"agt share pick {entry.key} --not-in {label}"],
                          "label": label, "why": message})
    for entry in plan.pending:
        found.append({"id": f"delete:{entry.key}", "kind": "delete", "item": entry.key, "who": "choice",
                      "message": f"plugin {entry.name} was uninstalled in {', '.join(entry.decision.labels)}; "
                                 "agt's copies of it wait", "fix": [f"agt share rm {entry.key}"],
                      "folders": list(entry.decision.labels)})
    for problem in plan.problems:
        found.append({"id": f"unreadable:plugins:{problem.partition(':')[0]}", "kind": "unreadable", "item": "plugins",
                      "who": "action", "message": f"agt can't read plugins in {problem}", "fix": []})
    return found


def watched_files(included: list[providers.Provider]) -> list[Path]:
    """The files an install, an uninstall or turning a plugin on or off changes (Codex's `config.toml` is watched already)."""

    return [path for provider in included if provider.cli == "claude"
            for path in (provider.folder / "settings.json", provider.folder / "plugins" / "installed_plugins.json",
                         provider.folder / "plugins" / "known_marketplaces.json")]


# `agt share plugins apply`: install now, after you say yes -----------------------------------


@dataclass
class Offer:
    plugin: Plugin
    source_label: str
    label: str  # the CLI folder that doesn't have it
    cli: str
    steps: list[list[str]] = field(default_factory=list)  # the commands, without the binary
    why_not: str | None = None  # why agt can't install it there
    asks: list[str] = field(default_factory=list)  # what it would be missing there, which your yes covers
    failed: str | None = None  # an earlier install there failed (at this version)

    @property
    def key(self) -> str:
        return f"{self.plugin.id}:{self.label}"


def offers(included: list[providers.Provider], off: set[str] | None = None, current: State | None = None) -> list[Offer]:
    """Every plugin on in one Claude Code or Codex folder and missing from another, with the steps or why not."""

    options = dataclasses.make_dataclass("Picks", ["off", "only", "skip"])(set(off or ()), {}, [])
    plan = build(included, current or State(), options, {})
    found = []
    for entry in plan.entries:
        if entry.held:
            continue
        for label in sorted(set(plan.by_label) - set(entry.copies)):
            source_label = entry.sources.get(label) or _source_for(entry, plan.by_label[label].cli, plan.by_label)
            if source_label is None:
                continue
            source, cli = entry.copies[source_label], plan.by_label[label].cli
            offer = Offer(source, source_label, label, cli, why_not=entry.stays.get(label),
                          asks=entry.asks.get(label, []), failed=entry.failing.get(label))
            if not offer.why_not:
                offer.steps = steps(source, cli, plan.holdings[label].markets, entry.accept.get(label))
            found.append(offer)
    return found


def record_install(current: State, plugin_id: str, label: str) -> None:
    """`plugins apply` installed it: the copy is agt's, as a sync's would be (uninstalling it stops it going there)."""

    record = current.items.setdefault(f"{KIND}/{plugin_id}", {})
    record["version"] = version_of(plugin_id)
    record["copies"] = {**record.get("copies", {}), label: record["version"]}
    record["made"] = sorted({*record.get("made", []), label})
    record["stopped"] = [other for other in record.get("stopped", []) if other != label]
