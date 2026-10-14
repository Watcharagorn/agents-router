# CLI Subagents in Codex

## Tools

After `codex plugin add cli-subagents@cli-subagents` (or a `[mcp_servers.cli-subagents]` entry with `command = "csa"`, `args = ["mcp"]`), the tools are `subagent_start`, `subagent_wait`, `subagent_message`, `subagent_status`, `subagent_result`, `subagent_cancel`, `subagent_list` and `subagent_accounts` on the `cli-subagents` server.

## Waiting

Codex ends a single MCP call after a few minutes, so `subagent_wait` returns after at most about four minutes. If jobs are still running, do other work and call `subagent_wait` again. Nothing is lost between calls; messages and results wait for you.

## Where the shared setup lives

Subagents on the user's other ChatGPT accounts get the setup of the main Codex home (`~/.codex`, or `CODEX_HOME`). Add new things there:

| To add | Put it in |
|---|---|
| A skill | `~/.codex/skills/<name>/SKILL.md`, or `<project>/.agents/skills/` |
| Instructions | `~/.codex/AGENTS.md` (all projects) or `<project>/AGENTS.md` |
| An MCP server | `codex mcp add <name> -- <command>` or `[mcp_servers.<name>]` in `~/.codex/config.toml` |
| A plugin | `codex plugin add <plugin>@<marketplace>` |
| Model defaults, features, profiles | `~/.codex/config.toml` |

Don't edit files inside `~/.cli-subagents/homes/codex/`; they're rebuilt from the main home before each run. Each account keeps its own login (`auth.json`) and session history.

## Mixing CLIs

Claude Code and Antigravity subagents started from here get a note listing `~/.codex/AGENTS.md`, the project's `AGENTS.md` and your skill folders. Claude Code also reads a project `AGENTS.md` by itself when the project has no `CLAUDE.md`.
