# Using CLI Subagents from each host

The host is the CLI your main session runs in. CLI Subagents is one MCP server, packaged as a plugin for Claude Code, Codex and Antigravity. Every host gets the same tools and the same skill. What differs is how you install it and how long a single tool call may wait.

First install the `csa` command once (Python 3.11+, no dependencies):

```bash
pipx install git+https://github.com/lunati228/CLI-Subagents
```

## Claude Code

```text
/plugin install cli-subagents --marketplace lunati228/CLI-Subagents
```

On older Claude Code versions: `/plugin marketplace add lunati228/CLI-Subagents`, then `/plugin install cli-subagents@cli-subagents`.

You get the MCP server, the `cli-subagents` skill, `/cli-subagents:delegate` and two hooks that call `csa` (so `csa` must be on PATH). The hooks wake an idle session when a subagent finishes or asks a question, and list uncollected results after a resume or compact; see [Claude Code](claude-code.md#messages-and-wake-ups). A `subagent_wait` call that runs longer than about two minutes moves to the background on its own (Claude Code 2.1.212+), and Claude gets a notification when it returns, so waiting doesn't block the session.

## Codex

```bash
codex plugin marketplace add lunati228/CLI-Subagents
codex plugin add cli-subagents@cli-subagents
```

Codex reads the same plugin manifest as Claude Code (checked with Codex 0.162.1: `codex mcp list` then shows the `cli-subagents` server). Without the plugin, add the server yourself:

```toml
# ~/.codex/config.toml
[mcp_servers.cli-subagents]
command = "csa"
args = ["mcp"]
tool_timeout_sec = 300
```

Codex ends a single MCP call after a few minutes (300 seconds in the 0.162.1 source; older docs say 60), so CLI Subagents caps waits from Codex at 240 seconds. Call `subagent_wait` again until the jobs finish. `wait_seconds` on `subagent_start` and `subagent_status` is capped the same way.

## Antigravity

```bash
agy plugin install https://github.com/lunati228/CLI-Subagents
```

The repository root has the Antigravity plugin files (`plugin.json`, `mcp_config.json`) next to the shared skill; `agy plugin validate` accepts them and converts `/delegate` into a skill. Keep waits short, as with Codex. The plugin hooks are Claude Code only.

## Any other MCP client

Register a stdio server with command `csa` and argument `mcp`. For example, in Cursor's `~/.cursor/mcp.json`:

```json
{ "mcpServers": { "cli-subagents": { "command": "csa", "args": ["mcp"] } } }
```

Copy `skills/cli-subagents/SKILL.md` into the client's rules or skills if it supports them, so the agent knows how to use the tools.

## Which subagent CLI runs the work

That's chosen per job with `runtime` (`claude`, `codex` or `antigravity`), independent of the host. A Codex session can start Claude Code subagents and the other way round. A subagent in a different CLI than its host gets a short note naming the host's instruction files and skill folders, so it can follow the same conventions without anything being copied.
