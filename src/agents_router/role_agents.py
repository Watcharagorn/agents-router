"""Role agent files agt-<role> for the orchestrator agt runs in.

Interface (fixed; other modules call it):
    sync(loaded: config.Config, host: str | None = None, dry_run: bool = False) -> list[str]
        Bring this orchestrator's agt-<role> files in line with loaded.roles: write one per enabled
        role, remove agt's own file for a role that is off or gone, never touch a file without the
        "Managed by agt roles" line. Returns one human line per file changed or kept.

Which orchestrator: `host` names it, or a list names several (claude, codex, antigravity; kiro has
no relay agents, so nothing): setup and update pass the CLIs you named with --host. With host None it's the CLI agt runs in (hosts.detect_host()). From a plain terminal
there is no such CLI: then only the CLIs that already hold agt's role agents are kept in line, so
`agt roles config --disable builder` at a terminal still removes agt's own agt-builder file, and
a CLI that never had role agents gets none.

Where the files go, in each CLI's own relay format (the templates in templates/role-agents/):

    Claude Code  <profile>/agents/agt-<role>.md, in every profile that has the plugin
    Codex        <CODEX_HOME>/agents/agt-<role>.toml
    Antigravity  agt's relay plugin, ~/.gemini/config/plugins/agents-router-relays/agents/agt-<role>.md

Each file runs `agt run --follow --role <role>` and carries no provider list, so a change of
providers never rewrites it. `agt share` never copies them (share/exclude.py skips every agt-* agent).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json
from pathlib import Path
from typing import Any, Iterable

from agents_router import hosts, roles
from agents_router.fsutil import is_link, write_private

MARKER = "Managed by agt roles"
MARKER_LINE = f"{MARKER}: agt setup, agt update and agt roles config rewrite or remove this file; delete this line to make it yours."
HOSTS = ("claude", "codex", "antigravity")  # Kiro has no relay agents (Q19)
SUFFIX = {"claude": ".md", "codex": ".toml", "antigravity": ".md"}
LABELS = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}
RELAYS_PLUGIN = "agents-router-relays"  # setup.ANTIGRAVITY_RELAYS


@dataclass(frozen=True)
class Folder:
    host: str
    label: str  # "Claude Code (claude-ntb)", "Codex"
    path: Path  # the agents folder
    problem: str | None = None  # why agt won't write there


@dataclass(frozen=True)
class Change:
    kind: str  # "write", "remove", "kept", "skipped"
    host: str
    line: str


def _template(name: str) -> str:
    return files("agents_router").joinpath("templates", *name.split("/")).read_text(encoding="utf-8")


def description(role: roles.Role) -> str:
    """The agent's description: the role's use-me-when line (Q29), then how to call it."""

    how = ("all at once, and returns one result per provider" if role.access == "read"
           else "one at a time, the first in its priority order with room")
    return (f"{role.use_when} Runs the {role.name} role through Agents Router (`agt run --role {role.name}`): agt picks "
            f"the providers from the user's [roles.{role.name}] settings, {how}. Start it in the background. Begin the "
            "prompt with optional lines effort=LEVEL, cwd=PATH, timeout=SECONDS, window=SECONDS (default 60), override=USER_WORDS "
            "(only with the user's request for more jobs), then the complete, self-contained task.")


def render(role: roles.Role, host: str) -> bytes:
    """The agt-<role> file for one CLI, in the format of that CLI's own relay agents."""

    body = _template(f"role-agents/{host}.md").replace("{{ROLE}}", role.name)
    about = description(role)
    if host == "claude":
        text = "\n".join([
            "---", f"# {MARKER_LINE}", f"name: agt-{role.name}", f"description: {json.dumps(about, ensure_ascii=False)}",
            "model: haiku", "effort: low", "color: cyan", "tools: Bash", "maxTurns: 200", "omitClaudeMd: true", "---",
        ]) + "\n" + body
    elif host == "antigravity":
        # The relay's own frontmatter settings (tools, model, policy), so the two kinds stay alike.
        relay = _template("antigravity-plugin/agents/agt-codex.md").split("\n---\n", 1)[0].splitlines()[1:]
        kept = [line for line in relay if not line.startswith(("#", "name:", "description:"))]
        text = "\n".join(["---", f"# {MARKER_LINE}", f"name: agt-{role.name}",
                          f"description: {json.dumps(about, ensure_ascii=False)}", *kept, "---"]) + "\n" + body
    else:
        from agents_router.share import codex_toml

        relay = _template("codex-agents/agt-codex.toml")
        settings = [line for line in relay.splitlines() if line.startswith(("model =", "model_reasoning_effort ="))]
        tail = relay[relay.index("\n[features]"):]
        text = "\n".join([f"# {MARKER_LINE}", f'name = "agt-{role.name}"',
                          f"description = {codex_toml.render_value(about)}", *settings,
                          "developer_instructions = '''", body.rstrip("\n"), "'''"]) + "\n" + tail
    return text.encode("utf-8")


def ours(path: Path) -> bool:
    """Whether agt wrote this file: one of its first lines carries the marker. A link is never agt's."""

    if is_link(path):
        return False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = [handle.readline() for _ in range(3)]
    except OSError:
        return False
    return any(MARKER in line for line in head)


