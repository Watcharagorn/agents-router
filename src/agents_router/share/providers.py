"""Where each CLI keeps its agents and skills, for every folder agt shares.

A provider is one CLI's config folder: a Claude folder (`~/.claude`,
`~/.claude-lab`, any `CLAUDE_CONFIG_DIR`), Codex's `~/.codex`, Antigravity's
`~/.gemini` or Kiro's `~/.kiro`. Each kind can have several locations, such as
Codex skills in `~/.agents/skills` and `~/.codex/skills`. Locations with the
same real path are one location, and new items go only to the first writable
one. Whether a CLI is installed is checked by looking for its binary, never by
running it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil

from agents_router.config import Config, default_primary, homes_dir
from agents_router.share import exclude, model

CLIS = ("claude", "codex", "antigravity", "kiro")
KINDS = ("agents", "skills")
BINARY = {"claude": "claude", "codex": "codex", "antigravity": "agy", "kiro": "kiro-cli"}
CLAUDE_MARKERS = (".claude.json", "settings.json", "projects", "statsig", "agents", "skills")


@dataclass
class Location:
    kind: str
    path: Path
    writable: bool = True

    @property
    def real(self) -> str:
        """The resolved path, or the path itself while it doesn't exist."""

        try:
            return str(self.path.resolve())
        except OSError:
            return str(self.path)


@dataclass
class Provider:
    cli: str
    folder: Path
    label: str
    installed: bool
    locations: dict[str, list[Location]] = field(default_factory=dict)

    def writable(self, kind: str) -> Location | None:
        return next((location for location in self.locations.get(kind, []) if location.writable), None)


@dataclass
class Candidate:
    """One row of the folder list: a provider, whether it's suggested, and why it's greyed out."""

    provider: Provider
    suggested: bool
    reason: str | None = None
    holds: dict[str, int] = field(default_factory=dict)  # shareable items per kind
    in_use: bool = False  # holds any agent or skill files, shareable or not
    missing_binary: bool = False  # greyed only because the CLI's binary wasn't found


def locations(cli: str, folder: Path, home: Path) -> dict[str, list[Location]]:
    if cli == "claude":
        return {"agents": [Location("agents", folder / "agents")], "skills": [Location("skills", folder / "skills")]}
    if cli == "codex":
        return {
            "agents": [Location("agents", folder / "agents")],
            # Codex reads user skills from ~/.agents/skills; ~/.codex/skills is deprecated, so it's only read.
            "skills": [Location("skills", home / ".agents" / "skills"), Location("skills", folder / "skills", writable=False)],
        }
    if cli == "antigravity":
        # The user skills folder isn't verified: the CLI docs say antigravity-cli/skills, the app's config/skills.
        return {
            "agents": [Location("agents", folder / "config" / "agents")],
            "skills": [Location("skills", folder / "antigravity-cli" / "skills"),
                       Location("skills", folder / "config" / "skills", writable=False)],
        }
    if cli == "kiro":
        return {"agents": [Location("agents", folder / "agents")], "skills": [Location("skills", folder / "skills")]}
    raise ValueError(f"unknown CLI {cli}")


def installed(cli: str, config: Config) -> bool:
    if cli == "kiro":
        return bool(os.environ.get("AGT_KIRO_COMMAND") or shutil.which(BINARY["kiro"]))
    from agents_router.runtimes import get_runtime

    return get_runtime(cli).find_binary(config.runtime(cli)) is not None


def base_label(cli: str, folder: Path) -> str:
    """`claude-lab` for ~/.claude-lab, `claude` for ~/.claude; the CLI's name for the others."""

    if cli != "claude":
        return cli
    name = folder.name.lstrip(".")
    return name if name.startswith("claude") else f"claude-{name}" if name else "claude"


