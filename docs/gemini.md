# Gemini through Antigravity CLI

CLI Subagents runs Gemini (and the other models Antigravity offers) through Google's Antigravity CLI, `agy`, with `runtime: "antigravity"`.

Check Google's terms first. The [Antigravity FAQ](https://antigravity.google/docs/faq#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching) says consumer plans don't support wrapping `agy` inside third-party coding agents or automating account rotation, and points that use to a Gemini Enterprise or AI Studio API key. Use this runtime with an account route Google allows. Details are in [provider terms](research/provider-terms.md).

## Setup

1. Install Antigravity CLI from [Google's documentation](https://www.antigravity.google/docs/cli/) and run `agy` once to sign in.
2. Install `csa` (see the [Claude Code guide](claude-code.md#1-install)).
3. Check: `csa accounts --runtime antigravity` lists the binary, its version and whether it can list models.

There is nothing else to configure. Jobs run on the account you signed in with in `agy`. Extra Google accounts are optional and off by default; see [More than one Google account](#more-than-one-google-account).

## Using it

```bash
csa run --runtime antigravity --model gemini-3.8-flash-high --access read "Review src/auth.py for bugs"
```

From a host, pass `runtime: "antigravity"` to `subagent_start`. `agy models` lists the model IDs your account can use.

| `access` | Antigravity mode |
|---|---|
| `read` | `--mode plan` |
| `edit` | `--mode accept-edits`: file edits allowed; shell commands are refused in headless runs |
| `full` | `accept-edits` plus `--dangerously-skip-permissions` |

Antigravity's own auto mode (a reviewer agent for commands) isn't available on every account or on Windows. Where your account has it, set `edit_permission_mode = "auto"` under `[antigravity]` in the config; AGY falls back to its default mode with a warning when it isn't available.

The prompt travels as a command-line argument, so it's limited to 24,000 characters. Point to files instead of pasting them. The job's `timeout_seconds` is passed to `agy` as `--print-timeout`.

## Messages

Workers on an extra account get `message_parent`, `ask_parent` and `check_parent_messages` from a `csa-parent` MCP server, which CLI Subagents writes into that account's `config/mcp_config.json` with the job's id before each job. Workers on your primary account don't get these tools: `agy` has no flag for an MCP server added for one run, and your own `~/.gemini/config/mcp_config.json` is left alone. Extra accounts with `max_jobs_per_account` above 1 don't get them either, because two jobs could then start from the same file.

Follow-ups still reach an Antigravity worker. A `subagent_message` sent while the job runs is given to the worker by the runner as a follow-up turn once its current turn ends. A `subagent_message` sent to a finished job continues the same conversation as a new job.

## More than one Google account

This is opt-in and at your own risk. Google's [Antigravity FAQ](https://antigravity.google/docs/faq#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching) says consumer plans don't support automating account switching. Nothing changes until you add an account, and jobs only move between accounts after you turn failover on.

### How it works

`agy` has no setting for its config folder, and it keeps its login in the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service on Linux), where every login of your user would share it. So each extra account gets its own folder under `~/.cli-subagents/homes/antigravity/`, and `agy` runs with `HOME` and `USERPROFILE` pointing there (on macOS and Linux the XDG folders too). CLI Subagents also sets `SSH_CONNECTION`, `SSH_CLIENT` and `SSH_TTY`. When `agy` thinks it runs over SSH, it stores its login in a file in that folder instead of the keyring, so each folder keeps its own login. Your primary account isn't touched: its jobs run with your normal environment and keyring login.

This relies on `agy` behaviour that Google doesn't document. If a later `agy` stops honouring it, the login won't stick in the folder: `csa doctor --runtime antigravity` then shows the account as not logged in, and its jobs end with the outcome `auth` and the login command to run.

Your setup in `~/.gemini` is linked into each account folder, so there is still one copy of it:

| What | How it gets into the account folder |
|---|---|
| `GEMINI.md`, `AGENTS.md`, `config/GEMINI.md`, `config/AGENTS.md`, `config/hooks.json` | hard links |
| `config/rules/`, `antigravity-cli/rules/` | one folder link each |
| `config/skills/`, `antigravity-cli/skills/`, `antigravity-cli/plugins/` | one link per entry; the cli-subagents plugin is left out so workers can't start subagents |
| `config/config.json`, `antigravity-cli/settings.json` | hard links, unless they set `enableAdc` or `modelProvider`, which can select a login of their own (you get a warning instead) |
| `config/mcp_config.json` | written before each job: your MCP servers (without cli-subagents) plus `csa-parent` |

Each account keeps its own login, conversations, project settings (`config/projects/`) and MCP OAuth tokens. Every job refreshes the links; `csa accounts sync NAME --runtime antigravity` does it by hand.

### Steps

1. Run `csa accounts add NAME --runtime antigravity`. It creates the folder, links your setup, prints the risk note and starts `agy` in that account's environment.
2. `agy` prints a Google sign-in link. Open it, sign in with the other account, paste the code back into `agy`, then exit `agy`. Odd characters may show up in the prompt because `agy` queries the terminal when it thinks it runs over SSH; ignore them. To sign in later or again, run `csa accounts login NAME --runtime antigravity`.
3. Run `csa doctor --runtime antigravity`. It lists each account with its folder, whether a login file exists, whether it's enabled, and any cooldown.
4. Pin a job to the account with `account: "NAME"` (`--account NAME` on the command line). To rotate between accounts, set `failover = "on-limit"` under `[antigravity]` in the config.

With `failover = "off"`, the default, jobs without `account` run on your primary account as before. With `"on-limit"`, a job goes to the least recently used account that isn't cooling down, the primary included (`include_primary = false` leaves it out). When an account runs out of quota, it cools down and the job starts again on the next account with the partial output attached. A conversation can't move between account folders, so the next account starts a new one. In rotation an account without a login file is skipped; a job pinned to it ends at once with `auth`.

### Limits

- `agy` reports no usage figures, so rotation picks the least recently used account, not the one with the most quota left.
- MCP servers and hooks run with the account folder as their home folder, so whatever they keep under `~` (caches, their own logins) is separate for each account.
- The quota error wording of `agy` is matched loosely ("quota", "resource exhausted", "rate limit", "429"). If a limit isn't recognised, the job ends with an error instead of moving on.
- `csa launch --runtime antigravity --account NAME` opens an interactive session on an extra account; it doesn't take a job slot.

## The standalone adapter

[`adapters/antigravity/`](../adapters/antigravity/) is the original Windows MCP server for AGY. It runs `agy` through ConPTY (older versions printed nothing to a pipe) and adds its own command reviewer, so headless runs can execute shell commands after a separate model approves each one. Use it if you need reviewed shell commands on Windows; otherwise the core runtime is simpler and needs no setup.
