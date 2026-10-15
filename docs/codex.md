# Codex: setup and use

CLI Subagents can run `codex exec` workers the same way it runs `claude -p` workers: in the background, on one of your accounts, with one shared setup. The host can be Claude Code (through the plugin) or any MCP host, Codex included.

Every worker is the official `codex` CLI with your own ChatGPT login. See [provider terms](research/provider-terms.md) for what OpenAI publishes about this kind of use.

## Requirements

- Codex CLI installed and signed in (`codex login status`)
- The `csa` package installed (see the [Claude Code guide](claude-code.md#1-install))

With no extra Codex accounts configured, workers run on your normal Codex login (`~/.codex`, or `CODEX_HOME`).

## Extra accounts

```bash
csa accounts add plus-2 --runtime codex
```

This creates `~/.cli-subagents/homes/codex/plus-2`, links in your shared setup, and opens `codex login` for that folder. Every command CLI Subagents runs for an extra account passes `cli_auth_credentials_store=file`, so each login lives in that account's own `auth.json` and never in a shared keyring entry. The tool never reads it.

Check everything with:

```bash
csa accounts --runtime codex
csa doctor --runtime codex
```

## What every account shares

| From the primary `CODEX_HOME` | How it reaches each account |
|---|---|
| `config.toml` (MCP servers, model defaults, features, profiles) | Written into the account folder before every run, table by table |
| `skills/`, `plugins/` entries | Linked one by one (junctions on Windows) |
| `AGENTS.md` | A hard link to your file |

The account's own `config.toml` keeps three kinds of tables that Codex writes per home: `[projects.*]` (folder trust), `[hooks.state.*]` and `[notice.*]`. Everything else comes from your primary file. Logins, sessions, history and the state databases stay per account. If an account folder already had its own `config.toml` the first time, it is kept as `config.toml.before-cli-subagents`.

This tool's own MCP server stays in the copied config, but every Codex worker, on the primary account too, is started with `-c mcp_servers.<name>.enabled=false` for it, so workers can't start subagents of their own. Instead each worker gets the `csa-parent` tools (`message_parent`, `ask_parent`, `check_parent_messages`) through `-c` overrides.

Workers start without `OPENAI_API_KEY`, `CODEX_API_KEY` or the host's `CODEX_HOME`, so they always use the account's own ChatGPT login.

## Using it

Pass `runtime: "codex"` to `subagent_start`, or `--runtime codex` to `csa run` and `csa start`.

| `access` | Codex flags |
|---|---|
| `read` | `-s read-only` (`sandbox_mode=read-only` when resuming) |
| `edit` | `--approve-for-me` (workspace-write sandbox, Codex's auto-review approves or denies escalations; the same settings as `-c` overrides when resuming) |
| `full` | `--dangerously-bypass-approvals-and-sandbox` |

`model` maps to `-m` and `effort` to `model_reasoning_effort` (`max` is sent as `xhigh`, Codex's highest level). `resume_job` continues a Codex thread with `codex exec resume` on the same account.

## Limits

`codex exec --json` doesn't report remaining quota, so CLI Subagents can't rank Codex accounts by usage the way it does for Claude. It recognizes a usage-limit failure from the error message and cools that account down until the `Retry-After` value or reported reset time in the error, or else for `cooldown_minutes` (default 60), never more than 7 days. The job then restarts on the next account with the partial output attached; a Codex thread isn't moved between accounts. Failover is on by default. `csa accounts reset <name> --runtime codex` clears a cooldown. Set `failover = "off"` under `[codex]` to stop at the limit instead.

## Notes

- Windows sandbox: without a `[windows] sandbox` setting in `~/.codex/config.toml`, Codex 0.162.1 has no sandbox backend (`codex doctor` says `sandbox backend: disabled`) and refuses every command in a sandboxed run, even reading a file. CLI Subagents fixes this for `read` and `edit` jobs by itself: when your Codex config sets no `[windows] sandbox`, it adds `-c windows.sandbox="unelevated"`. That needs no admin rights. Commands then run, writes inside the workspace work, and writes outside it (and outside Temp) are denied. `full` jobs bypass the sandbox and get nothing added. To change it, set `windows_sandbox` under `[codex]` in the CLI Subagents config: `"unelevated"` (default), `"elevated"` (needs admin rights once) or `"off"` (add nothing). A `[windows] sandbox` in your own Codex config always wins. `csa doctor --runtime codex` shows which one jobs will use. Details and sources: [the research note](research/codex-windows-sandbox.md).
- On Windows the npm `codex` command is a `.cmd` shim. CLI Subagents runs its `codex.js` launcher with Node directly, which avoids `cmd.exe` quoting problems.
- Codex prints nothing to stdout except the JSON events, and the final answer is the last `agent_message`.
