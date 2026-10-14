# Configuration

CLI Subagents keeps everything under `~/.cli-subagents/` (or `CLI_SUBAGENTS_HOME`). `csa accounts add` writes the config for you; edit it by hand for the other options.

## config.toml

```toml
job_retention_days = 7       # finished job records older than this are deleted
max_depth = 1                # 1 = workers can't start their own subagents

[claude]
primary = "~/.claude"        # the shared setup (default: CLAUDE_CONFIG_DIR or ~/.claude)
include_primary = false      # also run workers on the primary account
failover = "off"             # "off" or "on-limit"
max_jobs_per_account = 1     # parallel workers per account folder
connectors = "account"       # "account" keeps each account's claude.ai connectors; "off" hides them
edit_permission_mode = "auto"  # permission mode for access="edit"
cooldown_minutes = 60        # cooldown when a limit is hit without a reset time
# binary = "C:/path/to/claude.exe"   # only if auto-detection fails
# default_model = "sonnet"

[[claude.accounts]]
name = "max-2"
home = "~/.cli-subagents/homes/claude/max-2"
enabled = true

[codex]
primary = "~/.codex"        # default: CODEX_HOME or ~/.codex
failover = "off"

[[codex.accounts]]
name = "plus-2"
home = "~/.cli-subagents/homes/codex/plus-2"
```

The `[codex]` section takes the same keys as `[claude]`; `connectors` only applies to Claude. `[antigravity]` takes `binary`, `default_model` and `edit_permission_mode` (`accept-edits` by default, or `auto` where the account supports it); it can't have extra accounts or failover.

When no extra accounts are configured, workers run on the primary account. That is still useful from another host (for example Codex delegating to Claude Code), and with `include_primary = true` the primary account joins the pool.

`max_jobs_per_account` defaults to 1 because several Claude Code processes writing the same state file at once have corrupted it in the past. Raise it if you know your version handles it.

## Files

| Path | Contents |
|---|---|
| `config.toml` | The configuration above |
| `homes/<runtime>/<account>/` | Account folders (login, state, sessions, linked shared setup) |
| `jobs/<job_id>/` | `spec.json`, `status.json`, `events.jsonl`, `result.md`, `stderr-*.log`, `runner.log` |
| `state/<runtime>-usage.json` | Last known usage, cooldowns and last use per account |
| `state/leases/` | Lock files for per-account job slots |
| `cache/claude/<account>/` | Generated `settings.json` overlay and MCP config passed to workers |

Job records and generated files contain your prompts, results and MCP server settings. They stay on your machine and are created with owner-only permissions where the OS supports it.

## Environment variables

| Variable | Effect |
|---|---|
| `CLI_SUBAGENTS_HOME` | Use a different root folder |
| `CLI_SUBAGENTS_DEPTH` | Set by CLI Subagents in workers; used to refuse nested delegation |
| `CLAUDE_CONFIG_DIR` | Default for `[claude] primary` when the config doesn't set it |
| `CODEX_HOME` | Default for `[codex] primary` when the config doesn't set it |

Workers start from a cleaned environment. Variables that identify or authenticate the host session (`ANTHROPIC_*`, `CLAUDE*`, `CODEX_COMPANION_*` and similar) are removed, so a worker can only use its own account's login. Put environment settings you want workers to have into the `env` block of your primary `settings.json`; it is passed on, minus login-related keys.
