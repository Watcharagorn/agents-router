"""Items that are never shared, and why.

Each check returns the reason shown in `status` and the setup list, or None.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

OWN_AGENT_PREFIX = "agt-"  # agt's relay agents
OWN_SKILL = "agents-router"
NOT_SHARED_SKILLS = {"synced": "skills/synced is never shared"}
# Agents that rely on tools only KiroCrew has.
KIROCREW_PREFIX = "kirocrew"
KIROCREW_ONLY = {"ops-reporter"}


def agent(cli: str, name: str, path: Path, data: dict | None = None) -> str | None:
    if name.startswith("."):
        return "hidden file"
    if name.startswith(OWN_AGENT_PREFIX):
        return "agt's own relay agent"
    if name.lower().startswith(KIROCREW_PREFIX):
        return "a KiroCrew agent"
    if name in KIROCREW_ONLY:
        return "relies on KiroCrew-only tools"
    if cli == "kiro":
        if path.with_suffix(".lock").exists():
            return f"managed by {path.with_suffix('.lock').name}"
        if data is not None and str(data.get("prompt") or "").strip().lower().startswith("file://"):
            return "its prompt is a file:// link"
    return None


def skill(name: str, folder: Path, disabled: set[str] | None = None) -> str | None:
    if name.startswith("."):
        return "hidden folder"
    if name in NOT_SHARED_SKILLS:
        return NOT_SHARED_SKILLS[name]
    if name == OWN_SKILL or name.startswith(OWN_AGENT_PREFIX):
        return "agt's own skill"
    if not (folder / "SKILL.md").is_file():
        return "no SKILL.md"
    if disabled and _real(folder) in disabled:
        return "disabled in Codex"
    return None


def codex_disabled_skills(codex_home: Path) -> set[str]:
    """Real paths of skill folders that Codex's config.toml turns off (`[[skills.config]]`, enabled = false)."""

    try:
        data = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    entries = (data.get("skills") or {}).get("config") if isinstance(data.get("skills"), dict) else None
    found = set()
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("enabled") is False and entry.get("path"):
            path = Path(str(entry["path"])).expanduser()
            found.add(_real(path.parent if path.name == "SKILL.md" else path))
    return found


def _real(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)