def _claude_folders(loaded: Any, extra: Iterable[Path]) -> list[Folder]:
    from agents_router import setup
    from agents_router.profiles import same_folder

    accounts = [account for account in setup.claude_profiles(loaded, every=True) if setup.has_plugin(account.home)]
    for folder in extra:
        if not any(same_folder(folder, account.home) for account in accounts):
            match = next((account for account in setup.claude_profiles(loaded, every=True)
                          if same_folder(folder, account.home)), None)
            if match is not None:
                accounts.append(match)
    return [Folder("claude", f"{LABELS['claude']} ({setup.profile_name(account)})", Path(account.home) / "agents",
                   "the profile's agents folder is a link" if is_link(Path(account.home) / "agents") else None)
            for account in accounts]


def _antigravity_folder(loaded: Any, dry_run: bool) -> Folder:
    from agents_router.runtimes.antigravity import gemini_root

    root = gemini_root(loaded.runtime("antigravity"))
    plugin = root / "config" / "plugins" / RELAYS_PLUGIN
    problem = None
    if any(is_link(folder) for folder in (root / "config", plugin.parent, plugin, plugin / "agents")):
        problem = f"won't write through a folder link under {plugin}"
    elif not dry_run and not (plugin / "plugin.json").is_file():
        problem = f"agt's relay plugin isn't in {plugin} yet; `agt setup --host antigravity` installs it with the role agents"
    return Folder("antigravity", LABELS["antigravity"], plugin / "agents", problem)


def folders(loaded: Any, host: str, dry_run: bool = False, claude_folders: Iterable[Path] = ()) -> list[Folder]:
    """The agents folders one CLI reads, where its role agents go."""

    if host == "claude":
        return _claude_folders(loaded, claude_folders)
    if host == "codex":
        path = Path(loaded.runtime("codex").primary).expanduser() / "agents"
        return [Folder("codex", LABELS["codex"], path, "the agents folder is a link" if is_link(path) else None)]
    if host == "antigravity":
        return [_antigravity_folder(loaded, dry_run)]
    return []


def own_files(folder: Folder) -> list[Path]:
    """agt's role agents in one folder: agt-* files with the marker, never the built-in relays."""

    relays = {f"agt-{runtime}{SUFFIX[folder.host]}" for runtime in roles.RUNTIMES}
    if not folder.path.is_dir():
        return []
    return sorted(path for path in folder.path.glob(f"agt-*{SUFFIX[folder.host]}")
                  if path.name not in relays and ours(path))


def _hosts(loaded: Any, host: str | list[str] | None) -> list[str]:
    """The CLIs to sync: those named (setup's and update's explicit --host), else the one agt runs in."""

    if host is not None:
        named = [host] if isinstance(host, str) else list(host)
        return [name for name in HOSTS if name in named]
    found = hosts.detect_host()
    if found != "unknown":
        return [found] if found in HOSTS else []
    # A plain terminal: keep in line only the CLIs that already have agt's role agents.
    return [name for name in HOSTS if any(own_files(folder) for folder in folders(loaded, name))]


def changes(loaded: Any, host: str | list[str] | None = None, dry_run: bool = False, claude_folders: Iterable[Path] = ()) -> list[Change]:
    """`sync`, with each line's kind: what setup and update put in their reports."""

    wanted = {name: role for name, role in loaded.roles.items() if role.enabled}
    found: list[Change] = []
    for name in _hosts(loaded, host):
        for folder in folders(loaded, name, dry_run, claude_folders):
            found += _sync_folder(folder, wanted, dry_run)
    return found


def _sync_folder(folder: Folder, wanted: dict[str, roles.Role], dry_run: bool) -> list[Change]:
    out: list[Change] = []
    suffix = SUFFIX[folder.host]
    if folder.problem:
        if wanted or own_files(folder):
            out.append(Change("skipped", folder.host, f"{folder.label}: role agents not written: {folder.problem}."))
        return out
    for name, role in wanted.items():
        target = folder.path / f"agt-{name}{suffix}"
        content = render(role, folder.host)
        exists = target.exists() or is_link(target)
        if exists and not ours(target):
            out.append(Change("kept", folder.host,
                              f"{folder.label}: kept {target}: it isn't agt's (no \"{MARKER}\" line). Rename it, or keep "
                              f"it to go on using your own {name}."))
            continue
        try:
            if exists and target.read_bytes() == content:
                continue
            if not dry_run:
                write_private(target, content)
        except OSError as exc:
            out.append(Change("skipped", folder.host, f"{folder.label}: couldn't write role agent agt-{name}: {exc}"))
            continue
        verb = "would install" if dry_run else "installed"
        out.append(Change("write", folder.host, f"{folder.label}: {verb} role agent agt-{name} at {target}"))
    for path in own_files(folder):
        name = path.name[len("agt-"):-len(suffix)]
        if name in wanted:
            continue
        try:
            if not dry_run:
                path.unlink()
        except OSError as exc:
            out.append(Change("skipped", folder.host, f"{folder.label}: couldn't remove {path}: {exc}"))
            continue
        verb = "would remove" if dry_run else "removed"
        out.append(Change("remove", folder.host, f"{folder.label}: {verb} {path} (agt's own file; role {name} is off or gone)"))
    return out


def sync(loaded, host=None, dry_run=False, *, claude_folders=()):
    """Bring this orchestrator's agt-<role> files in line with loaded.roles (see the module notes)."""

    return [change.line for change in changes(loaded, host, dry_run, claude_folders)]

