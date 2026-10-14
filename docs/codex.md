# Codex: setup and use

CLI Subagents can run `codex exec` workers the same way it runs `claude -p` workers: in the background, on one of your accounts, with one shared setup. The host can be Claude Code (through the plugin) or any MCP host, Codex included.

Read [provider terms](research/provider-terms.md) first. OpenAI's terms bar circumventing rate limits, so failover between Codex accounts is off by default and stays your decision.

## Requirements

- Codex CLI installed and signed in (`codex login status`)
- The `csa` package installed (see the [Claude Code guide](claude-code.md#1-install))

With no extra Codex accounts configured, workers run on your normal Codex login (`~/.codex`, or `CODEX_HOME`).

## Extra accounts

```bash
csa accounts add plus-2 --runtime codex
```

This creates `~/.cli-subagents/homes/codex/plus-2`, copies in your shared setup, and opens `codex login` for that folder. Every command CLI Subagents runs for an extra account passes `cli_auth_credentials_store=file`, so each login lives in that account's own `auth.json` and never in a shared keyring entry. The tool never reads it.

Check everything with:

```bash
csa accounts --runtime codex
csa doctor --runtime codex
```

## What every account shares

| From the primary `CODEX_HOME` | How it reaches each account |
|---|---|
| `config.toml` (MCP servers, model defaults, features, profiles) | Written into the account folder before every run, without this tool's own MCP server |
| `skills/`, `plugins/` entries | Linked (junctions on Windows) |
| `AGENTS.md` | Copied |

Logins, sessions, history and the state databases stay per account. If an account folder already had its own `config.toml` the first time, it is kept as `config.toml.before-cli-subagents`.

Workers start without `OPENAI_API_KEY`, `CODEX_API_KEY` or the host's `CODEX_HOME`, so they always use the account's own ChatGPT login.

## Using it

Pass `runtime: "codex"` to `subagent_start`, or `--runtime codex` to `csa run` and `csa start`.

| `access` | Codex flags |
|---|---|
| `read` | `-s read-only` |
| `edit` | `--approve-for-me` (workspace-write sandbox, Codex's auto-review approves or denies escalations) |
| `full` | `--dangerously-bypass-approvals-and-sandbox` |

`model` maps to `-m` and `effort` to `model_reasoning_effort`. `resume_job` continues a Codex thread with `codex exec resume` on the same account.

## Limits

`codex exec --json` doesn't report remaining quota, so CLI Subagents can't rank Codex accounts by usage the way it does for Claude. It recognizes a usage-limit failure from the error message, cools that account down (`cooldown_minutes`, default 60), and, if you enabled `failover = "on-limit"` under `[codex]`, retries the job on the next account with the partial output attached.

## Notes

- On Windows the npm `codex` command is a `.cmd` shim. CLI Subagents runs its `codex.js` launcher with Node directly, which avoids `cmd.exe` quoting problems.
- Codex prints nothing to stdout except the JSON events, and the final answer is the last `agent_message`.
