# CLI Subagents in Antigravity

## Tools

After `csa setup` or `agy plugin install https://github.com/lunati228/CLI-Subagents` from a terminal (or a `cli-subagents` entry in `~/.gemini/config/mcp_config.json` with command `csa` and argument `mcp`), the `subagent_*` tools are available from the `cli-subagents` server, and `/delegate` is available as a skill.

## Waiting

Keep each `subagent_wait` short (a few minutes) and call it again while jobs run. Messages and results are kept until you collect them.

## Where the shared setup lives

Antigravity workers run on the signed-in account. Extra Google accounts are opt-in and at the user's own risk (Google's terms rule out automated account rotation on consumer plans); each extra account gets the setup below through links, and `account` pins a job to one. Your Antigravity setup lives in:

| What | Where |
|---|---|
| Instructions | `~/.gemini/GEMINI.md`, `<project>/GEMINI.md`, `<project>/AGENTS.md` |
| Rules, workflows, skills | `<project>/.agents/rules/`, `.agents/workflows/`, `.agents/skills/` |
| MCP servers | `~/.gemini/config/mcp_config.json`, `<project>/.agents/mcp_config.json` |
| Plugins | `agy plugin install <folder, URL or plugin@marketplace>` from a terminal, stored under `~/.gemini/antigravity-cli/plugins/` |

## Mixing CLIs

From Antigravity you can still start Claude Code and Codex subagents on the user's accounts (`runtime: "claude"` or `"codex"`). They get a note listing your GEMINI.md, AGENTS.md, rules and skill folders and read them first.
