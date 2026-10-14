# CLI Subagents in Antigravity

## Tools

After `agy plugin install https://github.com/lunati228/CLI-Subagents` (or a `cli-subagents` entry in `~/.gemini/config/mcp_config.json` with command `csa` and argument `mcp`), the `subagent_*` tools are available from the `cli-subagents` server, and `/delegate` is available as a skill.

## Waiting

Keep each `subagent_wait` short (a few minutes) and call it again while jobs run. Messages and results are kept until you collect them.

## Where the shared setup lives

Antigravity itself runs as one account today (Google's terms rule out automated account rotation on consumer plans; an opt-in rotation is planned), so there is nothing to sync between Antigravity accounts. Your Antigravity setup lives in:

| What | Where |
|---|---|
| Instructions | `~/.gemini/GEMINI.md`, `<project>/GEMINI.md`, `<project>/AGENTS.md` |
| Rules, workflows, skills | `<project>/.agents/rules/`, `.agents/workflows/`, `.agents/skills/` |
| MCP servers | `~/.gemini/config/mcp_config.json`, `<project>/.agents/mcp_config.json` |
| Plugins | `agy plugin install ...`, stored under `~/.gemini/antigravity-cli/plugins/` |

## Mixing CLIs

From Antigravity you can still start Claude Code and Codex subagents on the user's accounts (`runtime: "claude"` or `"codex"`). They get a note listing your GEMINI.md, AGENTS.md, rules and skill folders and read them first.
