# Installing CLI Subagents through an agent

Research date: 2026-10-14. All pages were fetched on that date and treated as data. Nothing fetched was run. Where a fetch tool summarised a page, the claim is marked as summarised, so re-check it against the page before quoting it.

## The problem

A user asked their Claude Code agent to "integrate this repo into my setup". The agent said parts had to be done by hand. A sandboxed test showed that Claude Code in auto mode installs the Python package and registers accounts, but does not run `/plugin install`. It stops at the plugin step. It seems not to know the terminal equivalents. Browser logins always need the user.

## What the official docs say

Claude Code docs, "Install and manage plugins" and "Plugin commands reference":

- Plugins can be installed from a shell with no session. `claude plugin marketplace add <owner/repo>` and `claude plugin install <plugin>@<marketplace>` both exist, and `claude plugin list` shows the result. The docs describe this as the route for setup scripts.
- One command can do both steps: `claude plugin install cli-subagents --marketplace lunati228/CLI-Subagents`. It needs Claude Code 2.1.292 or later. In a session the same flag works from 2.1.275, but the session form opens a details panel and asks for a scope, so an agent cannot finish it.
- The default scope is user. Plugins installed from a shell load the next time Claude Code starts, or after `/reload-plugins` in an open session.
- `/plugin` does not run in `claude -p`; Claude replies that it is not available. So a worker or sandbox running `claude -p` can never run `/plugin install`. This matches the sandbox test.
- Marketplaces added from the shell are added without a confirmation step. Organisation policy checks still apply.
- Some plugins install by running a command named in their marketplace entry. Claude Code asks you to accept it. `--yes` skips that prompt, but the docs say `--yes` is ignored when the command runs inside a Claude Code session, for example from the Bash tool. CLI Subagents does not use a command source, so this does not apply.
- Claude Code adds Anthropic's official marketplace itself on the first interactive start. Other marketplaces must be added.

Claude Code docs, MCP page:

- "Claude Code doesn't install servers on its own." You add them with `claude mcp add` or `add-json`. The page also tells the reader how to translate a launch command or an `mcpServers` JSON block into a `claude mcp add` command. Scope options are local (default), project and user.
- Servers in `.mcp.json` need approval in interactive sessions. In `claude -p` they load without asking.

Claude Code docs, permission modes (auto mode):

- Auto mode uses a classifier. The default block list includes "downloading and executing code, like `curl | bash`", launching an agent loop with `--dangerously-skip-permissions`, and sending keystrokes to Claude Code's own tmux pane to drive its own interface (treated as Claude changing its own permissions).
- The default allow list includes installing dependencies declared in lock files or manifests, local file operations in the working directory, and read-only HTTP.
- The classifier trusts the working directory and the remotes configured when the session started. Everything else counts as external.
- Plugin installs are not named in either list. I found no document or issue that says `claude plugin install` is blocked or allowed. A plugin install pulls code and hooks from an external repo and writes to `~/.claude/settings.json`, so it sits in a grey zone. The classifier considers what the user asked for. A web search turned up the same advice: an explicit instruction naming the exact command is more likely to pass than a vague "set it up". That comes from a secondary source, not the official page, so treat it as likely but untested.
- Local test needed: run `claude plugin marketplace add` and `claude plugin install` from an auto-mode session and note whether the classifier allows, asks or blocks. This research could not do that because `claude` is not installed on this machine's Git Bash path.

Local help output (this machine): `codex plugin` has `add`, `list`, `marketplace` (add, list, upgrade, remove) and `remove`, and `codex plugin marketplace add` takes a local path, `owner/repo[@ref]`, an HTTPS Git URL or an SSH Git URL. `agy` is not on PATH here, so the Antigravity commands are taken from the existing README, not checked.

## Other projects

