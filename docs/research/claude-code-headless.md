# Claude Code as a headless worker: official behavior

Checked 2026-10-14 against the Claude Code docs at https://code.claude.com/docs/en/ and the anthropics/claude-code issue tracker. Tags: (stated) means the cited page says it; (inferred) is a conclusion drawn from it; (not documented) means no official text was found; (community) means not official.

This note answers what CLI Subagents needs from `claude -p`: per-account config, auth, MCP, headless flags, rate-limit signals, session portability and plugin packaging. Terms and existing multi-account tools are covered in [claude-multi-account.md](claude-multi-account.md).

## 1. CLAUDE_CONFIG_DIR

- (stated) Each config dir has "its own settings, session history, and claude.ai login or API key." The docs recommend one dir per account to stay signed in to several accounts at once. [authentication](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts)
- (stated) "If you set CLAUDE_CONFIG_DIR, every ~/.claude path on this page lives under that directory." That covers `settings.json`, `settings.local.json`, `CLAUDE.md`, `rules/`, `skills/`, `commands/`, `agents/`, `output-styles/`, `workflows/`, `keybindings.json`, `themes/`, `plugins/` (install records, `cache/`, `data/`), `projects/<project>/` (transcripts, subagents, tool results, auto memory), `file-history/`, `history.jsonl`, `backups/`, `sessions/` and `.credentials.json` on Linux and Windows. [claude-directory](https://code.claude.com/docs/en/claude-directory)
- (not documented) Whether CLAUDE_CONFIG_DIR also moves `~/.claude.json` (app state, cached OAuth account, per-project trust, local and user MCP servers). (community) Bug reports say it resolves to `$CLAUDE_CONFIG_DIR/.claude.json`. Needs a local test.
- (stated) Credentials: macOS uses the Keychain and "keys the macOS Keychain entry to that directory too"; Linux and Windows use `.credentials.json` inside the config dir. [authentication](https://code.claude.com/docs/en/authentication#credential-management)
- (stated) Exception: a Console sign-in without an API key is stored outside the config dir (`%APPDATA%\Anthropic` on Windows, `~/.config/anthropic` elsewhere), so separate dirs don't keep those apart.
- (stated) Processes with different CLAUDE_CONFIG_DIR values may run at the same time; that is the documented multi-account pattern.
- (stated, partly) Within one dir, `sessions/` marker files detect concurrent sessions and token refresh is coordinated ("another Claude Code process is refreshing it"). Many concurrent writers in one dir are not documented as supported. [errors](https://code.claude.com/docs/en/errors)
- (community) [#29004](https://github.com/anthropics/claude-code/issues/29004) reports two or more Windows instances corrupting `.claude.json` (no locking or atomic write); closed as not planned, with many duplicates.
- (stated) Transcripts and history are not encrypted at rest.

## 2. setup-token and CLAUDE_CODE_OAUTH_TOKEN

- (stated) `claude setup-token` runs the browser login and prints a one-year OAuth token without saving it. Requires Pro, Max, Team or Enterprise. [authentication](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)
- (stated) The token "can only make model requests, so it can't establish Remote Control sessions or fetch claude.ai connectors. MCP servers you configure locally still work."
- (not documented) Its exact scopes. (community) Reports say it carries only `user:inference`, which would also explain why `rate_limits` comes back null for it (the SDK docs say that happens without the `user:profile` scope).
- (stated) Authentication precedence, highest first ([authentication](https://code.claude.com/docs/en/authentication#authentication-precedence)):
  1. Cloud provider switches (`CLAUDE_CODE_USE_BEDROCK`, `_VERTEX`, `_FOUNDRY`)
  2. `ANTHROPIC_AUTH_TOKEN`
  3. `ANTHROPIC_API_KEY` ("In non-interactive mode (`-p`), the key is always used when present.")
  4. `apiKeyHelper`
  5. `CLAUDE_CODE_OAUTH_TOKEN`
  6. Anthropic profile or federation credentials (only when named by `ANTHROPIC_PROFILE`)
  7. Subscription OAuth from `/login`
- (inferred) Any of the first five in a parent's environment or a shared settings file silently overrides a child's subscription login. The spawner must scrub them.

## 3. MCP configuration

- (stated) Scopes: local servers live in `~/.claude.json` under the project path, project servers in `.mcp.json` at the project root, user servers in `~/.claude.json`. [mcp](https://code.claude.com/docs/en/mcp)
- (stated) `--mcp-config` takes JSON files or strings; `--strict-mcp-config` uses only those. With `-p` the CLI waits up to `MCP_TIMEOUT` (30 s default) for servers to start. [cli-reference](https://code.claude.com/docs/en/cli-reference)
- (stated) claude.ai connectors "are automatically available in Claude Code" after a claude.ai login, and "are fetched only when your active authentication method is a claude.ai subscription login." They belong to that account. Controls: `"disableClaudeAiConnectors": true`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `deniedMcpServers`. Servers from `--mcp-config` are not affected by `disableClaudeAiConnectors`. [mcp](https://code.claude.com/docs/en/mcp#use-mcp-servers-from-claude-ai)
- (not documented) Where OAuth tokens for remote MCP servers are stored ("stored securely"); `claude mcp logout <name>` clears them.
- (stated) In `-p` runs, project `.mcp.json` servers load without asking; `disabledMcpjsonServers` blocks named ones.

## 4. Headless flags

- (stated) Output: `--output-format text|json|stream-json`, `--input-format text|stream-json`, `--verbose`, `--include-partial-messages`, `--include-hook-events`, `--json-schema`. JSON output includes `result`, `session_id`, `total_cost_usd` and usage. [headless](https://code.claude.com/docs/en/headless)
- (stated) Sessions: `--resume <id|name|path>`, `--continue`, `--fork-session`, `--session-id <uuid>`, `--no-session-persistence`.
- (stated) Config: `--settings <file|json>` overlays keys on top of the file-based ones; `--setting-sources user,project,local`; `--plugin-dir` (repeatable, session only); `--add-dir`; `--agents <json|file>`; `--append-system-prompt[-file]`; `--system-prompt[-file]`; `--tools`; `--disable-slash-commands`.
- (stated) Model and limits: `--model`; `--effort low|medium|high|xhigh|max|ultracode`; `--max-turns`; `--max-budget-usd` (client-side estimate).
- (stated) `--allowedTools` / `--disallowedTools` use permission-rule syntax, evaluated deny, then ask, then allow. [permissions](https://code.claude.com/docs/en/permissions)
- (stated) `--permission-mode`: `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions` (`manual` aliases `default`). `--permission-prompt-tool <mcp tool>` answers prompts in `-p`. `--permission-prompts none` denies unanswered prompts.
- (stated) Auto mode works with `-p`. Blocked actions are denied in non-interactive runs; after 3 blocks in a row or 20 in total the action doesn't run, but the run continues. The default starting mode for `-p` differs between versions, so pass it explicitly. [permission-modes](https://code.claude.com/docs/en/permission-modes)
- (stated) PreToolUse hooks return `allow`, `deny`, `ask` or `defer`. Deny and ask rules still apply whatever the hook says; across hooks the precedence is deny > defer > ask > allow. `defer` works only in `-p` with a single tool call per turn: the run exits with `stop_reason: "tool_deferred"` and resumes later. PermissionRequest hooks run in `-p`; undecided requests are denied. [hooks](https://code.claude.com/docs/en/hooks#pretooluse-decision-control)
- (stated) `--bare` never reads OAuth credentials and skips hooks, plugins, MCP and CLAUDE.md. It can't be used with subscription auth. [headless](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode)

## 5. Rate-limit signals

- (stated) Interactive messages: "You've hit your session limit · resets 3:45pm", "You've hit your weekly limit · resets Mon 12:00am", plus model-specific variants. A separate throttle reads "Server is temporarily limiting requests (not your usage limit)." [errors](https://code.claude.com/docs/en/errors)
- (not documented) The exit code and stdout of `-p` at a subscription limit. A failed run exits non-zero and prints the failure as the result.
- (stated, SDK) Assistant messages carry an `error` category such as `rate_limit` (429 against your quota) or `overloaded` (529). Result messages have `is_error` and `api_error_status`. [agent-sdk/typescript](https://code.claude.com/docs/en/agent-sdk/typescript)
- (stated) stream-json emits `system/api_retry` before a retry, with `error` and `error_status`. It only fires for retryable attempts. [headless](https://code.claude.com/docs/en/headless#handle-api-retries)
- (stated, SDK) `rate_limit_event` with `rate_limit_info.status` of `allowed`, `allowed_warning` or `rejected`, plus optional `resetsAt` and `utilization`. No separate 5-hour and weekly fields. (inferred) The CLI probably emits it on stream-json too; verify.
- (stated) The statusline input has `rate_limits.five_hour` and `seven_day` with `used_percentage` and `resets_at`, for Pro and Max only, after the first response. [statusline](https://code.claude.com/docs/en/statusline)
- (stated) `/usage` shows plan usage; in the SDK it returns `usage_report.rate_limits` (experimental). (not documented) Whether that costs a model request.

## 6. Session portability

- (stated) Transcripts live at `<config dir>/projects/<project>/<session>.jsonl`. [sessions](https://code.claude.com/docs/en/sessions#where-transcripts-are-stored)
- (stated) `--resume <id>` searches the current project, then every other project on the machine. `-p` sessions are resumable by ID.
- (stated) Resume does not restore `--mcp-config`, `--settings`, `--plugin-dir`, `--fallback-model` or `--add-dir`; pass them again.
- (stated) Resuming one session from two places without `--fork-session` interleaves both into one transcript.
- (inferred) Account B can't see a transcript from account A's dir unless the file is present in B's dir.
- (not documented) Whether a session is tied to an account, and whether resuming under another account works.

## 7. Plugin packaging

- (stated) Layout: `.claude-plugin/plugin.json` (only `name` required) plus root-level `skills/`, `commands/`, `agents/`, `hooks/hooks.json`, `.mcp.json`, `bin/`, `settings.json` and others. A root `CLAUDE.md` is not loaded; ship instructions as a skill. [manifest-reference](https://code.claude.com/docs/en/plugins/manifest-reference#standard-layout)
- (stated) Plugin MCP servers come from `.mcp.json` or inline `mcpServers`; tools are named `mcp__plugin_<plugin>_<server>__<tool>`.
- (stated) `${CLAUDE_PLUGIN_ROOT}` is the installed version folder (changes on update); `${CLAUDE_PLUGIN_DATA}` persists across updates; `${CLAUDE_PROJECT_DIR}` is the project root.
- (stated) Plugins are stored per config dir under `plugins/` (`cache/<marketplace>/<plugin>/<version>/`, `installed_plugins.json`, `known_marketplaces.json`). [plugins/loading](https://code.claude.com/docs/en/plugins/loading)
- (stated) Marketplaces use `.claude-plugin/marketplace.json` with `name`, `owner` and `plugins[]`; install with `claude plugin marketplace add` then `claude plugin install name@marketplace`. Settings can declare `extraKnownMarketplaces`. [marketplace-reference](https://code.claude.com/docs/en/plugins/marketplace-reference)
- (stated) Timeouts: `MCP_TIMEOUT` (startup, 30 s), `MCP_TOOL_TIMEOUT` (per call; default about 28 hours), `MAX_MCP_OUTPUT_TOKENS` (25,000), `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (30 min for stdio), `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` (2 min). [mcp](https://code.claude.com/docs/en/mcp)
- (stated) Long work can also run as background Bash; in `-p`, idle background waits stop after 10 minutes unless `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` changes it.

## 8. Legal text

From [legal-and-compliance](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use):

- "OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed to support ordinary use of Claude Code and other native Anthropic applications."
- "developers may not collect, store, or intermediate Claude.ai credentials or session tokens"
- "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription"
- "Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."
- "The Claude Code binary must not be modified."

(inferred) None of this addresses one person rotating several personal subscriptions to get past one account's limit. See the risk discussion in [claude-multi-account.md](claude-multi-account.md).

## Design implications

1. Give each account its own CLAUDE_CONFIG_DIR, set it explicitly on every child, and limit how many children share one dir at a time (default one).
2. Scrub `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CODE_USE_*` from the child environment, and keep `apiKeyHelper` out of shared settings. Check each account with `claude auth status --json` before use.
3. Share config by injecting it at launch instead of copying it into each dir: a `--settings` overlay, `--plugin-dir` per plugin, `--mcp-config`, `--agents`, `--append-system-prompt-file`. Confirm what loaded from the `system/init` event (`mcp_servers`, `plugins` and their error lists).
4. claude.ai connectors can't be shared because they follow each account's login. Offer `disableClaudeAiConnectors: true` in the shared overlay so every account sees the same set, and recommend configuring needed connectors as ordinary MCP servers.
5. Don't use `--bare`. Prevent recursive spawning with `--disallowedTools "mcp__<this server>__*"`. For untrusted repos consider `disableAllHooks`, since `-p` runs repo hooks and `.mcp.json` servers without asking.
6. Always pass `--permission-mode`, `--max-turns` and `--max-budget-usd`. `auto` is documented for `-p`.
7. Treat an account as exhausted on any of: `is_error` with `api_error_status` 429, `api_retry` with `error: rate_limit`, `rate_limit_event` with `status: rejected`, or the "hit your ... limit · resets" text. Test a real limit per account before relying on it.
8. Resume only inside the same config dir, and re-pass the injected flags. Moving a task to another account starts a new session with a handoff summary unless a test proves transcript copying works.
9. Long tasks: an async job API (start, status, result, cancel) instead of one long blocking MCP call.
10. Never read `.credentials.json` or the Keychain.

## Verified locally

Checked on 2026-10-14 with Claude Code 2.1.295 on Windows 11, using two account folders signed in to the same paid plan and Haiku for every run.

- `system/init` lists `mcp_servers` (with `name`, `status` and `source`, where claude.ai connectors have `source: claudeai`), `plugins`, `skills`, `agents`, `slash_commands`, `tools`, `permissionMode`, `model`, `apiKeySource` and `claude_code_version`. This is enough to check what a worker actually loaded.
- Every run emits a `rate_limit_event` such as `{"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.85, "resetsAt": 1792069200, "unifiedWindows": {"five_hour": {"utilization": 0.13, "resetsAt": ...}, "seven_day": {...}}}`. So both windows are reported on each run, not only near a limit.
- `--permission-mode auto` works with `-p`; `system/init` reports `permissionMode: auto`, and a file-creating task completed without prompts.
- The prompt can be sent on stdin with `-p` and no prompt argument, which avoids Windows command-line length and quoting limits.
- With `CLAUDE_CONFIG_DIR` set, the state file is `<dir>/.claude.json`; without it, `~/.claude.json`.
- A fresh account folder gets a `skills/synced` folder from Claude Code itself, which holds account-synced skills.
- A process started from inside a Claude Code session inherits the host's `ANTHROPIC_BASE_URL`, account and organization IDs, OAuth scopes, `CLAUDE_CODE_PLUGIN_DIRS`, `CLAUDE_CODE_EXECPATH` and about 30 more variables. Workers must not inherit them. `CLAUDE_CODE_EXECPATH` is still useful to find the host's `claude` binary.
- The Claude desktop app bundles the CLI under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\<version>\...\claude.exe`; inside the app's own process tree the same folder appears as `%APPDATA%\Claude\claude-code`.
- A login started with `claude auth login` uses whichever organization is active in the browser, so a profile meant for a personal plan can end up on a Team organization.
