# Using Agents Router from each host

The host is the CLI your main session runs in. Agents Router is one MCP server, packaged as a plugin for Claude Code, Codex and Antigravity. Every host gets the same tools and the same skill. What differs is how you install it and how long a single tool call may wait.

First install the `agt` command once (Python 3.11+, no dependencies):

```bash
pipx install git+https://github.com/Watcharagorn/agents-router
```

Then `agt setup` installs the plugin into every host below that is installed, using each host's own terminal commands, so an agent can run it for you. It skips what's already there, `--dry-run` shows the plan, and `--host` limits it to one host. It also registers account folders that are already logged in (`--account max-2=~/.claude-2`) and ends with what you still have to do, such as logins and restarts. The sections below give the commands it runs.

## Claude Code

From a terminal (this is what `agt setup` runs; checked with Claude Code 2.1.295):

```bash
claude plugin marketplace add Watcharagorn/agents-router
claude plugin install agents-router@agents-router
claude plugin list
```

Claude Code 2.1.292 and later also take one command: `claude plugin install agents-router --marketplace Watcharagorn/agents-router`. Inside a session the same thing is `/plugin install agents-router --marketplace Watcharagorn/agents-router`, or `/plugin marketplace add Watcharagorn/agents-router` then `/plugin install agents-router@agents-router` on older versions. A plugin installed from a terminal loads when Claude Code restarts, or after `/reload-plugins` in an open session.

If there is no `claude` command (the Claude desktop app often doesn't put one on PATH), `agt setup` looks for the copy bundled with the app. When it finds none, it adds the marketplace and enables the plugin in `settings.json` instead, keeping every other key and saving a backup:

```json
{
  "extraKnownMarketplaces": {
    "agents-router": { "source": { "source": "github", "repo": "Watcharagorn/agents-router" } }
  },
  "enabledPlugins": { "agents-router@agents-router": true }
}
```

Claude Code installs the plugin from there on its next start. `agt setup --claude-route settings` takes this route on purpose.

You get the MCP server, three agent types that show jobs in the agent list (`agents-router:agt-claude`, `-codex` and `-antigravity`; see [Claude Code](claude-code.md#subagents-in-the-agent-list)), the `agents-router` skill, `/agents-router:delegate` and two hooks that call `agt` (so `agt` must be on PATH). The hooks wake an idle session when a subagent finishes or asks a question, and list uncollected results after a resume or compact; see [Claude Code](claude-code.md#messages-and-wake-ups). A `subagent_wait` call that runs longer than about two minutes moves to the background on its own (Claude Code 2.1.212+), and Claude gets a notification when it returns, so waiting doesn't block the session.

## Codex

Run `agt setup --host codex` to install the plugin and three native relay agents.
The plugin commands it runs are:

```bash
codex plugin marketplace add Watcharagorn/agents-router
codex plugin add agents-router@agents-router
codex plugin list
```

Codex reads the same plugin manifest as Claude Code (checked with Codex 0.162.1: `codex mcp list` then shows the `agents-router` server). Its plugin loader does not register custom agents, so setup also installs the bundled TOML definitions in `~/.codex/agents/` (or `CODEX_HOME/agents/`). `agt update` refreshes them, preserving changed files in backups. A repeat setup keeps matching files untouched. If you installed only through `codex plugin add`, run `agt setup --host codex` to get the relays. Start a new Codex session to load them.

Use the native `spawn_agent` tool with `agent_type` set to `agt-claude`, `agt-codex` or `agt-antigravity`. The prompt takes the same option lines as the Claude Code relays, such as `model=gpt-6.1-sol`, `effort=high` and `access=read`, followed by the task. Those options select the worker. The relay itself runs on `gpt-6-luna` at low reasoning and copies the authenticated worker result back to the parent. Prefer these relays over `subagent_start` when available. See the [Codex host instructions](../skills/agents-router/hosts/codex.md) for a tool-call example and follow-ups.

Codex's documentation says the app exposes individual subagent threads for inspection; the CLI's `/subagents` picker switches between them. The relay runs `agt run --follow`, which prints the worker's steps into that thread. The exact app labels and live refresh behavior still need an app check. Codex inherits the parent's sandbox and MCP servers and supports only some capability reductions per agent; the shell-only relay behavior is an instruction, not an enforced tool allowlist. The [research note](research/codex-native-agents.md) records the sources and remaining app checks.

The agent files are personal Codex configuration: disabling or uninstalling the plugin does not remove them. Remove the three `agt-*.toml` files from the main home's `agents/` folder if you no longer want them.

When native relays are unavailable, use the MCP tools. Without the plugin, add the server yourself:

```toml
# ~/.codex/config.toml
[mcp_servers.agents-router]
command = "agt"
args = ["mcp"]
tool_timeout_sec = 300
```

Codex ends a single MCP call after a few minutes (300 seconds in the 0.162.1 source; older docs say 60), so Agents Router caps waits from Codex at 240 seconds. Call `subagent_wait` again until the jobs finish. `wait_seconds` on `subagent_start` and `subagent_status` is capped the same way.

## Antigravity

```bash
agy plugin install https://github.com/Watcharagorn/agents-router
agy plugin list
```

Run `agt setup --host antigravity` to get the main plugin and three native relay
agents. Setup and `agt update` install a companion plugin,
`agents-router-relays`, in `~/.gemini/config/plugins/`. Its `agents/` files use
Antigravity's format; the repository's root `agents/` files belong to Claude
Code and Antigravity skips them. Repeat setup leaves matching files untouched,
refuses links and backs up custom contents. If you installed only with
`agy plugin install`, run setup once to add the relays. Start a new session.

Ask the main agent to use native `invoke_subagent` with
`agt-claude`, `agt-codex` or `agt-antigravity`.
Pass the same option lines as the Claude relays, then the complete task. The
relay calls `agt run --follow --window 60` and copies the authenticated result
unchanged. In the app, open its conversation in the subagent panel; in the CLI,
use `/agents` and Enter to inspect tool output. Send follow-ups to that same
native agent, including after it finishes. Prefer these relays over
`subagent_start` when available. See the
[Antigravity host instructions](../skills/agents-router/hosts/antigravity.md).

The relay uses the supported `flash` model tier. Antigravity rejects exact
model IDs in native agent definitions, so the resolved Gemini model and effort
must be checked in the app. Copy fidelity, live refresh and relay cost also
need an app check; the [research note](research/antigravity-native-agents.md)
records the sources and local CLI validation. The companion is a separate
plugin: removing the main plugin does not remove it. Remove its
`~/.gemini/config/plugins/agents-router-relays/` directory to remove the relays.

The repository root provides the MCP server and shared skill. When native
relays are unavailable, use its `subagent_*` tools and keep waits short, as
with Codex. The wake-up hooks are Claude Code only.

## Any other MCP client

Register a stdio server with command `agt` and argument `mcp`. For example, in Cursor's `~/.cursor/mcp.json`:

```json
{ "mcpServers": { "agents-router": { "command": "agt", "args": ["mcp"] } } }
```

Copy the whole `skills/agents-router/` folder into the client's skills if it supports them. This keeps `SKILL.md` and its linked `hosts/*.md` notes together. If the client accepts only rules text, inline `SKILL.md` and the relevant host note.

## Which subagent CLI runs the work

That's chosen per job with `runtime` (`claude`, `codex` or `antigravity`), independent of the host. A Codex session can start Claude Code subagents and the other way round. A subagent in a different CLI than its host gets a short note naming the host's instruction files and skill folders, so it can follow the same conventions without anything being copied.