def _short(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def make_providers(folders: list[tuple[str, Path]], config: Config, home: Path | None = None) -> list[Provider]:
    home = home or Path.home()
    seen: list[tuple[str, Path]] = []
    for cli, folder in folders:
        folder = folder.expanduser().absolute()
        if not any(cli == other and same(folder, path) for other, path in seen):
            seen.append((cli, folder))
    bases = [base_label(cli, folder) for cli, folder in seen]
    cache: dict[str, bool] = {}
    result = []
    for (cli, folder), base in zip(seen, bases):
        if cli not in cache:
            cache[cli] = installed(cli, config)
        # Two folders with the same label get `<cli>@<folder>` instead.
        label = base if bases.count(base) == 1 else f"{cli}@{_short(folder)}"
        result.append(Provider(cli=cli, folder=folder, label=label, installed=cache[cli], locations=locations(cli, folder, home)))
    return result


def same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def account_folders(config: Config) -> dict[str, str]:
    """Real paths of extra-account folders `agt setup --account` registered -> the primary they follow."""

    found = {}
    for cli in ("claude", "codex", "antigravity"):
        runtime = config.runtime(cli)
        for account in runtime.accounts:
            found[str(Path(account.home).expanduser().resolve())] = str(runtime.primary)
    return found


def holds(provider: Provider, disabled: set[str] | None = None) -> dict[str, int]:
    """How many shareable agents and skills the provider's locations hold."""

    counts = {}
    for kind in KINDS:
        names = set()
        for location in provider.locations.get(kind, []):
            if kind == "agents":
                for path in model.agent_files(provider.cli, location.path):
                    name = model.agent_name(provider.cli, path)
                    if not exclude.agent(provider.cli, name, path, _kiro_data(provider.cli, path)):
                        names.add(name)
            elif location.path.is_dir():
                for entry in location.path.iterdir():
                    if entry.is_dir() and not exclude.skill(entry.name, entry, disabled):
                        names.add(entry.name)
        counts[kind] = len(names)
    return counts


def in_use(provider: Provider) -> bool:
    for kind in KINDS:
        for location in provider.locations.get(kind, []):
            if kind == "agents" and model.agent_files(provider.cli, location.path):
                return True
            if kind == "skills" and location.path.is_dir() and any(entry.is_dir() for entry in location.path.iterdir()):
                return True
    return False


def _kiro_data(cli: str, path: Path) -> dict | None:
    if cli != "kiro":
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def discovered_folders(config: Config, home: Path | None = None) -> list[tuple[str, Path]]:
    """Every CLI folder on this machine, the same way `agt setup` finds them."""

    home = home or Path.home()
    claude = {default_primary("claude"), home / ".claude"}
    try:
        entries = list(home.iterdir())
    except OSError:
        entries = []
    claude |= {entry for entry in entries if entry.name.startswith(".claude-") and entry.is_dir()
               and any((entry / marker).exists() for marker in CLAUDE_MARKERS)}
    folders: list[tuple[str, Path]] = [("claude", folder) for folder in sorted(claude)]
    folders.append(("codex", default_primary("codex")))
    folders.append(("antigravity", default_primary("antigravity").parent))
    folders.append(("kiro", home / ".kiro"))
    return [(cli, folder) for cli, folder in folders if folder.is_dir()]


def candidates(config: Config, folders: list[tuple[str, Path]] | None = None, *, home: Path | None = None,
               no_agy: bool = False) -> list[Candidate]:
    """The folder list: every provider folder, suggested when its CLI is installed and it holds agents or skills.

    A folder whose agents are all left out (Kiro's KiroCrew agents) is still in use, so it's suggested as a target.
    """

    home = home or Path.home()
    folders = discovered_folders(config, home) if folders is None else folders
    accounts = account_folders(config)
    agt_homes = str(homes_dir().resolve())
    codex_home = next((folder for cli, folder in folders if cli == "codex"), default_primary("codex"))
    disabled = exclude.codex_disabled_skills(codex_home)
    rows = []
    for provider in make_providers(folders, config, home):
        real = str(provider.folder.resolve())
        counts = holds(provider, disabled)
        reason = None
        if real == agt_homes or real.startswith(agt_homes + os.sep):
            reason = "agt's own account folders"
        elif real in accounts:
            reason = f"agt account, follows {accounts[real]} through links"
        elif provider.cli == "antigravity" and no_agy:
            reason = "left out with --no-agy"
        elif not provider.installed:
            reason = f"{BINARY[provider.cli]} isn't installed; run `agt setup` again after installing it"
        used = in_use(provider)
        rows.append(Candidate(provider=provider, suggested=reason is None and used, reason=reason, holds=counts, in_use=used,
                              missing_binary=reason is not None and not provider.installed and reason.startswith(BINARY[provider.cli])))
    return rows
