# Configuration

CLI Subagents keeps everything under `~/.cli-subagents/` (or `CLI_SUBAGENTS_HOME`). `csa accounts add` writes the config for you; edit it by hand for the other options.

## config.toml

```toml
job_retention_days = 7       # finished job records older than this are deleted
max_depth = 1                # 1 = workers can't start their own subagents
# max_jobs = 8               # most subagents running at once on this machine, all runtimes (default: no cap)

[claude]
primary = "~/.claude"        # the shared setup (default: CLAUDE_CONFIG_DIR or ~/.claude)
include_primary = false      # also run workers on the primary account
failover = "on-limit"        # continue on the next account at a usage limit; "off" to stop
max_jobs_per_account = 1     # parallel workers per account folder
connectors = "account"       # "account" keeps each account's claude.ai connectors; "off" hides them
edit_permission_mode = "auto"  # permission mode for access="edit"
cooldown_minutes = 60        # cooldown when the limit error has no Retry-After or reset time (any cooldown is capped at 7 days)
# windows_sandbox = "unelevated"   # [codex] on Windows: "unelevated", "elevated" or "off" (see docs/codex.md)
# binary = "C:/path/to/claude.exe"   # only if auto-detection fails
# default_model = "sonnet"

[[claude.accounts]]
name = "max-2"
home = "~/.cli-subagents/homes/claude/max-2"
enabled = true

[codex]
primary = "~/.codex"        # default: CODEX_HOME or ~/.codex
failover = "on-limit"

[[codex.accounts]]
name = "plus-2"
home = "~/.cli-subagents/homes/codex/plus-2"
```

