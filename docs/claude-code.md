# Claude Code: setup and use

This guide sets up CLI Subagents so a Claude Code session can hand work to `claude -p` workers running under your other Claude accounts, with the same MCP servers, plugins, skills and settings on every account.

Every worker is the official `claude` CLI with your own login, the same as opening a terminal yourself. Anthropic documents running several accounts with separate config folders; see [provider terms](research/provider-terms.md) for the sources.

## Requirements

- Claude Code installed (`claude` on PATH, or the copy bundled with the Claude desktop app, which is found automatically)
- Python 3.11 or newer
- One Claude subscription per extra account you want to use

## 1. Install

The package has no dependencies. Install it with any of:

```bash
pipx install git+https://github.com/lunati228/CLI-Subagents
```

```bash
uv tool install git+https://github.com/lunati228/CLI-Subagents
```

```bash
pip install --user git+https://github.com/lunati228/CLI-Subagents
```

This gives you the `csa` command. Check it with `csa --version`. If the command isn't found, add your Python scripts folder to PATH (`pipx ensurepath` does this for pipx).

Then add the plugin in Claude Code (Codex and Antigravity hosts: see [hosts](hosts.md)):

```text
/plugin install cli-subagents --marketplace lunati228/CLI-Subagents
```

On older Claude Code versions, run `/plugin marketplace add lunati228/CLI-Subagents` and then `/plugin install cli-subagents@cli-subagents`. The plugin brings the MCP server, the `cli-subagents` skill and the `/cli-subagents:delegate` command.

Without the plugin (or in another MCP host), register the server yourself with the command `csa` and the argument `mcp`, for example in a project `.mcp.json`:

```json
{ "mcpServers": { "cli-subagents": { "command": "csa", "args": ["mcp"] } } }
```

## 2. Add your other accounts

Your normal Claude Code folder (`~/.claude`, or `CLAUDE_CONFIG_DIR` if you set one) is the primary setup. It is the single place where you manage MCP servers, plugins, skills, agents, `CLAUDE.md` and settings.

For each extra account:

```bash
csa accounts add max-2
```

This creates `~/.cli-subagents/homes/claude/max-2`, links in your shared setup, and opens Claude Code's own login. Sign in with the account that profile should use. If you belong to several organizations, switch to the right one in the browser before you click Authorize; the login uses whichever organization is active there.

The tool never reads or copies the login. Each account folder keeps its own credentials, state and session history.

## 3. Check

```bash
csa accounts
csa doctor --live
```

`csa accounts` shows each account's login state, plan, last known usage and cooldown without using quota. `doctor --live` runs one tiny Haiku job per account and prints what each one loaded (model, permission mode, MCP servers, plugins, skills, agents), then lists any differences between accounts.

## 4. Use it

In Claude Code, ask for delegation in plain words ("have a subagent review the auth module on another account"), use `/cli-subagents:delegate <task>`, or let the skill decide. The tools are:

| Tool | Purpose |
|---|---|
| `subagent_start` | Start a job. Returns a job ID, or the result if it finishes within `wait_seconds` |
| `subagent_wait` | Wait for any of several jobs to finish or send a message; moves to the background in Claude Code and notifies you |
| `subagent_message` | Answer a subagent's question, send a follow-up while it runs, or continue a finished job |
| `subagent_status` | Progress, with an efficient wait |
| `subagent_result` | Final answer, outcome, account, session, cost |
| `subagent_cancel` | Stop a job and everything it started |
| `subagent_list` | Recent jobs |
| `subagent_accounts` | Accounts, logins, usage and cooldowns |

From a terminal, the same jobs are available through `csa run`, `csa start`, `csa status`, `csa result`, `csa cancel` and `csa jobs`.

`access` controls permissions. `read` runs the worker in plan mode (no edits), `edit` uses Claude Code's auto mode (edits allowed, with Claude's own classifier checking risky actions), and `full` skips permission checks and is only for when you ask for it.

## What every account shares

| From the primary setup | How it reaches each account |
|---|---|
| `agents/`, `commands/`, `output-styles/`, `rules/` | One link to your folder (junction on Windows, symlink elsewhere) |
| Skills | One link per skill; the account's own `skills/synced` stays its own |
| `CLAUDE.md` | A hard link: the same file on disk, not a copy |
| Project memory (`projects/<project>/memory`) | Linked to your memory for that project |
| `settings.json` (hooks, permissions, env, model defaults) | Passed with `--settings`, minus login-related keys |
| User and local-scope MCP servers | Passed with `--mcp-config` |
| Installed, enabled plugins | Passed with `--plugin-dir` |
| Project `.mcp.json`, `.claude/` and `CLAUDE.md` | Already shared, because they live in the repository |

Links are tracked in `.cli-subagents-managed.json` inside each account folder. Copies are only a fallback (a hard link across drives, a link the OS refuses). If an account already has its own file or folder with the same name, it is left alone and reported as a conflict. Because these are links, anything a worker adds there (a new agent, a memory note) lands in your one shared setup.

One more per-account detail: Claude Code skips a project's own `permissions.allow` rules in `-p` runs when that account never trusted the folder. Put permission rules you need in your user `settings.json`, which every account gets.

Two things stay per account because they belong to the account on Anthropic's side:

- claude.ai connectors (the integrations you add on claude.ai). Each account shows its own. To make every account identical, set `connectors = "off"` in the config, and add the services you need as regular MCP servers in the primary setup.
- OAuth logins for remote MCP servers. If a server needs a browser login, run `csa launch --account <name>` once and complete it with `/mcp`.

## Choosing accounts and failover

With no `account` given, a job goes to the account with the lowest recent usage that has a free slot. Usage comes from the `rate_limit_event` messages Claude Code reports during each run (5-hour and weekly utilization, plus reset times).

When an account reaches its limit, it cools down until the reported reset time and later jobs skip it. A job that was running when the limit hit continues on the next account: CLI Subagents copies the session transcript into that account's folder and resumes the same conversation there with a short note, so the subagent picks up where it stopped. (Checked with Claude Code 2.1.295: a transcript copied to another account folder resumes with its full history.) If the transcript can't be moved, the job restarts with the original task and the partial output.

To stop at the limit instead, set:

```toml
[claude]
failover = "off"
```

## Interactive sessions with the shared setup

```bash
csa launch
csa launch --account max-3 -- --model opus
```

`csa launch` opens a normal interactive Claude Code session on the account with the most room left (or the one you name), with the same shared setup. Anything after `--` goes to `claude`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `csa` not found by the plugin | Install the package (step 1) and make sure its scripts folder is on PATH, then restart Claude Code |
| `auth` outcome | Run `csa accounts login <name>` |
| An account loads different tools | Run `csa doctor --live` and read the differences; `csa accounts sync <name>` re-links the shared setup |
| Wrong organization after login | Log out with `claude auth logout` in that account (`CLAUDE_CONFIG_DIR` set to its folder), switch organization on claude.ai, log in again |
| `no_account` | Every account is cooling down or busy; `csa accounts` shows when the earliest resets |
| Job state `lost` | The background runner stopped (for example the computer slept). Start the job again |
| Need to see everything a worker did | `events_path` in the result points to the raw event log |
