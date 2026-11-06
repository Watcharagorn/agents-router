"""Read the installed role candidates and build bounded text for Claude Code hooks.

Only settings layers and agent files are read: no CLI, runtime account discovery,
subprocesses or writes belong on this hook path. Plan mode is handled by the gate,
not by the catalog, so the session note can still explain the planning rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents_router import config, levels, role_agents, roles
from agents_router.share import frontmatter

SENTENCE_LIMIT = 160
NOTE_LIMIT = 4_000
DENY_LIMIT = 2_000

_NOTE_START = "Agents Router role agents are installed in this profile and are the first choice for subagent work:"
_NOTE_END = '''A hook turns back Explore, Plan and general-purpose calls with this list. A call whose description starts
with "agt-fallback: <reason>" passes, for when no role fits or the user or a skill asked for that agent.
Other subagents (claude-code-guide, web-fetch, statusline-setup, fork, plugin agents) are not affected.
Your own agents are called as usual when no role above does their job. When one does the same job as a role above,
the role comes first.
Planning stays in the main session: when drafting a plan, or in plan mode, the main session gathers the data and writes
the plan itself. It does not start a subagent with mode "plan" or a Plan agent to plan for it.'''
_DENY_START = ("Agents Router role agents come first for Explore, Plan and general-purpose work in this profile. "
               "Installed candidates:")
_DENY_END = ("If none fits, a listed role is unavailable, or the user or a skill asked for {agent_type} by name, call {agent_type} "
             "again with a description that starts with 'agt-fallback: <reason>'.")


def _sentence(description: str, name: str) -> str:
    cut = description.rfind(role_agents.anchor(name))
    sentence = description[:cut] if cut >= 0 else description
    return " ".join(sentence.split())[:SENTENCE_LIMIT].rstrip()


def _shadowed(workspace: levels.Workspace | None) -> set[str]:
    if workspace is None:
        return set()
    found = set()
    for path in (workspace.root / ".claude" / "agents").rglob("*.md"):
        name = frontmatter.parse(path.read_text(encoding="utf-8")).fields.get("name")
        if isinstance(name, str):
            found.add(name)
    return found


def catalog(hook_input: dict[str, Any], env: dict[str, str], cwd: Path | str | None) -> list[tuple[str, str]]:
    """Managed, enabled role agents callable from this profile, or [] on silence or any error."""

    try:
        if (not isinstance(hook_input, dict) or "AGT_JOB_ID" in env or int(env.get("AGT_DEPTH") or 0) > 0
                or "agent_id" in hook_input or not env.get("CLAUDECODE")):
            return []
        workspace = levels.find_workspace(cwd)
        found = levels.layers(config.user_settings_path(env), workspace)
        if not config.subagents_from_layers(found).prefer_roles:
            return []
        enabled = {name for name, role in roles.from_layers(found).items() if role.enabled}
        profile = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
        folder = role_agents.Folder("claude", "Claude Code", profile / "agents")
        shadowed = _shadowed(workspace)
        candidates = []
        for path in role_agents.own_files(folder):
            agent_name = path.stem
            name = agent_name.removeprefix("agt-")
            if name not in enabled or agent_name in shadowed:
                continue
            fields = frontmatter.parse(path.read_text(encoding="utf-8")).fields
            if fields.get("name") != agent_name:
                continue
            description = fields.get("description")
            if not isinstance(description, str):
                return []
            candidates.append((agent_name, _sentence(description, name)))
        return sorted(candidates)
    except Exception:
        return []  # A hook failure must leave Claude Code free to call its subagent.


def _bounded(candidates: list[tuple[str, str]], start: str, end: str, cap: int, *, deny: bool = False) -> str:
    if not candidates:
        return ""
    candidates = sorted(candidates)
    rows: list[str] = []
    used = len(start) + 1 + len(end)  # Keep all fixed instructions, even when no role line fits.
    for name, sentence in candidates:
        row = f"- {name} ({sentence})" if deny else f"- {name}: {sentence}"
        remaining = len(candidates) - len(rows) - 1
        more = f"+{remaining} more (agt roles show)" if remaining else ""
        if used + len(row) + 1 + (len(more) + 1 if more else 0) > cap:
            break
        rows.append(row)
        used += len(row) + 1
    remaining = len(candidates) - len(rows)
    # Put the overflow count last while keeping the escape and planning instructions intact.
    return "\n".join([start, *rows, end, *([f"+{remaining} more (agt roles show)"] if remaining else [])])


def session_note(candidates: list[tuple[str, str]]) -> str:
    """The SessionStart additionalContext text, at most 4,000 characters."""

    return _bounded(candidates, _NOTE_START, _NOTE_END, NOTE_LIMIT)


def deny_reason(candidates: list[tuple[str, str]], agent_type: str) -> str:
    """The PreToolUse permissionDecisionReason text, at most 2,000 characters."""

    end = _DENY_END.format(agent_type=agent_type)
    return _bounded(candidates, _DENY_START, end, DENY_LIMIT, deny=True)
