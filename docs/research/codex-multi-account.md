# Codex CLI with several accounts

Research for Agents Router: running one shared Codex setup (MCP servers, skills, plugins, settings) across several ChatGPT logins, with the official `codex` binary doing all model traffic.

Accessed 2026-10-14. Local version checked: `codex-cli 0.162.1` (local --help).

Labels used below: docs (official Codex docs, fetched via learn.chatgpt.com, which the developers.openai.com links redirect to), source (openai/codex repository files), changelog, local --help (output of the installed binary), community (third-party posts, tools or issues), not found (the pages I read do not say).

## Summary

- CODEX_HOME decides which config, login and session state a Codex process uses. It defaults to `~/.codex` (docs).
- Login is per home. In file mode the credential is `auth.json` under CODEX_HOME. In keyring mode the docs do not say how entries are keyed (docs; not found).
- No official mechanism for sharing one config across homes. The documented building blocks are `-c` overrides, `--profile` (same home only) and project-level `.codex/config.toml` (docs, local --help). Symlinking or junctioning `config.toml`, `skills/` or `plugins/` into each home is not documented, so it needs a test.
- `codex exec --json` emits JSONL events. Those events have no rate-limit or remaining-percent field (source). A usage-limit failure should arrive as a `turn.failed` or `error` event with a message. The exact text is not verified, and the exit code is not documented (not found).
- Remaining quota percentages are in the app-server protocol (`RateLimitSnapshot.primary.usedPercent`, source). That protocol is marked experimental (local --help).
- Session files and their SQLite index live in each home. No official source says a session can be resumed under another account (not found). Community reports say it does not work (community).
- The docs recommend API key auth for programmatic CLI use such as CI/CD (docs). The terms excerpts bar circumventing rate limits and sharing account access. Rotating subscription logins to get past a usage limit is the part that conflicts with those terms.

## CODEX_HOME layout

Names in the local `%USERPROFILE%\.codex` (names only, nothing opened): `auth.json`, `cache`, `config.toml`, `goals_1.sqlite`, `installation_id`, `log`, `logs_2.sqlite` (with `-shm` and `-wal`), `memories_1.sqlite`, `models_cache.json`, `plugins`, `queue_1.sqlite` (with `-shm` and `-wal`), `session_index.jsonl`, `sessions`, `skills`, `state_5.sqlite`, `thread-writer-locks`, `thread_history_1.sqlite`, `tmp`, `.sandbox_migration`, `.sqlite-maintenance.lock` (local listing).

What the docs assign to CODEX_HOME:

- `config.toml`, the local configuration (docs, "Config and state locations").
- `auth.json` in file credential mode (docs, auth).
- `log/` by default, via `log_dir` (docs, config reference).
- `history.jsonl` when `history.persistence` is `save-all` (docs, config reference).
- The SQLite state DB, located by `sqlite_home`. The default is not stated (docs, config reference).
- Profile files, `<name>.config.toml` (docs, config-advanced).
- Skills and plugins: the config reference names `skills.config` and `plugins.<plugin>.enabled` but does not state the directory. The `skills/` and `plugins/` folders in the local listing are probably where they live (inference from the local listing; the docs do not name the path).
- `AGENTS.md`: not found in the pages I read.
- MCP OAuth credentials: the store is chosen by `mcp_oauth_credentials_store` (`auto`, `file`, `keyring`). The config reference states no default, and the docs do not say where file-mode MCP tokens are kept. No separate MCP credential file is in the local listing (docs; not found).

Concurrency and relocation:

- Setting `CODEX_HOME` moves the whole state directory (docs).
- Two processes with different `CODEX_HOME` values running at once: not documented (not found). Each home has its own SQLite files, which suggests separate state, but this is an inference.
- The local binary has a shared app-server daemon. `codex agents` says "Browse all agent sessions on the shared local app-server daemon", and `--no-daemon` says "Run without the shared background server, even if it is already running" (local --help). Whether that daemon is keyed by CODEX_HOME is not found. Test it before running two homes in parallel.

Config sharing options:

