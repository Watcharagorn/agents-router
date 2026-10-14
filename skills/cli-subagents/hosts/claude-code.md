# CLI Subagents in Claude Code

## Tools

The plugin's tools appear as `mcp__plugin_cli-subagents_cli-subagents__subagent_start` and so on (or `mcp__cli-subagents__...` if the server was added by hand). `/cli-subagents:delegate <task>` runs the whole loop.

## Waiting

`subagent_wait` may run up to an hour. After about two minutes Claude Code moves the call to the background and notifies you when it returns, so start jobs, call `subagent_wait`, and keep working on anything that doesn't depend on them.

The plugin's hooks also work without a wait. After `subagent_start`, a background hook wakes an idle session when the job finishes or asks a question. After a resume or compact, a hook lists finished jobs whose result nobody collected; read one with `subagent_result`. Each result is delivered once.

## Where the shared setup lives

Subagents on the user's other Claude accounts get everything from the main config folder (`~/.claude`, or `CLAUDE_CONFIG_DIR`). When you or the user add something new, put it there and every account has it on the next run:

| To add | Put it in |
|---|---|
| A skill | `~/.claude/skills/<name>/SKILL.md` (all projects) or `<project>/.claude/skills/<name>/SKILL.md` |
| A subagent definition | `~/.claude/agents/<name>.md` or `<project>/.claude/agents/` |
| A slash command | `~/.claude/commands/<name>.md` or `<project>/.claude/commands/` |
| A workflow | `~/.claude/workflows/` or `<project>/.claude/workflows/` |
| Rules | `~/.claude/rules/` or `<project>/.claude/rules/` |
| Instructions | `~/.claude/CLAUDE.md` (all projects) or `<project>/CLAUDE.md` |
| An MCP server | `claude mcp add --scope user ...` (all projects) or `<project>/.mcp.json` |
| A plugin | `/plugin install ...` in the main session |
| Settings, hooks, env, permissions | `~/.claude/settings.json` |

Never edit files inside `~/.cli-subagents/homes/`; those account folders are links to the setup above and are rebuilt before each run. Things that stay per account: the claude.ai login, claude.ai connectors, and browser logins for remote MCP servers (`/mcp` once per account). Tools built into the Claude desktop app itself (browser or terminal panes) belong to the app and aren't available to workers.

## Mixing CLIs

A Codex or Antigravity subagent started from here gets a note listing `~/.claude/CLAUDE.md`, the project's `CLAUDE.md` and `.claude/rules`, and your skill folders, and reads them first. Mention anything else it needs in the task.
