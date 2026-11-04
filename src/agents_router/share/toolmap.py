"""Agent tool names from Claude Code to the other CLIs.

Only names that are known to exist are written: an unknown tool name can make
an Antigravity agent hang. A Claude tool with no counterpart is reported as
lost. Codex agents can't limit tools at all, which `tool_widening` decides.

Kiro copies get the universal tags (`read`, `write`, `shell`, `web`,
`subagent`), which both of Kiro's engines read and its V3 auto-upgrade leaves
alone. A tag is a group: read back, `read` is Read, Glob, Grep and LS, so a
narrower Claude agent is widened in Kiro, never cut down.
"""

from __future__ import annotations

import re

# Kiro CLI 2.x (V2 engine): the names its own agent files use. The Kiro runtime trusts these; share writes tags.
KIRO_V2 = {
    "Bash": ["execute_bash"], "Read": ["fs_read"], "LS": ["fs_read"], "Write": ["fs_write"], "Edit": ["fs_write"],
    "MultiEdit": ["fs_write"], "NotebookEdit": ["fs_write"], "Glob": ["glob"], "Grep": ["grep"],
    "WebFetch": ["web_fetch"], "WebSearch": ["web_search"],
}
# Kiro's universal agent format (2.37.1 and later, both engines): one tag per group of tools.
KIRO_GROUPS = {
    "read": ["Read", "Glob", "Grep", "LS"], "write": ["Write", "Edit", "MultiEdit", "NotebookEdit"], "shell": ["Bash"],
    "web": ["WebFetch", "WebSearch"], "subagent": ["Task"],
}
KIRO_TAGS = {claude: tag for tag, group in KIRO_GROUPS.items() for claude in group}
# Kiro's V2 and V3 tool names, read back as the tag of their whole group.
KIRO_ALIASES = {name: KIRO_TAGS[claude] for claude, names in KIRO_V2.items() for name in names}
# The V2 names share wrote into Kiro copies before the tags; `kiro_retagged` moves a copy off them.
KIRO_V2_NAMES = set(KIRO_ALIASES)
KIRO_ALIASES.update({
    **{name: "read" for name in ("read_file", "grep_search", "list_directory", "file_search")},
    "str_replace": "write", "run_command": "shell",
})
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


def to_kiro(tools: list[str]) -> tuple[list[str], list[str]]:
    """Claude tools as Kiro tags (and `@server/tool` names), and the tools Kiro has no tag for."""

    mapped, lost = [], []
    for tool in tools:
        reference = mcp_reference(tool)
        if reference and reference[2] is None:
            name = f"@{reference[0]}/{reference[1]}" if reference[1] else f"@{reference[0]}"
        else:
            name = KIRO_TAGS.get(tool)
        if not name:
            lost.append(tool)
        elif name not in mapped:
            mapped.append(name)
    return mapped, lost


def kiro_retagged(tools: object) -> list | None:
    """A Kiro copy's `tools` with the V2 names share used to write turned into tags; None when it has none of them.

    Every other entry (tags, `@server` names, `*`, names agt doesn't know) stays as it is, in its place.
    """

    if not isinstance(tools, list):
        return None
    found: list = []
    changed = False
    for tool in tools:
        name = tool
        if isinstance(tool, str) and tool in KIRO_V2_NAMES:
            name = KIRO_ALIASES[tool]
            changed = True
        if name not in found:
            found.append(name)
    return found if changed else None


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
        if cli == "kiro" and tool == "@builtin":  # every built-in tool: all the groups together
            names = [name for group in KIRO_GROUPS.values() for name in group]
        elif cli == "kiro" and tool.startswith("@"):
            server, _, name = tool[1:].partition("/")
            names = [f"mcp__{server}__{name}" if name else f"mcp__{server}"]
        elif cli == "kiro":
            names = KIRO_GROUPS.get(KIRO_ALIASES.get(tool, tool), [])  # a tag or an alias: its whole group
        else:
            names = [FROM_ANTIGRAVITY[tool]] if tool in FROM_ANTIGRAVITY else []
        found += [name for name in names if name not in found]
    return found


def for_target(source: str, target: str, tools: list[str] | None, servers: set[str]) -> list[str] | None:
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
    mapped = to_kiro(claude)[0]
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