The `[codex]` section takes the same keys as `[claude]`; `connectors` only applies to Claude. `share_parent_mcp` (default `true`) applies to Codex and Antigravity: workers started from Claude Code get its MCP servers (see [codex.md](codex.md#what-a-worker-gets-from-a-claude-code-session)); for Antigravity your own Gemini servers win when two have the same name. `windows_sandbox` only applies to Codex: on Windows, read and edit jobs get `-c windows.sandbox="<value>"` unless your Codex `config.toml` already sets `[windows] sandbox`; `"off"` adds nothing. `[antigravity]` takes `binary`, `default_model`, `edit_permission_mode` (`accept-edits` by default, or `auto` where the account supports it) and `share_parent_mcp`, the review keys below, and the account keys further down.

```toml
[antigravity]
reviewer = "agy"            # who judges actions the fixed rules can't settle: "agy", "claude" or "off"
# reviewer_model = "gemini-3.8-flash-low"   # default for "agy"; "haiku" is the default for "claude"
# reviewer_account = "max-2"                # the Claude account for reviewer = "claude" (default: your primary)
sandbox = true              # run edit jobs with agy --sandbox
```

With `reviewer = "off"` only the fixed rules decide, and everything they would hand to the reviewer is denied. See [Safety](gemini.md#safety) for what the gate does.

Extra Antigravity accounts are opt-in and at your own risk (Google's FAQ says consumer plans don't support automated account switching; see [Gemini](gemini.md#more-than-one-google-account)). For `[antigravity]`, `failover` is `"off"` by default: jobs without `account` then run on the primary account only, and an extra account is used only when a job pins it. `"on-limit"` rotates through the primary and the enabled extra accounts (`include_primary` is `true` by default here). An Antigravity account folder stands in for a home folder: it gets its own `.gemini`.

```toml
[antigravity]
failover = "off"            # "on-limit" rotates between the accounts (use at your own risk)

[[antigravity.accounts]]
name = "google-2"
home = "~/.cli-subagents/homes/antigravity/google-2"
```

When no extra accounts are configured, workers run on the primary account. That is still useful from another host (for example Codex delegating to Claude Code), and with `include_primary = true` the primary account joins the pool.

`max_jobs_per_account` defaults to 1 (see Limits for per-account caps) because several Claude Code processes writing the same state file at once have corrupted it in the past. Raise it if you know your version handles it.

## Limits

CLI Subagents counts the running jobs in `~/.cli-subagents/jobs` before it starts one. That covers nested subagents and other chat sessions on the same machine. If no account can take the job now, the start is refused at once with one line that names the cap and what to do instead. The model that asked doesn't have to track quotas.

Nothing is capped until you set a cap. Presets are recommended defaults and apply only when you set `plan`.

| Key | Where | Meaning |
|---|---|---|
| `plan` | `[codex]`, `[claude]`, `[antigravity]` (the primary account) and each `[[<runtime>.accounts]]` | A preset: `free`, `plus`, `pro`, `max-5x`, `max-20x`, `team`, `ultra` and so on, per runtime (table in [research/limits.md](research/limits.md)). `"auto"` reads a Claude account's plan from its login info. No `plan` means no preset. |
| `max_jobs` | an account entry | Most jobs running on that account. Beats the preset. |
| `max_effort` | an account entry | Highest effort. A higher request is lowered to it and the job gets a warning. |
| `models` | an account entry | Models this account may run. A job asking for another model skips the account. |
| `primary_max_jobs`, `primary_max_effort`, `primary_models` | `[<runtime>]` | The same three for the primary account. |
| `max_jobs` | `[<runtime>]` | Most jobs running on that runtime, over all accounts. |
| `max_jobs` | top level | Most jobs running over all runtimes. |
| `usage_ceiling` | `[<runtime>]` | Default `0.95`. An account at or above this share of its 5-hour or weekly window gets no new jobs. It applies only when usage is known; csa never guesses. |
| `when_full` | `[<runtime>]` | `"refuse"` (default): the start fails with a short message. `"queue"`: the job starts and waits for a free slot (the caps still hold on each account). |
| `max_jobs_per_account` | `[<runtime>]` | The old default for accounts without a cap. It shapes the runner's queue and refuses nothing. |

Order for one account: its own `max_jobs`, then its plan preset, then `max_jobs_per_account`. A `fallback = true` account still comes after every other account.

```toml
[codex]
plan = "plus"                       # the primary account: 4 jobs at once
usage_ceiling = 0.95

[[codex.accounts]]
name = "free-2"
home = "~/.cli-subagents/homes/codex/free-2"
plan = "free"                       # 1 job, effort up to high
fallback = true                     # only after the other accounts
models = ["gpt-6-luna"]             # the free plan's model
```

A refusal looks like this:

```
Codex is at its limit: 4 of 4 subagents running (plan preset plus: 4 per account; change with max_jobs). Start this with runtime claude, or wait for one to finish.
Room: Claude 2 accounts (no usage data) · Codex primary 8% left (5h, resets 03:12)
```

The `Room:` line is also in the `subagent_start` result, the `subagent_accounts` result and the `csa run --follow` header. It shows what is already known, marks old data with its age (`as of 2 h ago`) and leaves out accounts with no data. Codex usage is read from the newest rollout file in each Codex account folder (the `rate_limits` field only). Claude usage comes from the events of jobs that already ran. Antigravity shows none. Nothing starts a job to measure usage.

`csa accounts`, `subagent_accounts` and `csa doctor` list each account's cap, effort cap and model list, and where each one comes from. For Claude accounts they also suggest a plan (read from `oauthAccount.userRateLimitTier` and `organizationRateLimitTier` in the account's `.claude.json`, nothing else). A suggestion is not applied until you set `plan`.

### What goes where

- Project defaults are safe for everyone: no caps without a plan, `usage_ceiling` 0.95 only when usage is known, `when_full = "refuse"`.
- Your `config.toml` holds your plans and caps: `plan`, `max_jobs`, `models`, `fallback`.
- Your agent instructions (`CLAUDE.md` and the like) hold preferences, such as which model to use for what kind of task.
- csa decides by itself: it counts running jobs, checks cooldowns and usage, picks an account, and lowers the effort when the account's plan needs it.

## Files

| Path | Contents |
|---|---|
| `config.toml` | The configuration above |
| `homes/<runtime>/<account>/` | Account folders (login, state, sessions, linked shared setup) |
| `homes/antigravity/primary/` | The folder Antigravity jobs on your primary account run in (its login stays in the OS keyring) |
| `homes/antigravity/.reviewer/<account>/` | The bare folder the Antigravity reviewer runs from |
| `jobs/<job_id>/` | `spec.json`, `status.json`, `events.jsonl`, `result.md`, `stderr-*.log`, `runner.log`, the message logs `inbox.jsonl` and `outbox.jsonl`, for Claude workers the `mcp.json` and `settings.json` they launch with, and for Antigravity workers the review gate's `review-context.json`, `review.jsonl` (every decision) and `review-cache.json` |
| `state/<runtime>-usage.json` | Last known usage, cooldowns and last use per account |
| `state/leases/` | Lock files for per-account job slots |
| `cache/claude/<account>/` | Generated `settings.json` overlay and MCP config passed to workers |

Job records and generated files contain your prompts, results and MCP server settings. They stay on your machine and are created with owner-only permissions where the OS supports it.

## Environment variables

| Variable | Effect |
|---|---|
| `CLI_SUBAGENTS_HOME` | Use a different root folder (`csa --home <folder>` sets it for one command) |
| `CLI_SUBAGENTS_DEPTH` | Set by CLI Subagents in workers; used to refuse nested delegation |
| `CLAUDE_CONFIG_DIR` | Default for `[claude] primary` when the config doesn't set it |
| `CODEX_HOME` | Default for `[codex] primary` when the config doesn't set it |

Workers start from a cleaned environment. Variables that identify or authenticate the host session (`ANTHROPIC_*`, `CLAUDE*`, `CODEX_COMPANION_*` and similar) are removed, so a worker can only use its own account's login. Put environment settings you want workers to have into the `env` block of your primary `settings.json`; it is passed on, minus login-related keys.
