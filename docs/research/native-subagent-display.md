# Showing jobs as native subagents in Claude Code

Research date: 2026-10-14. Sources: the Claude Code docs, the mods API typings that Claude Code 2.1.293 writes for mod authors, the installed OpenAI Codex plugin (1.0.6, Apache-2.0), and transcripts of Codex plugin runs on Windows. No code was copied.

## The question

A job started with `subagent_start` shows up in the main session only as a generic MCP tool call. Native subagents get a row with a name and a short task, a color, an entry in the desktop app's task list, and a view of their own that you can open to watch what they do. We want our jobs to look the same, with a label that names the plugin, the provider (Claude Code, Codex or Antigravity), the model and the access mode, and we want the user to be able to click a job and follow the worker's steps and reasoning.

## How the Codex plugin does it

- The label comes from a plugin agent file, `agents/codex-rescue.md`, so the type is `codex:codex-rescue`. Its frontmatter sets `model: sonnet`, `tools: Bash` and two skills.
- The agent is a forwarder. Its rules allow exactly one Bash call to `codex-companion.mjs task ...`, and it returns that command's stdout unchanged. The companion drives `codex app-server` over JSON-RPC.
- In foreground mode the companion writes one stderr line per Codex event (`[codex] Running command: ...`, `[codex] Command completed ...`, `[codex] Assistant message captured: <96 characters>`) and prints Codex's final message on stdout. The full messages and reasoning summaries go only to a log file. The app-server client opts out of the streaming delta notifications, so nothing arrives token by token.
- In background mode the companion starts a detached worker and prints one line ("started in the background as task-..."). The forwarder returns that line and ends within seconds, so its view shows nothing of Codex's work. Nothing tells the main session when the job finishes; the user runs `/codex:status` or `/codex:result`. The SessionEnd hook kills the session's running jobs.
- Clicking a codex-rescue row in the desktop app shows the forwarder's skill loads, its own thinking, its Bash call with the prompt, and the Bash result. The only parts that come from Codex are the `[codex]` lines and the final answer, and they appear only when the Bash call ends.
- On Windows the plugin's read-only mode (`sandbox: "read-only"`, approval `never`) rejected every shell command in our runs, including `Get-Content` and `pwd`. Read-only Codex tasks could not read a single file. Our own Codex runtime uses `-s read-only` for `read` jobs, so it needs the same check on Windows.

Two other plugins follow the same pattern for Gemini: `gemini:gemini-agent` (bbingz/gemini-plugin-cc, MIT) and `gemini:gemini-rescue` (josephyaduvanshi/gemini-companion, Apache-2.0). Each makes one short Bash call that starts a background job and returns an ID.

## What Claude Code offers

Plugin agents (docs: sub-agents, plugins reference):

- A plugin agent's type is `<plugin>:<name>`. The transcript row reads `name(description)`, where the description is the few words the caller passes to the Agent tool when it starts the agent, not the frontmatter `description`.
- `name` can't contain `:`. Stick to lowercase letters, digits and hyphens.
- `color` (red, blue, green, yellow, purple, orange, pink, cyan) colors the row in the task list and the transcript. There is no display-name field.
- Plugin agents ignore `permissionMode`, `hooks`, `mcpServers` and `initialPrompt`. To call our MCP server they list `mcp__plugin_cli-subagents_cli-subagents__<tool>` in `tools`.
- A plugin can't ship permission allow rules, so a forwarder's Bash or MCP calls are checked against the user's own rules or auto mode.
- A background subagent's result reaches the main session as the engine's own completion notification. `SendMessage` resumes a finished subagent with its history.
- The Bash tool's ceiling is 10 minutes (`BASH_MAX_TIMEOUT_MS`). A foreground command that reaches its timeout moves to the background instead of being killed.
- A plugin's `settings.json` may ship `subagentStatusLine`, a script that replaces the text of each agent row in the terminal's agent panel. It receives the agent's model, type, name and description. Desktop support isn't documented.

Mods (docs: plugins/mods; typings, marked early access; terminal 2.1.287+, desktop 2.1.286+). A plugin lists modules in `hooks/hooks.json` as `{"modules": ["./register.tsx"]}`.

- `agent.spawn` hooks may rewrite an agent's `description` ("a rewrite is what the task shows as"), its `subagentType` (to an existing type only), `model`, `background` and `cwd`. `name` is fixed.
- `$.agent.register(spec)` adds an agent type at runtime with every field an agent file has. It must run once the session is bound (`session.start`), and the Agent tool can use the type from the next turn. One type per configured account or model is possible.
- `$.agent.list()` rows carry id, type, description, status, parent and name, but no model. A mod can't make the list show a model it chooses, except inside the description.
- `ui.render` can redraw `ToolUse`, `ToolResult` and `ToolGroup` rows on every surface, so our MCP tool rows can carry our label. `AbovePrompt` and `Pane` know which transcript is on screen, including one agent's view opened from the task list.
- There is no API for adding entries to the desktop's background task list. The "MCP task" entries there come from MCP's own task-augmented tool calls.
- `$.prompt.submit` queues a prompt that starts a turn once the session is idle, which could replace our PostToolUse `asyncRewake` hook. `$.session.append` adds text to the main loop or to a running subagent's loop.
- `turn.step` runs for every model request, including a subagent's, and a hook may answer it with chunks it builds itself (text, thinking, tool calls, stop) without calling the model. In principle a mod could drive one of our agents with no model at all and stream the worker's real steps into its view. The typings warn that steps of an agent a hook spawned itself skip that hook, and a step in progress is lost on reload.

## Options