- `-c key=value` overrides one config key per invocation. Values are parsed as TOML (local --help, `codex exec --help`).
- `--profile name` loads `$CODEX_HOME/<name>.config.toml` on top of `config.toml` in the same home (docs). Profile files cannot be shared across homes without the same file-sharing question below.
- `--ignore-user-config` skips `$CODEX_HOME/config.toml` and "auth still uses `CODEX_HOME`" (local --help, exec). A wrapper could pass everything through `-c` instead.
- Project `.codex/config.toml` is read from the project root down to the working directory, and only when the project is trusted (docs). Project-level config cannot set profiles (config reference).
- Symlinks and junctions: the docs do not mention them (not found).

MCP servers:

- Each server is a `[mcp_servers.<name>]` table in `config.toml` (docs, extend/mcp; config reference).
- Keys in the config reference: `command`, `args`, `env` (stdio); `url`, `bearer_token_env_var` (HTTP); `enabled`. Defaults: `startup_timeout_sec` 10 s, `tool_timeout_sec` 60 s, `auth` `oauth` (config reference).
- Project-level servers go in `.codex/config.toml`, for trusted projects only (docs).
- `codex mcp add` writes to `config.toml`. The MCP page says the config lives there and that an OAuth client secret is saved as `mcp_servers.<name>.oauth.client_secret` in `config.toml` (docs). The page does not name the exact file `add` writes to beyond that.
- Subcommands: `list`, `get`, `add`, `remove`, `login`, `logout` (local --help).

## Auth

- Two sign-in modes: ChatGPT sign-in, which uses plan access, and API key sign-in, which uses standard API pricing "instead of included ChatGPT plan credits" (docs, auth).
- `cli_auth_credentials_store` takes `file`, `keyring`, `auto` or `ephemeral`. `file` writes `auth.json` under CODEX_HOME. `keyring` uses the OS credential store and "fails if it is unavailable". `auto` uses the OS store when available and falls back to `auth.json`. `ephemeral` keeps credentials in memory for the current process (docs, auth; config reference).
- The config reference states no default. A third-party sample file sets `file` (community). Set the value explicitly.
- Auth is tied to CODEX_HOME in file mode (docs).
- Device code login: `codex login --device-auth` (local --help; docs, auth). The docs suggest it for remote or headless machines.
- `codex login --with-api-key` reads the key from stdin. `codex login status` shows status. `codex logout` removes credentials (local --help).
- `CODEX_API_KEY` appears in the CI guidance (docs, non-interactive). Its precedence against `OPENAI_API_KEY` is not found in the pages I read.

## Headless and rate-limit signals

Flags confirmed in `codex exec --help` (local --help, 0.162.1): `--json`, `-o` / `--output-last-message`, `--output-schema`, `--ephemeral`, `-s` / `--sandbox` (`read-only`, `workspace-write`, `danger-full-access`), `-C`, `--add-dir`, `--ignore-user-config`, `--ignore-rules`, `--skip-git-repo-check`, `-m`, `-p`, `--color`, `--worktree`, `--dangerously-bypass-approvals-and-sandbox`.

Resume and fork: `codex exec resume [SESSION_ID]` or `--last`, and `codex exec fork` (local --help; docs, non-interactive).

Approvals: the exec help has no `--ask-for-approval` flag. The top-level `codex --help` lists `-a` / `--ask-for-approval` with `on-request` and `never` (local --help). The docs say `codex exec` runs in a read-only sandbox by default (docs).

App server: `codex app-server` is listed as "[experimental]" (local --help).

JSONL event types (source: `codex-rs/exec/src/exec_events.rs`):

- `thread.started` (`thread_id`)
- `turn.started`
- `turn.completed` (`usage`)
- `turn.failed` (`error`, which is an object with `message`)
- `item.started`, `item.updated`, `item.completed` (`item`)
- `error` (`message`)

The docs list the same event names (docs, non-interactive).

Rate-limit signal in exec:

