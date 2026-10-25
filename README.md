# Agents Router

Give your coding agent subagents that run in your other agent CLIs and on your other accounts, all with the same tools.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Status: early development](https://img.shields.io/badge/status-early%20development-orange.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

Your main session, in Claude Code, Codex or Antigravity, can start subagents. Each subagent is a real Claude Code, Codex or Antigravity session running in the background on one of your accounts. Every account uses the same MCP servers, skills, plugins and instructions you already set up, so nothing has to be configured twice. When an account runs out of quota, the work moves to the next one and carries on.

It installs as a plugin in Claude Code, Codex and Antigravity, and works as a plain MCP server in any other MCP client.

```mermaid
flowchart LR
    host["Your main session<br/>Claude Code · Codex · Antigravity"]
    core["Agents Router<br/>jobs · accounts · messages"]
    shared[("Your setup<br/>MCP servers · skills<br/>plugins · instructions")]
    w1["Claude Code<br/>account 2"]
    w2["Claude Code<br/>account 3"]
    w3["Codex<br/>account 1"]
    w4["Antigravity<br/>Gemini"]

    host -- "start · wait · message" --> core
    core --> w1 & w2 & w3 & w4
    shared -. "one shared pool" .-> w1 & w2 & w3
```

## What you get

- Subagents that feel built in. Start one, keep working, and get the result back when it's done. In Claude Code they appear in the agent list like native subagents, colored by CLI and labeled with the model and task, and you can open one to watch each step and its reasoning as the worker goes. `agt setup --host codex` also installs native Codex relay agents, with a separate thread for each job; see [host support](docs/hosts.md#codex) for the app verification still due. Claude Code's hooks also wake an idle session when a subagent finishes or asks a question, and list any uncollected results after you resume.
- Two-way messages. A subagent can send progress notes or ask a question and wait for the answer; you can send it follow-ups while it works, and each message is delivered once.
- One setup for every account. Your MCP servers, skills, plugins, agents, `CLAUDE.md`, project memory and settings are shared from your normal config folder to each extra account of that CLI through links and launch flags, not copied. Only the login differs per account.
- The same agents and skills in every CLI. `agt share` copies your agents and skills between Claude Code, Codex, Antigravity and Kiro, each in that CLI's own format, and keeps the copies in step when you edit one. Anything it replaces or removes is backed up first. See [Sharing agents and skills](docs/share.md).
- Automatic account switching. Claude Code and Codex report each account's 5-hour and weekly usage, and new work goes to the account with the most room. If a Claude, Codex or Gemini account hits its limit mid-task, the job continues on the next account (a Claude conversation moves with its transcript). Failover is on by default and can be turned off per CLI.
- Limits handled for you. Plan presets and extra caps are opt-in. By default there's no cap on jobs per account; set `max_jobs_per_account` to run at most that many workers per account and queue the rest. `usage_ceiling = 0.95` blocks new starts on accounts with known usage at or above 95%. Set a plan or extra caps to refuse starts that would go over them, with one line that says what to do instead.
- Only the user can authorize more parallel jobs with `override=<their words>` (relay or MCP) or `--override "..."` (`agt run` / `agt start`). Never set override on your own judgment; use it only when the user asked for more jobs in this conversation, or confirmed after you warned them and told them how much of the 5-hour window is left. It bypasses agt's caps and queueing; real usage-limit cooldowns, model and effort restrictions, login, depth and access still apply. See [configuration](docs/configuration.md#a-user-authorized-override).
- Optional delegation rules. Preview a template with model tiers with `agt rules`, then choose whether to install it. See [Delegation rules](docs/delegation.md).
- Mixing CLIs. A Claude Code session can hand a task to Codex or Gemini (through Antigravity). The subagent gets your Claude MCP servers and is told where your instructions and skills live.
- Safety checks. `read` jobs get read-only tools only, and `edit` jobs run in each CLI's own auto-review mode. Gemini workers, which have none on Windows, run behind a review gate of fixed rules plus a separate reviewer model, and `edit` jobs inside Antigravity's sandbox. Skipping permissions is an explicit choice per job.
- No tokens touched. Every model call comes from the vendor's own CLI with your own login. Nothing reads, copies or forwards credentials.
- Nothing to install besides Python 3.11. Pure standard library, with its own small MCP server.

## Install

1. Install the `agt` command with [uv](https://docs.astral.sh/uv/getting-started/installation/) (on a Mac, `brew install uv`):

   ```bash
   uv tool install --managed-python git+https://github.com/Watcharagorn/agents-router
   ```

   `--managed-python` gives `agt` a Python of its own, so a system Python upgrade can't break it. pipx works too: `pipx install git+https://github.com/Watcharagorn/agents-router`. Both need git. If `agt` isn't found afterwards, run `uv tool update-shell` (or `pipx ensurepath`) and open a new terminal.

2. Add the plugin to the CLI you work in:

   | Host | Command |
   |---|---|
   | Claude Code | `/plugin install agents-router --marketplace Watcharagorn/agents-router` in a session, or `claude plugin install agents-router --marketplace Watcharagorn/agents-router` in a terminal |
   | Codex | `agt setup --host codex` installs the plugin and native relay agents |
   | Antigravity | `agy plugin install https://github.com/Watcharagorn/agents-router` |
   | Any MCP client | Server command `agt` with argument `mcp` |

3. Optional: add your other accounts, each with the CLI's own login.

   ```bash
   agt accounts add max-2
   agt accounts add plus-2 --runtime codex
   agt doctor --live
   ```

`agt setup` can do step 2 for every installed CLI and register account folders that are already logged in; see [For AI agents](#for-ai-agents).

That's it. Ask your main session to use a subagent, or use `/agents-router:delegate <task>` in Claude Code. Details per host are in [docs/hosts.md](docs/hosts.md).

## For AI agents

To have your agent (Claude Code, Codex, Antigravity or another) install this for you, paste a prompt like this:

```text
Set up Agents Router from https://github.com/Watcharagorn/agents-router by following
the "For AI agents" section of its README. Then ask me what else I want, such as my
other account folders, sharing my agents and skills between my CLIs, or delegation
rules, and tell me exactly what I still have to do myself.
```

If you're the agent, do this. No slash commands are needed.

1. Install the `agt` command and check it with `agt --version`:

   ```bash
   uv tool install --managed-python git+https://github.com/Watcharagorn/agents-router
   ```

   Without uv, use `pipx install git+https://github.com/Watcharagorn/agents-router`. Both need git. If `agt` isn't on PATH afterwards, run `uv tool update-shell` (or `pipx ensurepath`); the user then opens a new terminal.

2. Run setup's dry run. Ask the user whether they have other account folders (a second Claude login in `~/.claude-2`, say), and add one `--account` per folder they name; folders that are already logged in need no new login:

   ```bash
   agt setup --dry-run --json --account max-2=~/.claude-2 --account codex:plus-2=~/.codex-2
   ```

   It returns one proposal: `plugin` (the hosts it installs into), `share` (the user's agents and skills, and which CLI each goes to), `repos` (repos with their own agents or skills), `switches` (worktrees, auto copy, tool widening, delegation rules, Codex's session line), `needs_answer` (items whose copies differ), `still_needed` and `command`.

3. Show the user the share table and the switches, and ask once: as suggested, change some, or the plugin only. With their answers, run the dry run again with them as flags (`--resolve <item>=<label>`, `--skip <item>@<label>`, `--only <item>=<label>[,<label>]`, `--projects <paths>|none`, `--rules on`, `--codex-session-line on`, or `--share off`). Then run the newest proposal's `command` exactly as given; it ends in `--yes --if-plan <id>`, which refuses if anything changed meanwhile.

   `agt setup` installs the plugin into each installed host with the host's own terminal commands, registers and syncs the account folders, turns sharing on, and ends with what the user still has to do. It skips anything already in place, so running it twice is safe. It never starts a login and never reads credential files. Use `--host claude`, `--host codex` or `--host antigravity` to limit it. In a terminal, a person runs plain `agt setup` and gets the same proposal as one screen: Enter does all of it. See [For agents](docs/share.md#for-agents) in the share guide.

4. Offer the optional [delegation rules](docs/delegation.md) template. It tells the main agent which models to use per tier, keeps work projects with the home vendor, and sets helper limits. Personal projects can try another CLI first and fall back to the home model when there is no room.

   Ask whether the user wants it and which home and try-first models they want for each tier. If they agree, preview and install with their choices; otherwise skip it. For example:

   ```bash
   agt rules --show --target claude --home-high "Opus 5.5" --try-low "Gemini 3.8 Flash high" --try-medium "Sol 6.1 high"
   agt rules --install --target claude --home-high "Opus 5.5" --try-low "Gemini 3.8 Flash high" --try-medium "Sol 6.1 high"
   ```

   Use the user's models and the host they want. Ask for work folders if the user wants to list them with `--work-folder`. Nothing installs these rules without `--install`.

5. Show the user the "Still needed from you" list from the output. Typical items: browser logins for accounts that aren't logged in (`agt accounts login <name>`), restarting the CLI (or `/reload-plugins` in Claude Code) so the plugin loads, and putting `agt` on PATH.

6. Run `agt doctor --runtime <runtime>` for each runtime you set up (`claude`, `codex`, `antigravity`; no model calls). Plain `agt doctor` checks Claude only. Adding `--live` runs one tiny job per account in that runtime and uses a little quota.

If `agt setup` fails for a host, these are the commands it runs, so you can run them yourself:

| Host | Commands |
|---|---|
| Claude Code | `claude plugin marketplace add Watcharagorn/agents-router` then `claude plugin install agents-router@agents-router` |
| Codex | `codex plugin marketplace add Watcharagorn/agents-router` then `codex plugin add agents-router@agents-router` |
| Antigravity | `agy plugin install https://github.com/Watcharagorn/agents-router` |

When the `claude` command can't be found (common with the Claude desktop app) or the plugin commands fail, `agt setup` declares the plugin in Claude Code's `settings.json` instead (`~/.claude/settings.json`, or the one in `$CLAUDE_CONFIG_DIR`). It keeps every other key and saves a backup next to the file. Claude Code installs the plugin on its next start, so the user has to restart it. `agt setup --claude-route settings` takes this route directly. By hand, merge this into the file:

```json
{
  "extraKnownMarketplaces": {
    "agents-router": { "source": { "source": "github", "repo": "Watcharagorn/agents-router" } }
  },
  "enabledPlugins": { "agents-router@agents-router": true }
}
```

To update later, run `agt update` (`--dry-run` shows the plan first). It reinstalls `agt` from GitHub with the tool that installed it (pipx, uv or pip) and updates the plugin in every host that has it, then lists the restarts. On Windows it prints the package command for you to run instead, because `agt` can't replace itself while running. By hand: `uv tool install --force git+https://github.com/Watcharagorn/agents-router` (or `pipx install --force` with the same URL), then `claude plugin marketplace update agents-router` and `claude plugin update agents-router@agents-router`, `codex plugin marketplace upgrade agents-router` and `codex plugin add agents-router@agents-router`, or `agy plugin install https://github.com/Watcharagorn/agents-router` again.

To register an account folder by hand: `agt accounts add <name> --home <folder> --no-login`. This defaults to Claude; add `--runtime codex` or `--runtime antigravity` for those accounts. For a new account, omit `--no-login` to open that runtime's login, which the user completes in the browser.

Never read or copy credential files, and never edit files inside `~/.agents-router/homes/`; `agt` manages those folders. Change agt's settings with `agt config set`, never by editing a limit or safety key by hand; raising a limit or loosening safety takes `--user-said "<the user's words>"` (see [configuration](docs/configuration.md#agt-config)). More for agents working in this repository is in [AGENTS.md](AGENTS.md).

## Status

| Subagent CLI | State | Guide |
|---|---|---|
| Claude Code (`claude -p`) | Works | [Claude Code](docs/claude-code.md) |
| Codex (`codex exec`) | Works | [Codex](docs/codex.md) |
| Antigravity (`agy`, Gemini) | Works; extra accounts at your own risk | [Gemini](docs/gemini.md) |
| Kiro (`kiro-cli chat --no-interactive`) | New: built from Kiro's docs, not yet run live; one account | [Kiro](docs/kiro.md) |
| Cursor, Gemini CLI, Copilot, OpenCode, others | Researched | [CLI comparison](docs/research/cli-permissions-and-headless.md) |

Tested live on Windows with Claude Code 2.1.299, Codex 0.162.1 and Antigravity 1.4.0; the test suite also runs on macOS and Linux in CI.

## How the shared setup works

Your normal config folder (`~/.claude`, `~/.codex`) is the one pool. Each extra account gets its own folder that holds only its login, state and session history. Before every run Agents Router points that folder at the pool:

- folders such as `agents/` and `commands/` become one link to yours, and each skill is linked by name;
- `CLAUDE.md` and `AGENTS.md` are hard links to your files, so it's the same file on disk;
- project memory is linked to your main project memory;
- your settings, MCP servers and plugins are passed to the worker as launch flags (`--settings`, `--mcp-config`, `--plugin-dir`), minus anything that would pick a different login;
- for Codex, your `config.toml` is written into the account folder, which keeps its own trust and notice tables.

Sharing between accounts never moves or rewrites anything in your own folders. Only `agt share`, once you turn it on, writes into them: the copies of your agents and skills, with a backup of anything it replaces. One limit: tools built into the Claude desktop app itself (its browser or terminal panes, for example) belong to the app, not to your config, so CLI workers can't have them. Everything in your config folders and plugins is shared. `agt doctor --live` starts a tiny job on each account and compares exactly what each one loaded.

## Using your accounts

All of this runs through each vendor's official CLI with your own login: the same thing you'd get by opening a terminal and typing the task yourself. Anthropic documents running several accounts side by side with separate config folders, and OpenAI ships its own plugin that runs Codex from Claude Code. Google's Antigravity FAQ rules out wrapping `agy` on consumer plans and automating account rotation, so the Antigravity runtime uses your one signed-in account unless you add more yourself. Extra accounts are marked use at your own risk; once added, they're used the same way as for Claude and Codex, and `failover = "off"` turns switching off. The details and quotes are in [provider terms](docs/research/provider-terms.md); each vendor's terms apply to your accounts.

## Repository layout

```text
src/agents_router/      Core: jobs, account pool, shared setup, messages, settings, MCP server, agt CLI
  runtimes/             Claude Code, Codex and Antigravity adapters
  share/                agt share: agents and skills across the CLIs
  agy_review/           The review gate for Antigravity workers
.claude-plugin/         Claude Code and Codex plugin manifest and marketplace
agents/                 Relay agents that show jobs in Claude Code's agent list
src/agents_router/templates/codex-agents/   Native Codex relay definitions installed by setup/update
plugin.json, mcp_config.json   Antigravity plugin
skills/agents-router/   The skill that teaches the host agent how to use subagents
commands/               /agents-router:delegate
tests/                  Unit and end-to-end tests with fake CLIs
adapters/antigravity/   The original standalone AGY server (superseded)
docs/                   Guides, design and research
```

## Documentation

| Document | Contents |
|---|---|
| [Hosts](docs/hosts.md) | Install and use from Claude Code, Codex, Antigravity and other MCP clients |
| [Claude Code](docs/claude-code.md) | Accounts, what is shared, switching, troubleshooting |
| [Codex](docs/codex.md) and [Gemini](docs/gemini.md) | The other subagent CLIs |
| [Sharing agents and skills](docs/share.md) | `agt share`: the same agents and skills in Claude Code, Codex, Antigravity and Kiro |
| [Configuration](docs/configuration.md) | Settings files per workspace, `agt config`, the log, files, environment |
| [Delegation rules](docs/delegation.md) | Optional model tiers, work folders and helper limits |
| [Design](docs/design.md) | Architecture, decisions and what was verified |
| [Research](docs/research/) | CLI comparison, prior art, multi-account tools, provider terms |

## Contributing

Issues and pull requests are welcome. Run the tests before sending a change:

```bash
python -m unittest discover -s tests -t tests
```

Set `PYTHONPATH` to `src` (or install with `pip install -e .`) first. New runtime adapters should keep the vendor's binary unmodified, leave authentication to it, and default to its safer permission mode.

## License and credit

Agents Router is by [Watcharagorn](https://github.com/Watcharagorn). Based on [CLI Subagents](https://github.com/lunati228/CLI-Subagents) by [lunati](https://github.com/lunati228). Licensed under [Apache-2.0](LICENSE). If you redistribute it or build on it, keep the [NOTICE](NOTICE) file. To cite the project, use [CITATION.cff](CITATION.cff).