| Option | What the user gets | Extra model cost | Risk |
|---|---|---|---|
| A. Forwarder agents (Codex pattern, improved) | Native row, color, task list entry, clickable view, engine completion notice | About 2 to 4 small Haiku requests per job | Low: documented plugin features |
| B. Mod that redraws our MCP tool rows and lists jobs above the prompt | Branded rows and a job list, no native agent entry | None | Medium: early-access API |
| C. Mod-driven agents (`turn.step` answers, no model) | Everything in A, plus the worker's own steps and reasoning in the agent's view | None | High: early-access API, edge cases on reload |

## Cost of the relay

Measured on 2026-10-15 for the worker only (`csa run --follow`, no relay): a Haiku 5.5 worker that listed the folders, read the first lines of the README and wrote three bullets took 7 seconds and 3 turns. It used 4 fresh input tokens, 22,687 cache-write tokens, 51,349 cache-read tokens and 701 output tokens (256 of them thinking), which Claude Code reported as $0.0054 at list price. On a subscription that counts toward the plan's limits instead of being billed.

The relay adds a Haiku loop on top. It doesn't think about the task. It reads its short instructions and the task, copies the task into one `csa run --follow` call (output tokens about the size of the task), reads the printed step lines (input tokens, mostly cache reads), and copies the result into its final message (output tokens about the size of the result), because Claude Code hands the main session only a subagent's last message. Estimate: 2 or 3 turns for a short job, roughly 15,000 to 30,000 input tokens and output about the size of the task plus the result, so a third to half a cent per job at the rates above. The relay's real numbers are still to be read from its transcript after the first desktop test.

The relay never sees the worker's own tokens, so its cost doesn't grow with them. A worker that uses a million tokens (most of them re-reads of its own context) might run 30 minutes, which is about 4 relay turns, roughly 30,000 to 100,000 mostly cached input tokens and 2,000 to 5,000 output tokens. That is a few percent of the worker's token count on a much cheaper model, well under 1% of the job's cost. Long reasoning text is printed in full and is the one part that can make it grow; shortening `thinking:` lines would cap it.

OpenAI's Codex plugin pays the same kind of copy cost: its `codex-rescue` relay copies the task in and Codex's answer out, and loads two skill files on every run. Its agent file sets Sonnet; starting it on Haiku (the model of the Agent call overrides the file) makes it cheaper.

## Switches (decided, not built yet)

No settings UI. Most people install through an agent and never open one, and a UI is one more thing to maintain. Instead:

- Defaults that need no changes: relay agents on in Claude Code; account failover on for Claude Code and Codex; rotation off for Antigravity (Google's terms), where it will be opt-in and marked "use at your own risk".
- One place for switches: `~/.cli-subagents/config.toml`, which already holds `failover` per runtime and `enabled` per account.
- A small `csa config` command to show and change them in one line (`csa config show`, `csa config set relay off`, `csa config set antigravity.failover on`), so a user can just tell their agent "turn off the Haiku relay".
- Relay off: the MCP server's instructions tell the main session to use the MCP tools instead, and `csa run --follow` refuses with a pointer to `subagent_start` if a relay is started anyway. Nothing in the user's Claude Code settings is edited. (Claude Code's own `permissions.deny` rule `Agent(cli-subagents:cli-subagents-claude)` also hides an agent type, for users who prefer that.)
- Later, only if people ask: show two or three of these switches as Claude Code plugin options (`userConfig`), which appear in the `/plugin` settings menu with no custom UI.

## Plan

1. Option A first (built 2026-10-15). Agent files `agents/cli-subagents-claude.md`, `cli-subagents-codex.md` and `cli-subagents-antigravity.md` (types `cli-subagents:cli-subagents-claude` and so on, named so the label still says CLI Subagents if the app hides the plugin prefix), on Haiku with low effort and no CLAUDE.md, orange, green and blue. The Agent call's description carries the model, mode and task, for example `Opus 5.5 · edit · fix the login test`.
2. Add `csa run --follow` (built 2026-10-15, with `csa follow <job> --from <offset>`; see `src/cli_subagents/follow.py`): start a job and print one readable line per worker event as it happens (tool calls, commands, file edits, messages and reasoning text in full), then the result. The parsers in `runtimes/` already read every event into `events.jsonl`. A worker question prints as a marked line and ends the command so the forwarder can hand it to the main session; the answer comes back through `SendMessage` and `csa message`.
3. Test on the desktop app what a running Bash call shows in the agent's view (live lines or only the end result), and whether the type shows with or without the `cli-subagents:` prefix.
4. Later, behind a setting: an `agent.spawn` mod that writes the label in one fixed format, then a prototype of option C.

## Sources

- Claude Code docs: sub-agents (frontmatter, foreground and background, resume), plugins reference (components, manifest `settings`), statusline (subagent status lines), MCP (plugin tool names), tools reference and environment variables (Bash timeout), plugins/mods overview, reference and API.
- Mods typings: `index.d.ts` written by Claude Code 2.1.293 into a mod's `.claude-plugin/types/claude-code/` folder (AgentSpawnInput, AgentSpec, AgentInfo, ui.render components, turn.step, session.append, prompt.submit).
- OpenAI Codex plugin 1.0.6 (github.com/openai/codex-plugin-cc, Apache-2.0): `agents/codex-rescue.md`, `commands/rescue.md`, `scripts/codex-companion.mjs`, `scripts/lib/codex.mjs`, `scripts/lib/tracked-jobs.mjs`, `scripts/lib/app-server.mjs`, `hooks/hooks.json`.
- github.com/bbingz/gemini-plugin-cc `plugins/gemini/agents/gemini-agent.md`; github.com/josephyaduvanshi/gemini-companion `plugins/gemini/agents/gemini-rescue.md`.
- desktop-statusline mod (github.com/centminmod/claude-plugins, MIT): reads `$.agent.list()` and draws running agents above the prompt in the desktop app.
