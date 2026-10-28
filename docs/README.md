# Agents Router documentation

Guides for Agents Router (`agt`): sharing agents, skills, instructions, MCP servers, hooks and plugins between Claude Code, Codex CLI, Kiro CLI and Antigravity, and running subagents in those CLIs on your own accounts. Start with the [README](../README.md) for install and a quick start.

## Guides

| Guide | What it covers |
|---|---|
| [Hosts](hosts.md) | Installing the plugin in Claude Code, Codex and Antigravity, or the MCP server in any other MCP client, and how long a tool call may wait in each |
| [Sharing agents and skills](share.md) | `agt share`: agents, skills, instruction files, MCP servers, hooks and plugins across Claude Code, Codex, Antigravity and Kiro, with repos, conflicts, backups and the background watcher |
| [Claude Code](claude-code.md) | `claude -p` subagents on your other Claude accounts: what every account shares, switching, limits, troubleshooting |
| [Codex](codex.md) | `codex exec` subagents: extra accounts, quota, web search, reasoning summaries |
| [Gemini through Antigravity](gemini.md) | `agy` subagents: native relay agents, extra Google accounts at your own risk, the review gate and sandbox |
| [Kiro](kiro.md) | `kiro-cli` subagents: access levels, what a worker sees, what a live run still has to confirm |
| [Configuration](configuration.md) | Settings files per user and workspace, `agt config`, the log, limits, overrides, model pools, files and environment variables |
| [Delegation rules](delegation.md) | The optional `agt rules` template: model tiers, work folders, helper limits |

## Design and research

- [Design and build plan](design.md): goals, architecture, the shared setup per account, and what was verified.
- Research: what the design rests on.
  - CLIs: [permissions and headless modes compared](research/cli-permissions-and-headless.md), [Claude Code as a headless worker](research/claude-code-headless.md), [what a Claude Code session knows](research/claude-code-context-inventory.md), [exit codes](research/exit-codes.md), [model capacity errors](research/capacity-errors.md), [read access command safety](research/read-safety.md).
  - Accounts and terms: [provider terms](research/provider-terms.md), [Claude](research/claude-multi-account.md), [Codex](research/codex-multi-account.md) and [Antigravity](research/antigravity-multi-account.md) with several accounts, [limits](research/limits.md).
  - Native subagents: [in Claude Code](research/native-subagent-display.md), [in Codex](research/codex-native-agents.md), [in Antigravity](research/antigravity-native-agents.md), [an automatic reviewer for Antigravity workers](research/antigravity-auto-review.md), [the Codex sandbox on Windows](research/codex-windows-sandbox.md).
  - Other projects: [prior art](research/prior-art.md), [lifecycle and sharing elsewhere](research/lifecycle-and-sharing.md), [OpenCodex](research/opencodex.md), [installing through an agent](research/agent-install.md), [delegation rules notes](research/delegation-rules.md).

## The standalone Antigravity adapter

Superseded by the Antigravity runtime in [Gemini through Antigravity](gemini.md). Kept for reference: [architecture](antigravity/architecture.md), [setup on Windows](antigravity/setup.md), [configuration](antigravity/configuration.md).
