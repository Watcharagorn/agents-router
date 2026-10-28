"""Which agent CLI the main session runs in, and where its context lives.

A subagent in the same CLI as its main session loads that context on its own.
A subagent in a different CLI (a Codex worker for a Claude Code session, say)
doesn't know the parent's instruction files or skills, so it gets a short note
pointing at them. Nothing is copied or edited; the note only names paths that exist.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

HOSTS = ("claude", "codex", "antigravity")


def detect_host(client_name: str | None = None, env: dict[str, str] | None = None) -> str:
    """Name of the host CLI, from the MCP client name or, failing that, the environment."""

    name = (client_name or "").lower()
    if "claude" in name:
        return "claude"
    if "codex" in name:
        return "codex"
    if "antigravity" in name or "agy" in name or "gemini" in name:
        return "antigravity"
    env = dict(os.environ if env is None else env)
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if any(key.startswith(("CODEX_THREAD", "CODEX_SANDBOX")) for key in env):
        return "codex"
    return "unknown"


def agent_running(env: dict[str, str] | os._Environ | None = None) -> bool:
    """Whether an agent rather than a person runs this command: a host CLI's session or an agt worker."""

    env = os.environ if env is None else env
    try:
        depth = int(env.get("AGT_DEPTH") or 0)
    except ValueError:
        depth = 0
    return bool(env.get("AGT_JOB_ID")) or depth > 0 or detect_host(env=dict(env)) != "unknown"


def _existing(paths: list[Path]) -> list[str]:
    seen: list[str] = []
    for path in paths:
        try:
            if path.exists() and str(path) not in seen:
                seen.append(str(path))
        except OSError:
            continue
    return seen


def host_context(host: str, cwd: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Instruction files and skill folders the host's main session uses for this project."""

    env = dict(os.environ if env is None else env)
    project = Path(cwd)
    home = Path.home()
    instructions: list[Path] = []
    skills: list[Path] = []
    if host == "claude":
        config = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        instructions = [
            config / "CLAUDE.md",
            config / "rules",
            project / "CLAUDE.md",
            project / ".claude" / "CLAUDE.md",
            project / "CLAUDE.local.md",
            project / ".claude" / "rules",
        ]
        skills = [config / "skills", project / ".claude" / "skills"]
    elif host == "codex":
        codex_home = Path(env.get("CODEX_HOME") or home / ".codex")
        instructions = [codex_home / "AGENTS.md", project / "AGENTS.md", project / "AGENTS.override.md"]
        skills = [codex_home / "skills", project / ".agents" / "skills", project / ".codex" / "skills"]
    elif host == "antigravity":
        instructions = [home / ".gemini" / "GEMINI.md", project / "GEMINI.md", project / "AGENTS.md", project / ".agents" / "rules"]
        # The CLI's docs name antigravity-cli/skills and the app's config/skills; a machine has one or the other.
        skills = [home / ".gemini" / "antigravity-cli" / "skills", home / ".gemini" / "config" / "skills",
                  project / ".agents" / "skills"]
    return {"host": host, "instructions": _existing(instructions), "skills": _existing(skills)}


HOST_LABELS = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity CLI", "kiro": "Kiro CLI"}


def parent_note(parent: dict[str, Any] | None, runtime: str) -> str:
    """Extra prompt text for a worker whose CLI differs from its main session's."""

    if not parent or parent.get("host") in (None, "unknown", runtime):
        return ""
    instructions = parent.get("instructions") or []
    skills = parent.get("skills") or []
    if not instructions and not skills:
        return ""
    label = HOST_LABELS.get(str(parent["host"]), str(parent["host"]))
    lines = [f"You were started by a {label} session, which works with this context:"]
    if instructions:
        lines.append("Instruction files and folders (read these first and follow them where they apply): " + ", ".join(instructions))
    if skills:
        lines.append(
            "Skill folders (each skill is a folder with a SKILL.md; use one when its description fits your task): "
            + ", ".join(skills)
        )
    return "\n".join(lines) + "\n\n"