| Project | How it installs | Manual steps for the user | Addresses an AI agent doing the install? |
|---|---|---|---|
| openai/codex-plugin-cc | Three slash commands inside Claude Code: `/plugin marketplace add openai/codex-plugin-cc`, `/plugin install codex@openai-codex`, `/reload-plugins`, then `/codex:setup` | Typing the slash commands; `!codex login` or API key; `/codex:setup` can offer `npm install -g @openai/codex` | No (summarised) |
| SinanTufekci/agent-intern | Shell commands: `claude plugin marketplace add SinanTufekci/agent-intern` then `claude plugin install agent-intern@agent-intern`; or MCP only with `claude mcp add -s user agent-intern -- uvx agent-intern` | Install and log in to at least one backend CLI; restart Claude Code; uv required | No explicit agent text, but every command is a terminal command, so an agent can run them (summarised) |
| BeehiveInnovations/pal-mcp-server | `git clone` plus `./run-server.sh` (auto-configures Claude Desktop, Claude Code, Gemini CLI, Codex CLI, Qwen CLI); or a `uvx --from git+...` block pasted into `.mcp.json` | Put API keys in `.env` or the config; each CLI's own login | No (summarised) |
| mkXultra/ai-cli-mcp | MCP config with `npx -y ai-cli-mcp@latest`, or `claude mcp add ai-cli '{...json...}'` | Install each CLI; run `claude --dangerously-skip-permissions` once by hand to log in and accept terms; `codex login`; `agy` first sign-in | No (summarised) |
| alessai/Hive-MCP | `claude mcp add hive -- npx @alessai/hive-mcp` | Install the CLIs you want; their credentials | No (summarised) |
| uwuclxdy/clauth | `cargo install clauth` or `curl -fsSL .../install.sh \| bash`; plugin through the TUI or `/plugin marketplace add uwuclxdy/clauth` + `/plugin install clauth@clauth` | `clauth capture <name>` after logging in to each account; binary must be on PATH for the plugin | No (summarised) |
| steipete/claude-code-mcp | MCP config with `npx -y @steipete/claude-code-mcp@latest` | Node 20+, Claude CLI, run `claude --dangerously-skip-permissions` once manually | No (summarised) |
| jamubc/gemini-mcp-tool | `claude mcp add gemini-cli -- npx -y gemini-mcp-tool` (Windows needs a different flag form) | Install `agy` with a curl script; run `agy` once to sign in | No (summarised) |
| shinpr/sub-agents-mcp | MCP config JSON with `npx -y sub-agents-mcp` and two env vars; no `claude mcp add` line | Create an agents folder with markdown files; edit the client config by hand; Node 22; CLI login | No (summarised) |
| obra/superpowers | Per-host list. Claude Code: `/plugin install superpowers@claude-plugins-official`, or marketplace add then install. Antigravity: `agy plugin install <url>`. Codex CLI: `/plugins` menu. Also commands for Gemini, Copilot, Droid, Qwen, Pi and others. OpenCode: tell the agent "Fetch and follow instructions from <raw INSTALL.md URL>" | Slash commands for Claude Code and Codex; restart sessions | Partly. The OpenCode entry is a paste-to-your-agent prompt backed by an agent-written INSTALL.md with numbered steps, a verify step and a troubleshooting section |
| wshobson/agents | Claude Code slash commands; Codex shell commands `codex plugin marketplace add` and `codex plugin add`; `gh skill` and `npx skills add` for skills only; `make install-*` from a clone | Slash commands for Claude Code | No |
| thedotmack/claude-mem | `npx claude-mem install` (one command, interactive browser sign-in unless a flag or env var skips it); or slash commands | Browser sign-in unless skipped; restart | No |
| anthropics/claude-plugins-official | `/plugin install <name>@claude-plugins-official` only; the page documents no shell form | Run in a session | No (the docs site documents the shell form) |

Cloud-hosted MCP directories (Glama, Cline) show a further pattern: an `llms-install.md` file in the repo, written as step-by-step instructions for an agent, with a "please install this MCP server using this link" prompt. Cline reads it. Several community copies had placeholder paths, so quality varies, and I found no formal spec.

None of the nine named projects has an AGENTS.md, llms.txt or "paste this to your agent" section for installation. I could not confirm absence of files the fetch summaries did not list, so check each repo's file list before saying "none" in public text.

## Answers

### 1. Is the friction universal?

