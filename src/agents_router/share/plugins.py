"""Plugins you turned on in one CLI, offered to your other CLIs (`agt share plugins`).

agt reads which plugins are on where, and lists each one another CLI can
install natively from the same marketplace: Claude Code and Codex install the
same plugin packages. It never installs on its own: only `agt share plugins
apply` does, after you say yes (in a terminal, or `--yes` with the plugins
named), and never the watcher.

What agt reads:

| CLI | Plugins | Marketplaces |
|---|---|---|
| Claude Code | `enabledPlugins` in the folder's `settings.json`, with `plugins/installed_plugins.json` (user scope) | `plugins/known_marketplaces.json` |
| Codex | `[plugins."<id>"] enabled` in `config.toml` | `[marketplaces.<name>]` in `config.toml` |

A plugin turned off on purpose (`false`) is never turned on or installed there,
a plugin installed only for a project is never offered user-wide, and agt's
own plugin is left to `agt setup`. A failed install is remembered and not
offered again until the plugin's version changes. Antigravity and Kiro aren't
offered plugins yet, and a plugin's parts (agents, skills, MCP servers, hooks)
reach CLIs that can't install it only through those kinds' own sharing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tomllib
from typing import Any

from agents_router import fsutil
from agents_router.share import providers
from agents_router.share.state import share_dir

KIND = "plugins"
OWN = "agents-router"
NATIVE = ("claude", "codex")  # CLIs that install the same plugin packages


@dataclass
class Plugin:
    id: str  # name@marketplace
    enabled: bool | None  # True on, False turned off on purpose, None installed but not listed as either
    version: str = ""
    source: str | None = None  # what `plugin marketplace add` takes, when agt knows it

    @property
    def name(self) -> str:
        return self.id.partition("@")[0]

    @property
    def marketplace(self) -> str:
        return self.id.partition("@")[2]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _source(entry: Any) -> str | None:
    """`owner/repo`, a git URL or a folder: what `plugin marketplace add` takes."""

    if isinstance(entry, str):
        return entry or None
    if not isinstance(entry, dict):
        return None
    inner = entry.get("source") if isinstance(entry.get("source"), dict) else entry
    for key in ("repo", "url", "path", "source"):
        value = inner.get(key)
        if isinstance(value, str) and value and value not in ("github", "git", "directory", "url"):
            return value
    return None


def inventory(provider: providers.Provider) -> dict[str, Plugin]:
    """The provider's user plugins: on, turned off, or installed (Claude Code and Codex)."""

    folder = provider.folder
    found: dict[str, Plugin] = {}
    if provider.cli == "claude":
        settings = _read_json(folder / "settings.json")
        enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
        installed = (_read_json(folder / "plugins" / "installed_plugins.json") or {})
        installed = installed.get("plugins") if isinstance(installed, dict) else None
        markets = _read_json(folder / "plugins" / "known_marketplaces.json")
        markets = markets if isinstance(markets, dict) else {}
        for plugin_id, entries in (installed or {}).items():
            users = [entry for entry in entries or [] if isinstance(entry, dict) and entry.get("scope", "user") == "user"]
            if users:
                found[plugin_id] = Plugin(plugin_id, None, str(users[0].get("version") or ""))
        for plugin_id, on in (enabled or {}).items():
            if isinstance(on, bool) and (plugin_id in found or not on):
                found.setdefault(plugin_id, Plugin(plugin_id, on)).enabled = on
        for plugin in found.values():
            plugin.source = _source(markets.get(plugin.marketplace))
    elif provider.cli == "codex":
        try:
            config = tomllib.loads((folder / "config.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            config = {}
        markets = config.get("marketplaces") if isinstance(config.get("marketplaces"), dict) else {}
        for plugin_id, table in (config.get("plugins") or {}).items() if isinstance(config.get("plugins"), dict) else []:
            if isinstance(table, dict) and "@" in plugin_id:
                on = table.get("enabled", True)
                plugin = Plugin(plugin_id, on if isinstance(on, bool) else None, str(table.get("version") or ""))
                plugin.source = _source(markets.get(plugin.marketplace))
                found[plugin_id] = plugin
    return {plugin_id: plugin for plugin_id, plugin in found.items() if plugin.name != OWN}


# What could go where -----------------------------------------------------------------------


@dataclass
class Offer:
    plugin: Plugin
    source_label: str
    label: str  # the CLI folder that doesn't have it
    cli: str
    steps: list[list[str]] = field(default_factory=list)  # the commands, without the binary
    why_not: str | None = None  # why agt can't install it there
    failed: str | None = None  # an earlier install there failed (at this version)

    @property
    def key(self) -> str:
        return f"{self.plugin.id}:{self.label}"


def failures_path() -> Path:
    return share_dir() / "plugins.json"


def failures() -> dict[str, dict[str, str]]:
    data = fsutil.read_json(failures_path(), {})
    return data if isinstance(data, dict) else {}


def offers(included: list[providers.Provider], off: set[str] | None = None) -> list[Offer]:
    """Every plugin that's on in one CLI and that another could install natively, with the steps or why not."""

    native = [provider for provider in included if provider.cli in NATIVE]
    held = {provider.label: inventory(provider) for provider in native}
    failed = failures()
    found: list[Offer] = []
    seen: set[tuple[str, str]] = set()
    for provider in native:
        for plugin in held[provider.label].values():
            if plugin.enabled is not True or f"{KIND}/{plugin.id}" in (off or set()):
                continue
            for target in native:
                there = held[target.label].get(plugin.id)
                if target.label == provider.label or (plugin.id, target.label) in seen or there is not None:
                    continue  # it has it (on, or turned off on purpose)
                seen.add((plugin.id, target.label))
                offer = Offer(plugin, provider.label, target.label, target.cli)
                if not plugin.source:
                    offer.why_not = f"agt doesn't know where the {plugin.marketplace} marketplace comes from"
                else:
                    markets = {other.marketplace for other in held[target.label].values()}
                    add = ["plugin", "marketplace", "add", plugin.source]
                    install = ["plugin", "install" if target.cli == "claude" else "add", plugin.id]
                    offer.steps = ([] if plugin.marketplace in markets else [add]) + [install]
                record = failed.get(offer.key)
                if record and record.get("version") == plugin.version:
                    offer.failed = record.get("message") or "it failed before"
                found.append(offer)
    return found


def summary(found: list[Offer]) -> list[str]:
    ready = [offer for offer in found if not offer.why_not and not offer.failed]
    lines = [f"Plugins: {len(ready)} could be installed in another CLI"
             + (f" ({', '.join(f'{offer.plugin.name} in {offer.label}' for offer in ready)}); "
                "`agt share plugins apply` asks you about each." if ready else ".")]
    lines += [f"  {offer.plugin.name} in {offer.label}: {offer.why_not}" for offer in found if offer.why_not]
    lines += [f"  {offer.plugin.name} in {offer.label}: the last install failed ({offer.failed}); agt tries again when "
              "its version changes, or name it: `agt share plugins apply " + offer.key + "`"
              for offer in found if offer.failed]
    return lines


# Installing ----------------------------------------------------------------------------------


def _env(provider: providers.Provider) -> dict[str, str]:
    from agents_router.runtimes.claude import _default_dir

    env = dict(os.environ)
    if provider.cli == "claude":
        env.pop("CLAUDE_CONFIG_DIR", None)
        if not fsutil.same_path(provider.folder, _default_dir()):
            env["CLAUDE_CONFIG_DIR"] = str(provider.folder)
    elif provider.cli == "codex":
        env["CODEX_HOME"] = str(provider.folder)
    return env


def install(offer: Offer, provider: providers.Provider) -> tuple[bool, list[str]]:
    """Run the offer's steps with the CLI's own command, then check `plugin list` shows it. (ok, lines)"""

    from agents_router import setup
    from agents_router.config import load_config
    from agents_router.runtimes import get_runtime

    binary = get_runtime(offer.cli).find_binary(load_config().runtime(offer.cli))
    if not binary:
        return False, [f"the {offer.cli} command isn't installed"]
    env = _env(provider)
    lines = []
    for step in offer.steps:
        shown = " ".join([offer.cli, *step])
        code, stdout, stderr = setup.run_command([*binary, *step], env, timeout=300)
        if code != 0:
            message = f"`{shown}` failed (exit {code}): {setup._last_line(stdout, stderr)}"
            _remember(offer, message)
            return False, lines + [message]
        lines.append(f"ran `{shown}`")
    code, stdout, _ = setup.run_command([*binary, "plugin", "list", "--json"], env, timeout=120)
    listed = setup._json(stdout) if code == 0 else None
    if listed is not None and not any(item.get("id") == offer.plugin.id or item.get("pluginId") == offer.plugin.id
                                      for item in setup._items(listed)):
        message = f"the commands worked, but `{offer.cli} plugin list` doesn't show {offer.plugin.id}"
        _remember(offer, message)
        return False, lines + [message]
    _forget(offer)
    return True, lines


def _remember(offer: Offer, message: str) -> None:
    data = failures()
    data[offer.key] = {"version": offer.plugin.version, "message": message[:300]}
    fsutil.make_private_dir(failures_path().parent)
    fsutil.write_json(failures_path(), data)


def _forget(offer: Offer) -> None:
    data = failures()
    if data.pop(offer.key, None) is not None:
        fsutil.write_json(failures_path(), data)
