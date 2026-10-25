"""Agent tool names from Claude Code to the other CLIs.

Only names that are known to exist are written: an unknown tool name can make
an Antigravity agent hang. A Claude tool with no counterpart is reported as
lost. Codex agents can't limit tools at all, which `tool_widening` decides.
"""

from __future__ import annotations

import re

# Kiro CLI 2.x (V2 engine): the names its own agent files use.
KIRO_V2 = {
    "Bash": ["execute_bash"], "Read": ["fs_read"], "LS": ["fs_read"], "Write": ["fs_write"], "Edit": ["fs_write"],
    "MultiEdit": ["fs_write"], "NotebookEdit": ["fs_write"], "Glob": ["glob"], "Grep": ["grep"],
    "WebFetch": ["web_fetch"], "WebSearch": ["web_search"],
}
KIRO_V3_RENAMES = {"execute_bash": "shell"}
# agy 1.3.6, as it sends them to PreToolUse hooks (agy_review/rules.py).
ANTIGRAVITY = {
    "Bash": ["run_command", "command_status"], "Read": ["view_file"], "LS": ["list_dir"], "Write": ["write_to_file"],
    "Edit": ["replace_file_content"], "MultiEdit": ["multi_replace_file_content"], "Glob": ["find_by_name"],
    "Grep": ["grep_search"],
}
SHELL_TOOLS = {"Bash"}
TOOL_WIDENING = ("allow-if-shell", "skip")
MCP_RE = re.compile(r"^mcp__(?P<server>[^_].*?)(?:__(?P<tool>.+))?$")


def parse_tools(value: str | list | None) -> list[str] | None:
    """Claude's `tools` field: None when it's missing, which means every tool."""

    if value is None:
        return None
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def mcp_reference(tool: str) -> tuple[str, str | None, str | None] | None:
    """(server, tool or None for the whole server, plugin or None) for an `mcp__...` tool name."""

    match = MCP_RE.match(tool)
    if not match:
        return None
    server, name = match.group("server"), match.group("tool")
    if server.startswith("plugin_"):
        plugin, _, rest = server[len("plugin_"):].partition("_")
        return (rest or plugin), name, plugin
    return server, name, None


def servers_named(tools: list[str] | None) -> list[str]:
    """MCP servers an agent's tools call, in order: the ones that must come along with it."""

    found = []
    for tool in tools or []:
        reference = mcp_reference(tool)
        if reference and reference[2] is None and reference[0] not in found:
            found.append(reference[0])
    return found


def to_kiro(tools: list[str], engine: str = "v2") -> tuple[list[str], list[str]]:
    mapped, lost = [], []
    for tool in tools:
        reference = mcp_reference(tool)
        if reference and reference[2] is None:
            names = [f"@{reference[0]}/{reference[1]}" if reference[1] else f"@{reference[0]}"]
        else:
            names = KIRO_V2.get(tool)
        if not names:
            lost.append(tool)
            continue
        for name in names:
            name = KIRO_V3_RENAMES.get(name, name) if engine == "v3" else name
            if name not in mapped:
                mapped.append(name)
    return mapped, lost


def to_antigravity(tools: list[str]) -> tuple[list[str], list[str]]:
    mapped, lost = [], []
    for tool in tools:
        names = ["call_mcp_tool"] if mcp_reference(tool) else ANTIGRAVITY.get(tool)
        if not names:
            lost.append(tool)
            continue
        mapped += [name for name in names if name not in mapped]
    return mapped, lost


def _reverse(table: dict[str, list[str]]) -> dict[str, str]:
    back: dict[str, str] = {}
    for claude, names in table.items():
        for name in names:
            back.setdefault(name, claude)
    return back


FROM_KIRO = {**_reverse(KIRO_V2), "shell": "Bash"}
FROM_ANTIGRAVITY = _reverse(ANTIGRAVITY)


def to_claude(cli: str, tools: list[str] | None) -> list[str] | None:
    """Another CLI's tool list as Claude names. None means every tool; names with no Claude tool are left out."""

    if tools is None or cli == "codex":
        return None
    if cli == "claude":
        return list(tools)
    if cli == "kiro" and "*" in tools:
        return None
    found: list[str] = []
    for tool in tools:
        if cli == "kiro" and tool.startswith("@"):
            server, _, name = tool[1:].partition("/")
            name = f"mcp__{server}__{name}" if name else f"mcp__{server}"
        else:
            name = (FROM_KIRO if cli == "kiro" else FROM_ANTIGRAVITY).get(tool)
        if name and name not in found:
            found.append(name)
    return found


def for_target(source: str, target: str, tools: list[str] | None, servers: set[str], engine: str = "v2") -> list[str] | None:
    """The tools a new copy in `target` gets. None leaves the field out, which means every tool.

    A Kiro copy leaves out the tools of MCP servers that Kiro doesn't have,
    so it never names a server it can't load.
    """

    claude = to_claude(source, tools)
    if claude is None or target == "codex":
        return None
    if target == "claude":
        return claude
    if target == "antigravity":
        return to_antigravity(claude)[0]
    mapped = to_kiro(claude, engine)[0]
    return [tool for tool in mapped if not tool.startswith("@") or tool[1:].partition("/")[0] in servers]


def codex_problem(tools: list[str] | None, widening: str) -> str | None:
    """Why a Claude agent with these tools may not go to Codex, which can't limit tools."""

    if tools is None:
        return None
    if widening == "skip":
        return "Codex can't limit an agent's tools (tool_widening = \"skip\")"
    if not SHELL_TOOLS & set(tools):
        return "Codex can't limit an agent's tools, and this one has no shell (tool_widening = \"allow-if-shell\")"
    return None