Mostly yes for the plugin step, and it comes from README wording more than from the tools. Projects that give only slash commands (codex-plugin-cc, the official marketplace page, claude-mem's alternative path, wshobson for Claude Code) leave the agent stuck, because a slash command is not a shell command and `/plugin` does not run in `claude -p`.

Projects avoid it in four ways:

- They ship plain MCP servers launched by `npx` or `uvx`, installed with one `claude mcp add ... -- npx ...` line (Hive-MCP, jamubc, ai-cli-mcp, agent-intern's MCP route). No plugin step exists, so nothing is stuck. The cost is no skill, no hooks and no slash command.
- They print the shell form next to the slash form (agent-intern uses `claude plugin ...`; wshobson uses `codex plugin ...`).
- They provide a one-command installer (`npx claude-mem install`, pal's `run-server.sh`, clauth's curl script).
- superpowers gives agents an INSTALL.md to fetch and follow, at least for OpenCode.

Everyone shares the login step. Every project in the list leaves CLI login to the user, and several add a one-time manual `claude --dangerously-skip-permissions` run to accept terms. That one cannot be automated and the other projects do not try.

### 2. What CLI Subagents should add

In order of value:

1. Put the shell commands in the README and in `docs/hosts.md`, above or beside the slash command. For Claude Code: `claude plugin install cli-subagents --marketplace lunati228/CLI-Subagents` (2.1.292+) or the two-step form for older versions: `claude plugin marketplace add lunati228/CLI-Subagents` then `claude plugin install cli-subagents@cli-subagents`. Add `claude plugin list` as the check and a note that a running session needs `/reload-plugins` or a restart. The Codex and Antigravity commands are already terminal commands; keep them.
2. Add an "Install with your agent" README section with a short copy-paste prompt. It should name the exact commands, so the auto-mode classifier sees the user asking for those exact actions. The prompt should say: install `csa` with pipx, run `csa setup`, register extra accounts, run `csa doctor`, and stop and tell me when a browser login is needed.
3. Add `csa setup` (or `csa install-plugin`). It detects which of `claude`, `codex` and `agy` are on PATH, runs the matching plugin commands for each, falls back to `claude mcp add` / `codex mcp add` / `agy` equivalents when the plugin route fails, imports existing account folders (for example `~/.claude-*`), runs `csa doctor`, and prints a list of what is left for the user (logins). It should print each command before running it, support `--dry-run` and `--host`, and stay idempotent. Reasons to prefer a script over a prompt: it works the same in every host, and the classifier evaluates one command, not a chain.
4. Add `AGENTS.md` (read by Codex and others) and a `CLAUDE.md` pointer, and an `llms-install.md` for Cline-style agents, all short and pointing to the same steps. Do not rely on llms.txt alone; no evidence any of the CLIs fetches it on its own.
5. Add a minimal MCP-only path as the fallback: `claude mcp add --scope user cli-subagents -- csa mcp` and `codex mcp add cli-subagents -- csa mcp`. It skips the plugin entirely, so the agent can finish even when the plugin step is blocked. State what is lost: the skill, the slash command and the wake-up hooks.
6. Avoid `curl | bash` as the install route: auto mode blocks it by default.

### 3. What Claude Code will do on its own, with one approval, or never

These are based on the docs; the auto-mode column for plugin commands is not confirmed by a test.

| Step | Auto mode | Notes |
|---|---|---|
| `pipx install git+https://github.com/...` | Observed to run in the sandbox test | Installing from a user-named repo; the user supplied the link |
| `csa accounts add`, `csa doctor`, other local `csa` commands | Runs | Local files, no external effect |
| `claude plugin marketplace add` and `claude plugin install` from the Bash tool | Unknown, probably allowed or one approval | Not in the block list or the allow list. The user naming the repo and the commands makes it more likely to pass. Needs a local test. If it stops, one approval click should be enough |
| `codex plugin ...`, `agy plugin install <url>` from Bash | Same as above | Same classifier, same uncertainty |
| `claude mcp add` | Probably runs or needs one approval | Writes to `~/.claude.json`; a project-scoped `.mcp.json` entry additionally needs approval in the next interactive session |
| `/plugin install ...`, `/reload-plugins`, any slash command | Not possible for the agent | Slash commands are typed by the user; `/plugin` does not run in `claude -p` or inside a worker; sending keystrokes to Claude's own pane is blocked |
| Browser logins (`claude` login, `codex login`, `agy` first sign-in) | Impossible | Need the user in a browser. The agent can start the command and tell the user |
| Accepting the terms prompt on first run | Impossible | Interactive, one time |
| Restarting the session after a plugin install | User | Or `/reload-plugins` typed by the user |

The user must also stay in control of one thing: `--dangerously-skip-permissions` anywhere in the install flow would be blocked by the classifier and is a bad idea; CLI Subagents should not ask for it.

## Gaps and things to test

- Run `claude plugin marketplace add` and `claude plugin install` from an auto-mode session and a manual session on a clean profile. Record the outcome and the exact classifier message if blocked.
- Check that `claude plugin install cli-subagents --marketplace lunati228/CLI-Subagents` works on 2.1.292+ and that the two-step form works on older versions.
- Check whether Codex's plugin commands and `agy plugin install` are accepted by their own sandboxes when run by a Codex or Antigravity agent.
- The summaries of the nine third-party READMEs came from a fetch tool that paraphrases. Re-read any claim you intend to quote.

## Sources

- Claude Code, Install and manage plugins: https://code.claude.com/docs/en/discover-plugins
- Claude Code, Plugin commands reference: https://code.claude.com/docs/en/plugins/cli-reference
- Claude Code, MCP: https://code.claude.com/docs/en/mcp
- Claude Code, Permission modes (auto mode): https://code.claude.com/docs/en/permission-modes
- Claude Code, Configure auto mode: https://code.claude.com/docs/en/auto-mode-config
- https://github.com/openai/codex-plugin-cc
- https://github.com/SinanTufekci/agent-intern
- https://github.com/BeehiveInnovations/pal-mcp-server
- https://github.com/mkXultra/ai-cli-mcp
- https://github.com/alessai/Hive-MCP
- https://github.com/uwuclxdy/clauth
- https://github.com/steipete/claude-code-mcp
- https://github.com/jamubc/gemini-mcp-tool
- https://github.com/shinpr/sub-agents-mcp
- https://github.com/obra/superpowers and https://raw.githubusercontent.com/obra/superpowers/refs/heads/main/.opencode/INSTALL.md
- https://github.com/wshobson/agents
- https://github.com/thedotmack/claude-mem
- https://github.com/anthropics/claude-plugins-official
- Glama llms-install.md examples, found by web search: https://glama.ai/mcp/servers/@mufeedvh/code2prompt/blob/0250ffaebc86c8db547db682c6b4ceed66425382/llms-install.md
- Secondary source on auto-mode classifier intent: https://code.claude.com/docs/en/auto-mode-config (search summary only)
- Local: `codex plugin --help`, `codex plugin marketplace add --help` (Codex CLI on this machine, 2026-10-14)