- The exec event types have no rate-limit or remaining-percent field (source).
- Judging from the event shapes, a usage or rate limit would arrive as `turn.failed` with `error.message`, or as a top-level `error` event with `message`. The exact text for a usage limit is not verified from source (not found).
- Exit code: not documented (not found).
- Community report: a `codex exec` run ended with "exceeded retry limit, last status: 429 Too Many Requests" and an error body with code `rate_limit_exceeded` (community, [openai/codex issue 9135](https://github.com/openai/codex/issues/9135)). Other issues with the same text exist (community, [issue 26034](https://github.com/openai/codex/issues/26034)).

Remaining quota:

- The app-server protocol has `RateLimitSnapshot` with `limitId`, `limitName`, `primary`, `secondary`, `credits`, `individualLimit`, `spendControlReached`, `planType`, `rateLimitReachedType` and `normalModelSlug`. Each window is `RateLimitWindow` with `usedPercent` (number), `windowDurationMins` and `resetsAt` (source, schema/typescript/v2).
- The schema directory also has `AccountRateLimitsUpdatedNotification.ts` (source).
- A read method `account/rateLimits/read` is described in a mirror of openai/codex PR #5302 (community). I did not confirm the method name in the current main branch.
- Because the app-server is experimental, the method names and fields can change.

## Session portability

- Sessions are stored as JSONL rollout files under `sessions/`, grouped by date (community, [Codex session management guide](https://codex.danielvaughan.com/2026/04/13/codex-cli-session-management-resume-fork-transcripts)). The session index is a separate SQLite database in the same home (local listing: `state_5.sqlite`, `thread_history_1.sqlite`, `session_index.jsonl`).
- Official resume commands: `codex resume <SESSION_ID>`, `codex resume --last`, `codex exec resume` (local --help; docs).
- Resuming a session from account A under account B: not found in official sources.
- Community reports say sessions are kept per CODEX_HOME and do not carry over when the home changes (community, [Korean Windows guide](https://wikidocs.net/blog/@edwin/33022/)).
- Symlinking the `sessions/` folder is reported as risky, because resume depends on the SQLite index, and a mismatch between index and rollout files can break resume (community, same guide; community, [Codex session lifecycle guide](https://codex.danielvaughan.com/2026/06/05/codex-cli-session-lifecycle-archive-resume-fork-compact-management/)).

## Existing tools

1. codex-switcher (`cxs`), [mincia1110/codex-switcher](https://github.com/mincia1110/codex-switcher). Community, README seen only as search summary. Targets WSL and Linux. It keeps each account in its own home, copies the selected account into the default home before running `codex`, and has an `--isolated` mode that runs each account in its own CODEX_HOME. Shared sessions are a folder with symlinks from each account's sessions path. Rotation on limits: not described.
2. prodex, [docs.rs crate page](https://docs.rs/crate/prodex/0.2.94). Community, crate page seen via search. One profile is one CODEX_HOME. It auto-rotates across logged-in ChatGPT accounts when the current one hits its quota, and keeps continuation affinity so a chain of work stays on one account. Rotation on limits: yes. This is the feature the terms excerpts below cover.
3. codex-profiles, [OpenAI community post](https://community.openai.com/t/codex-profiles-switch-codex-accounts-without-copying-auth-json/1380415). Community. Launches Codex CLI or Desktop with a named CODEX_HOME without copying `auth.json`. Rotation on limits: not described.

Also seen: aicx on PyPI (https://pypi.org/project/aicx/), which covers Codex and Claude Code and shares conversations by default (community, search summary).

A GitHub issue in a third-party Codex app repo reports a maintainer comment that the Codex CLI and App Server do not support multiple accounts (community, [issue 66 on CodexMonitor](https://upd.dev/Dimillian/CodexMonitor/issues/66), January 2026, not verified against the thread).

## Terms (quotes)

The OpenAI help article and terms page both returned HTTP 403 with a bot-challenge page, to WebFetch and to a browser session. I did not try to bypass the challenge. The quotes below come from search-result excerpts and a mirror, not from the live pages. Verify them on the live pages before relying on them.

- Terms of Use, mirror of the December 2024 version: "You may not share your account credentials or make your account available to anyone else and are responsible for all activities that occur under your account." ([mirror](https://open.windriver.com/info/uni-license-list/licenses/openai-tou-20241211.html), secondary, not current).
- Terms of Use, current text as seen in search excerpt: "Interfere with or disrupt our Services, including circumvent any rate limits or restrictions or bypass any protective measures or safety mitigations we put on our Services." ([openai.com row terms](https://openai.com/en-GB/policies/row-terms-of-use/), search excerpt, not opened).
- Services Agreement, which governs API and business use, as seen in search excerpt: "violate or circumvent Usage Limits or otherwise configure the Services to avoid Usage Limits" and "buy, sell, or transfer API keys from, to, or with a third party" ([services agreement PDF](https://cdn.openai.com/osa/openai-services-agreement.pdf), search excerpt, not opened).
- Codex help, as seen in search excerpt: "Codex usage counts against a shared allowance on your plan." ([help article 11369540](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan-usage-limits-and-troubleshooting), search excerpt, page returned 403).
- Codex auth docs, as quoted by the fetch summary: "Use API key authentication for programmatic Codex CLI workflows, such as CI/CD jobs." and "When you sign in with an API key, Codex uses standard API pricing instead of included ChatGPT plan credits." ([docs, auth](https://learn.chatgpt.com/docs/auth)).
- Codex auth docs, same page: "If you use file-based storage, treat `~/.codex/auth.json` like a password: it contains access tokens." ([docs, auth](https://learn.chatgpt.com/docs/auth)).

Reading: the quotes do not forbid holding several accounts that you own. They do bar sharing account access with others, and bar circumventing rate limits. Using several accounts to get past a usage limit, including automatic rotation at the limit, falls under the circumvention clause as quoted. The docs point automated use toward API keys. A commentary piece makes the same point, but it is not OpenAI text (community). This is not legal advice. Check the live terms before shipping rotation.

## Other CLIs

- Gemini CLI. `GEMINI_CLI_HOME` is "the root directory for Gemini CLI's user-level configuration and storage", and the CLI creates a `.gemini` folder inside it (docs, [geminicli.com configuration reference](https://geminicli.com/docs/reference/configuration)). MCP servers go in `mcpServers` in `~/.gemini/settings.json` (user) and `.gemini/settings.json` (project). Whether `GEMINI_CLI_HOME` moves the user `settings.json` is inferred from the `.gemini` rule, not stated for `mcpServers`.
- Cursor CLI (`cursor-agent`). `CURSOR_CONFIG_DIR` sets the config directory. On Linux and BSD, `XDG_CONFIG_HOME` gives `$XDG_CONFIG_HOME/cursor/cli-config.json` (docs, [Cursor CLI configuration](https://cursor.com/docs/cli/reference/configuration)). The config page does not mention MCP. Forum reports put global MCP in `~/.cursor/mcp.json` and say project `.cursor/mcp.json` was not picked up in some versions (community, [forum thread](https://forum.cursor.com/t/cursor-agent-not-picking-up-mcp-json-from-project/157026)).
- Antigravity CLI (`agy`). Global MCP is `~/.gemini/config/mcp_config.json`, workspace MCP is `.agents/mcp_config.json`, and the top-level key is `mcpServers` (docs, [Antigravity MCP](https://antigravity.google/docs/mcp?tab=cli)). The relocation variable: not found.

## Design implications

Sharing and isolation are separate problems. Put the shared parts in one directory, keep the per-account parts in each account's home, and let the wrapper assemble each home before a run.

Per account, never shared: `auth.json` (or the keyring entry), `sessions/`, `session_index.jsonl`, the SQLite files, `log/`, `tmp/`, `cache/`, `installation_id`.

Shared, one copy: the base `config.toml` (MCP servers, skill settings, plugin enablement, model defaults), `skills/`, `plugins/`, and `AGENTS.md` if used.

Set `cli_auth_credentials_store = "file"` in the shared base config. Keyring entries may not be separated per home (not found), so file mode keeps each login in its own `auth.json`.

Keep secrets out of the shared file. `codex mcp add` with OAuth writes `client_secret` into `config.toml` (docs). Use `bearer_token_env_var` and environment variables for tokens.

Options for getting shared config into each home:

1. Copy-sync (documented paths only). Before each run, copy the shared `config.toml`, `skills/` and `plugins/` into the account home. Simple and deterministic. Config changes made by `codex` (for example `codex mcp add`) must be written back to the shared copy or they are lost.
2. Junction or symlink from each home to the shared files. Not documented (not found). General Windows behavior, not from a source: directory junctions usually need no admin rights, while file symlinks need Developer Mode or admin. Test that codex reads through them before relying on it.
3. Per-run `-c` overrides with `--ignore-user-config`. Documented flags, but complex `mcp_servers` tables passed through `-c` need a test.

Testing without a model call: `codex mcp list` and `codex login status` with `CODEX_HOME` pointing at a test folder show what config and login the process sees. Do not run `codex exec` with a prompt for this.

Rate limits and rotation:

- Read remaining quota from the app-server, per home (`usedPercent`, `resetsAt`), if the method works in the installed version. Test it first.
- On a failed turn, treat `turn.failed` with a usage or 429 message as "account exhausted until reset". Do not infer the limit from exit codes, which are undocumented.
- Automatic rotation to another account at the limit is the behavior the terms excerpts cover. Make it an explicit user choice, or do not ship it. Ask before implementing.

Process risks:

- The shared app-server daemon may mix homes. Test with two homes before running them in parallel.
- Do not symlink `sessions/` or the SQLite files between homes (community reports of broken resume).

## Sources

Accessed 2026-10-14.

Docs (fetched):
- https://learn.chatgpt.com/docs/config-file/config-advanced (from developers.openai.com/codex/config-advanced)
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://learn.chatgpt.com/docs/auth (from developers.openai.com/codex/auth)
- https://learn.chatgpt.com/docs/non-interactive-mode (from developers.openai.com/codex/noninteractive)
- https://learn.chatgpt.com/docs/extend/mcp?surface=cli (from developers.openai.com/codex/mcp)
- https://github.com/openai/codex/blob/main/docs/config.md (stub, links to docs)
- https://github.com/openai/codex/blob/main/docs/exec.md (stub, links to docs)

Source:
- https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs
- https://github.com/openai/codex/tree/main/codex-rs/app-server-protocol/schema/typescript/v2
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/app-server-protocol/schema/typescript/v2/RateLimitSnapshot.ts
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/app-server-protocol/schema/typescript/v2/RateLimitWindow.ts

Changelog:
- https://github.com/openai/codex/blob/main/CHANGELOG.md (only an Unreleased entry; points to the GitHub releases page for history). No entries found for the terms searched.

Local:
- `codex --version`, `codex --help`, `codex exec --help`, `codex login --help`, `codex mcp --help` (codex-cli 0.162.1).
- Names-only listing of `%USERPROFILE%\.codex`.

Community:
- https://github.com/openai/codex/issues/9135 (429 retry-limit report)
- https://github.com/openai/codex/issues/26034
- https://upd.dev/Dimillian/CodexMonitor/issues/66
- https://codex.danielvaughan.com/2026/04/13/codex-cli-session-management-resume-fork-transcripts
- https://codex.danielvaughan.com/2026/06/05/codex-cli-session-lifecycle-archive-resume-fork-compact-management/
- https://wikidocs.net/blog/@edwin/33022/
- https://forum.cursor.com/t/cursor-agent-not-picking-up-mcp-json-from-project/157026
- https://community.openai.com/t/codex-profiles-switch-codex-accounts-without-copying-auth-json/1380415

Tools:
- https://github.com/mincia1110/codex-switcher
- https://docs.rs/crate/prodex/0.2.94
- https://pypi.org/project/aicx/

Terms:
- https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan-usage-limits-and-troubleshooting (403, search excerpt only)
- https://openai.com/policies/terms-of-use/ (403, not used)
- https://openai.com/en-GB/policies/row-terms-of-use/ (search excerpt only)
- https://cdn.openai.com/osa/openai-services-agreement.pdf (search excerpt only)
- https://open.windriver.com/info/uni-license-list/licenses/openai-tou-20241211.html (mirror of December 2024 terms)

Other CLIs:
- https://geminicli.com/docs/reference/configuration
- https://cursor.com/docs/cli/reference/configuration
- https://antigravity.google/docs/mcp?tab=cli
