# Lifecycle and sharing: what other projects do

Accessed 2026-10-14. Everything below comes from source files fetched from raw.githubusercontent.com or the GitHub API, plus the locally installed Codex plugin (`~/.claude/plugins/cache/openai-codex/codex/1.0.6/`). Nothing was cloned, installed or run, so "works while the host is idle" statements say whether the code or its author claims it, not that we tested it. Line numbers are from the raw files read on that date (for the one very large file, `clauth/src/runtime.rs`, they are the numbers printed by grep). Fetched content was treated as data.

Our side was read in `src/cli_subagents/` (sync.py, runtimes/claude.py, runtimes/codex.py, messages.py, tools.py, hosts.py, runner.py, fsutil.py) and `docs/design.md`.

## A. Sharing tools and config across accounts of the same CLI

### uwuclxdy/clauth (MIT, Rust, branch `mommy`)

Mechanism: every `clauth start <profile>` gets its own `CLAUDE_CONFIG_DIR` tree, `~/.clauth/profiles/<profile>/runtime-<sid>/` (`src/runtime.rs:1-66`).

* Shared: every top-level entry of `~/.claude/` except `settings.json` and `.credentials.json` (`runtime.rs:4792-4803`, walk at `4820-4885`). That includes `projects/`, `todos/`, `commands/`, `plugins/`, `hooks/`, `history.jsonl`, and `CLAUDE.md` and `skills/` (the probe in `link_mode_of`, `runtime.rs:122-131`, looks at those two). The tree is a forest of symlinks into `~/.claude/`.
* Per profile: `.credentials.json` (symlink to the profile's own credential store), `settings.json` (built from the shared base plus profile overrides, `write_merged_settings`), and `.claude.json` (own copy seeded from `~/.claude.json`).
* `.claude.json` sync (`src/claude_json.rs:1-119`): a background tick treats the home file and every runtime copy as members. The newest parseable file wins for shared fields and is written to every other member; each member keeps its own copy of `PER_PROFILE_FIELDS` (`claude_json.rs:34-85`: `oauthAccount`, usage and billing caches, feature-flag caches, model-access caches, `primaryApiKey`, `customApiKeyResponses`). The reason is stated in the header (`claude_json.rs:3-11`): Claude Code trusts the cached `oauthAccount` and does not re-derive it from the token, so one shared file leaks identity.
* `settings.json` sync (`src/settings_sync.rs:1-50`, `70-130`): same engine (`src/jsonsync.rs`). Per-profile keys: `apiKeyHelper`, `proxyAuthHelper`, `awsCredentialExport`, `awsAuthRefresh`, `gcpAuthRefresh`, `otelHeadersHelper`, `forceLoginMethod`, `forceLoginGatewayUrl`, `forceLoginOrgUUID`, `model`; `env` is merged key by key.
* Concurrent writes: Claude Code rewrites these files in place, so clauth parses first and skips a file that does not parse (caught mid-write), writes only on change, and uses atomic writes (`claude_json.rs:15-21`). A per-session liveness marker holds a `flock` for the session's life (`runtime.rs:42-52`). Staging files from a publish in flight are skipped by name (`runtime.rs:4850-4860`).
* Windows: `runtime.rs:4690-4730` probes by trying a real `symlink_file` at acquire time. If it fails (no developer mode, no admin), it falls to "fake" mode: a recursive copy with a watchdog that mirrors both ways by newest mtime (`runtime.rs:37-46`). In fake mode all shared sessions of a profile share one tree, so they cannot have independent credentials (`runtime.rs:22-26`). `unlink_link` (`runtime.rs` near 4900) documents that on Windows `remove_file` fails with error 5 on a live junction or directory symlink and `remove_dir` removes the link without touching the target. Dangling links are pruned before each rebuild (`prune_dangling_links`).
* `--isolated`: nothing linked from `~/.claude/` (`runtime.rs:4862-4870`), so a throwaway Claude cannot clean the operator's `projects/` under its own `cleanupPeriodDays`.
* MCP servers are not translated: they travel inside the synced `.claude.json`.

### Dicklesworthstone/coding_agent_account_manager (caam, Go)

* "Shallow profile" (`cmd/caam/cmd/shallow.go:20-70`, `internal/shallow/shallow.go:1-120`): a per-identity `HOME` directory. Only the identity files are real; everything else is a symlink back to the real home. Claude: `.claude/.credentials.json`, `.claude/.credentials.lock`, `.claude/settings.json`, `.claude.json` real; `projects/`, `todos/`, `.bashrc`, `.gitconfig`, `.ssh` linked (`shallow.go:145`). The lock file is kept real so two sessions do not serialize on one flock (`shallow.go:28-36`). Codex: `.codex/auth.json` and `config.toml` real, `CODEX_HOME` pinned. agy: the antigravity token files real, `GEMINI_HOME` pinned.
* Skills: `RepairSkillShare` (`shallow.go:101-120`) re-links the skills directory on every spawn because Codex creates its own real `skills/` inside the profile and hides the shared one.
* Claude `.claude.json` is seeded from the real home with account keys stripped (`internal/shallow/claude_config.go:1-60`). `claudesettings.CheckPrivate` (`internal/claudesettings/isolated.go:20-60`) refuses a profile settings file that is a symlink or aliases the shared file, because reading through the link would read another account's auth.
* Codex `config.toml` merge (`internal/shallow/codex_config.go:15-60`, `152-345`): three classes of key. caam forces `cli_auth_credentials_store = "file"`. Profile-local and never overwritten: `[hooks.state.*]`, `[projects.*]`, `[notice.*]` (`codex_config.go:70-72`). Everything else follows the real config, with whole tables (`[mcp_servers.x]` and its subtables) replaced as a unit so a stale `command` cannot survive next to a new `url` (the bug behind issue 103, per the comment). Nothing is deleted. The edit is a text splice that keeps comments and order.
* Windows: `isolated_source_windows.go` and `backup_rename_windows.go` exist; I did not find a junction path in the files read. Not verified.
* Concurrency: per-identity lock file, atomic file writes (`writeFileAtomic`).

### realiti4/claude-swap (Python)

* Two modes. The default `cswap switch` swaps credentials in place. `cswap run` (`src/claude_swap/session.py:1-30`) launches Claude with `CLAUDE_CONFIG_DIR` pointing at a persistent per-account profile under `<backup_dir>/sessions/<num>-<email-slug>/`.
* Shared (`session.py:76-83`): `settings.json`, `keybindings.json`, `CLAUDE.md`, `skills`, `commands`, `agents`, plus a mirror of user-scope `mcpServers` (`_sync_mcp_servers`, `session.py:1195`, taking Claude's config lock only when it writes). Opt-in `--share-history` links `projects/` and `history.jsonl` (`session.py:85-88`).
* How: symlinks on macOS and Linux; copies re-synced on every launch on Windows (`session.py:20-24`, `1122`). History is not shared on Windows because a copy would fork it. A manifest `.cswap-shared.json` records what it created so removal never touches user data (`session.py:90-93`).
* It links to the resolved target when the source is itself a symlink, because Claude Code's atomic settings write would otherwise replace a link-to-a-link with a plain file (`session.py:1141-1146`).
* Locks: `claude_locks.py:1-35` follows Claude Code's own `proper-lockfile` protocol (directory locks `.oauth_refresh.lock` and `~/.claude.lock`, stale after 60 s, `~/.claude.json.lock` stale after 10 s) while swapping credentials. Its own `locking.py` uses `msvcrt` on Windows and `fcntl` elsewhere.

### kaitranntt/ccs (TypeScript)

* Layout: `~/.ccs/shared/*` symlinks to `~/.claude/*`, and per-instance links from `~/.ccs/instances/<profile>/` into `shared` (`src/management/shared-manager/shared-dir-linker.ts:1-20`).
* Shared items (`shared-manager/types.ts:51-69`): directories `commands`, `skills`, `agents`, `plugins`; files `settings.json` (policy `newer-wins`) and `CLAUDE.md` (policy `canonical-first`). Plugin internals `cache`, `marketplaces`, `installed_plugins.json` are linked as-is, with registry path normalization.
* Project memory: instance `projects/<p>/memory/` is moved to `~/.ccs/shared/memory/<p>/` and linked back (`shared-manager/project-memory-sync.ts:1-50`), with conflict copies when merging.
* MCP servers: only the `mcpServers` key is copied from the global `~/.claude.json` into the instance `.claude.json`; instance entries win except names CCS manages (`src/management/instance-manager.ts:204-250`).
* Windows: `symlink(..., 'junction')` with `path.resolve` targets; on failure a recursive copy plus a warning to enable Developer Mode (`shared-manager/symlink-helpers.ts:69-86, 125-137`; also `shared-dir-linker.ts:215, 284`).
* Per-profile `shared_resource_mode` of `shared` or `profile-local` (`src/auth/shared-resource-policy.ts`).
* Concurrent writes: a per-profile sync lock with pid and nonce, stale detection by `process.kill(pid, 0)` (`src/management/profile-context-sync-lock.ts:1-40`).

### What this means for us

Our design (`docs/design.md`, `sync.py`, `runtimes/claude.py`) differs from all four in one useful way. We do not write the account's `.claude.json` or `settings.json` at all. MCP servers go in through `--mcp-config`, settings through `--settings`, plugins through `--plugin-dir`, and only skills, agents, commands, rules and CLAUDE.md are linked or copied, tracked in a manifest. That removes the whole class of concurrent in-place writes and the identity-leak problem that drives clauth's 50-key per-profile list. The sync code already has the safety points the others had to learn: junction with copy fallback, `rmdir` for junctions (`fsutil.py:168-176`), hard link with copy fallback and re-link after an atomic save (`sync.py:253-283`), no deleting of entries the account owns, and a lock around sync.

Gaps found:

1. Our `DROP_SETTINGS` (`runtimes/claude.py:57-65`) lacks four keys that clauth treats as per-account: `proxyAuthHelper`, `gcpAuthRefresh`, `otelHeadersHelper`, `forceLoginGatewayUrl`.
2. Our Codex `_write_config` (`runtimes/codex.py:244-262`) copies the primary `config.toml` over the account's file on every run, so anything Codex wrote into the account's file (`[projects.*]` trust, `[hooks.state.*]`, `[notice.*]`) is lost each run. This is an inference from the code; I did not run Codex to see what it does when trust is missing.

## B. Subagent lifecycle UX

### OpenAI Codex plugin for Claude Code (local copy 1.0.6)

* Start: `/codex:rescue` forwards to a thin `codex:codex-rescue` Agent whose only tool is Bash and whose only job is one `codex-companion.mjs task ...` call (`agents/codex-rescue.md`, `commands/rescue.md`). `--background` runs that wrapper agent in the background, so completion reaches the main session through Claude Code's own "background agent finished" notification. The helper itself has `task --background` that spawns a detached `task-worker` and returns a job id (`scripts/codex-companion.mjs:673-688`).
* Progress: `status` renders a table; `status <job> --wait` polls every 2 s up to 240 s (`codex-companion.mjs:69-70, 318-330`). Jobs carry a `phase` field (`lib/tracked-jobs.mjs`). `result` and `cancel` are separate commands.
* Hooks (`hooks/hooks.json`): SessionStart (exports session id and transcript path via `CLAUDE_ENV_FILE`), SessionEnd (kills the session's running jobs and the broker, `scripts/session-lifecycle-hook.mjs:42-75, 104`), and Stop (`scripts/stop-review-gate-hook.mjs`). The Stop hook only warns on stderr that a task is still running (`stop-review-gate-hook.mjs:148-157`), unless the optional review gate is on, in which case it blocks the stop with `{"decision":"block"}`. There is no completion hook and no PostToolUse hook.
* Two-way messaging: none. The worker cannot ask the parent anything.
* Idle host: a background wrapper agent notifies when it finishes; nothing else wakes the host.
* Extra: `commands/transfer.md` plus `scripts/lib/claude-session-transfer.mjs` import the current Claude transcript (path passed via env) into a resumable Codex thread.

### uwuclxdy/clauth

* Background delegate returns a `job_id`; `monitor` collects (`src/mcp/mod.rs:1123, 1795`). Job files: one JSON per job, claim-by-rename to `<id>.json.claim`, and a `<id>.json.delivered` ledger, so exactly one delivery path wins (`src/mcp/jobs.rs:672-691, 787-792`). Done records are garbage-collected (`jobs.rs:1002`).
* Progress: a 2-second heartbeat is written atomically to the job file with `last_output_at` and the output tail (`mod.rs:69-75, 3309-3370, 4935-4969`; `jobs.rs:475`), and the same cadence drives MCP `notifications/progress` while `monitor` or a foreground delegate waits (`mod.rs:1964-1990, 1992-2105`).
* Completion push: `plugins/hooks/hooks.json:29-45` registers a PostToolUse hook on the delegate tool with `"asyncRewake": true`, `"timeout": 4260`, and a `rewakeMessage`. The hook (`clauth mcp-await-job`, `mod.rs:5743-5790`) reads the hook payload from stdin, pulls job ids out of `tool_response` (`mod.rs:5830-5860`), polls the job files every 200 ms for up to 4200 s (`mod.rs:2823-2838`, `5798-5835`), claims each finished job, prints its result envelope, and exits with code 2, which wakes the model. If the deadline passes it still exits 2 with a note to call `monitor`. The code comment says the deadline is a delivery window, not a bound on the job, and that the hook's own limit (4260 s) must stay above it because a kill delivers nothing (`mod.rs:2825-2838`).
* Resume and compaction: a SessionStart hook (`src/hook_jobs.rs:29-76`) names finished-but-uncollected background jobs when the source is `resume` or `compact`. It matches strictly on the host session id and names at most 10 jobs. Reason (`hook_jobs.rs:6-14`): the completion hook is bound to the session that started the job, so a result left uncollected when that session exited has no other way back.
* Idle host: the author relies on `asyncRewake` exit 2 to deliver while the main session is idle. The hook runs after the tool call returns, so it keeps waiting after the turn ends. I found no official Claude Code documentation for `asyncRewake` (a web search returned only issue threads, for example an April 2026 issue about asyncRewake output rendering as a visible system reminder, closed 2026-04-11). Treat it as working but undocumented.
* Two-way messaging: none from worker to parent.

### Dicklesworthstone/mcp_agent_mail (Python, FastMCP over HTTP)

* Mechanism: a server with SQLite and git-backed mailboxes; tools `send_message`, `reply_message`, `fetch_inbox`, `mark_message_read`, `acknowledge_message` (`src/mcp_agent_mail/app.py:7132, 8527, 9592, 9843, 9921`).
* Delivery to an agent: a PostToolUse hook runs `scripts/hooks/check_inbox.sh` (`.claude/settings.json`). It is rate limited by a temp file (default 120 s), asks `fetch_inbox` with `unread_only=true` over curl, and stays silent when there is no mail (`check_inbox.sh:29-36, 66-80, 92-100`). With `AGENT_MAIL_HOOK_FORMAT=json` it prints a `hookSpecificOutput.additionalContext` envelope. The script's own comment says plain PostToolUse stdout reaches only the human terminal, and only that envelope reaches the agent (`check_inbox.sh:31-36, 167-172`).
* Codex: `scripts/hooks/codex_notify.sh:1-9` is marked deprecated because Codex's `notify` program gets stdin, stdout and stderr redirected to /dev/null, so its output cannot reach the model; the project moved to a Codex PostToolUse hook.
* Idle host: no. The hook fires only after a tool call, so an idle agent sees nothing until it acts.

### Other projects found

* Claude Code channels (official docs, `code.claude.com/docs/en/channels-reference.md`): an MCP server declares `experimental["claude/channel"]` and sends `notifications/claude/channel`; the text arrives in the session as a `<channel>` tag. Two-way works through an ordinary reply tool. Events that arrive while Claude is busy are delivered together on the next turn. Limits: research preview, needs Claude Code 2.1.80 or later, custom channels need `--dangerously-load-development-channels` (a confirmation dialog), the flag is ignored under `-p` and the Agent SDK, Team and Enterprise need an admin setting, and events are dropped silently if the session did not load the server as a channel. This is a real push path for an idle host, but not usable as a default for a plugin today.
* `@junghanacs/pi-shell-acp` `scripts/raw-async-delivery/README.md` (third party): a plugin SessionStart hook emits `watchPaths` for `<session_id>/inbox.signal`; a `FileChanged` hook with `asyncRewake` fires when a sender touches the file and wakes an idle session in 1 to 2 seconds. The author says this was verified on Claude Code 2.1.163. Stop hooks deliver only at the next turn boundary and cannot wake an idle session. Doorbell messages get a forced prefix and `rewakeMessage` is ignored; imperative wording gets treated as prompt injection. For Codex it needs `codex app-server --listen unix://...` and a `turn/start` over WebSocket; a plain embedded TUI cannot be addressed.
* `PeterHiroshi/bg-dispatch` (skill, read via a page summary only): Claude Code Stop hook runs `on-complete.sh` then `notify.sh`; a watchdog waits 35 s for the hook and sends the notification itself if the hook did not fire; `meta.json` flags prevent double notification. I did not read its source.
* Discovery only, no source read: mcp-talk (devinvenable), mcp-dispatch, Interagent, InterAgentMail, claude-presence (a UserPromptSubmit hook that emits `systemMessage` so a banner shows above the reply).

### Comparison with ours

| | Completion push | Progress | Worker to parent | Parent to worker | Idle host |
|---|---|---|---|---|---|
| Ours | none; `subagent_wait` blocks and relies on Claude Code moving long MCP calls to the background | MCP progress ping every 15 s with a fixed message (`tools.py:16, 219-246`) | `message_parent`, `ask_parent` (blocks with pings), files in the job folder | `subagent_message`; worker sees it only when it calls `check_parent_messages` or while blocked in `ask_parent` | only if the model had already called `subagent_wait` and that call was backgrounded |
| Codex plugin | wrapper Agent in background | status table, `--wait` poll | none | resume thread | yes via Claude's own agent notification |
| clauth | PostToolUse `asyncRewake` hook | 2 s heartbeat with tail | none | none | yes (claimed; undocumented hook) |
| mcp_agent_mail | none | none | mail tools | PostToolUse reminder | no |

Our message store (`messages.py`) is already close to what mcp_agent_mail does, with less machinery (JSONL files, per-reader seen lists, file locks, `reply_to` linking). Our blocking `ask_parent` with progress pings is something none of them have.

## C. Passing the parent's context to a worker in a different CLI

I found no project that tells a worker in another CLI about the parent's CLAUDE.md, AGENTS.md or skills.

* pal-mcp-server clink (`tools/clink.py:186-215`, `clink/agents/base.py`): the worker gets role system-prompt text and referenced file contents. It does not mention the parent's instruction files or skills.
* caam, clauth, ccs, claude-swap: all work within one CLI. Sharing is by linking the same CLI's own files (CLAUDE.md, skills, agents) so each worker finds them natively. No cross-CLI mapping.
* codex-plugin-cc: `session-lifecycle-hook.mjs:77-81` exports the Claude transcript path so `/codex:transfer` can import the whole Claude session into Codex. That moves conversation history, not instruction files, and is a user command rather than something every delegation does.
* Our `hosts.py` (`host_context`, `parent_note`) already does something none of these do: it names the existing instruction files and skill folders of the host and tells the worker to read them. It is cheap and has no translation step.

MCP config translation between CLIs exists only in config-sync tools:

* dyoshikawa/rulesync (MIT) `src/features/mcp/codexcli-mcp.ts` (1057 lines) converts a canonical MCP JSON into Codex `config.toml` tables. Field maps at lines 33-50 and 85-92: `enabledTools` to `enabled_tools`, `disabledTools` to `disabled_tools`, `envVars` to `env_vars`, `timeout` to `tool_timeout_sec`, `networkTimeout` to `startup_timeout_sec`. Transport is inferred by Codex from `command` versus `url`, so `type` is dropped (`restateCanonicalTransport`, around line 610). `headers` becomes `http_headers`, but headers on a stdio server are dropped with a warning (`485-503`). Codex does not expand `${VAR}`, so `Authorization: Bearer ${VAR}` becomes `bearer_token_env_var` and a header whose whole value is `${VAR}` becomes `env_http_headers.<name>` (`516-600`); any other `${...}` stays literal with a warning. Server names must match `[a-zA-Z0-9_-]+`, otherwise a stable hashed `mcp_<hash>` name is used (`314-335`). A server with an empty command array and no url is skipped because Codex would reject the whole file (`284-310`). Codex OAuth keys are validated (`147-260`).
* intellectronica/ruler (MIT) `src/agents/CodexCliAgent.ts:13-160` writes only `command`, `url` and `env` into `[mcp_servers]` and can overwrite the whole section. It is much lossier than rulesync (no headers, no timeouts).

Our Claude-to-Codex worker path today starts the Codex worker with the Codex primary config, so MCP servers defined in Claude's `~/.claude.json` are not visible to it.

## Recommendations for CLI Subagents

At most 12. Each names where it comes from, what to change, and why. The first four matter most.

1. **Wake the idle main session when a job ends or asks a question.** From clauth `plugins/hooks/hooks.json:29-45`, `src/mcp/mod.rs:5743-5790, 5798-5835, 2823-2838`. Add `hooks/hooks.json` to the Claude plugin with a PostToolUse hook, matcher on `subagent_start`, `asyncRewake: true`, timeout a little above the wait window. New `csa hook await` command: read the payload from stdin, take the `job_id` (or ids) from `tool_response`, poll `status.json` every 200 ms to 1 s, and exit 2 after printing the result summary. Also exit 2 when `messages.has_new_for_host` is true or `open_questions` is non-empty, so a worker question wakes the host (clauth only wakes on completion). On deadline, print "still running, call subagent_wait" and exit 2. Why better: today the host only learns anything if the model chose to call `subagent_wait` and Claude Code backgrounded it. Keep `subagent_wait` for hosts without the hook. Caveat: `asyncRewake` is undocumented upstream; ship it behind a plugin version check and keep the tool-based path.

2. **One delivery per result.** From clauth `src/mcp/jobs.rs:672-691, 787-792`. Add a claim step in `jobs.py` (rename `status.json` marker or write `delivered.json` with who delivered and when). The hook claims before printing; `subagent_result` and `subagent_wait` report "already delivered by hook" instead of the full text again, or still return it when asked by id. Why: with change 1 the same result would otherwise reach the model twice and cost context.

3. **Name uncollected finished jobs after resume or compaction.** From clauth `src/hook_jobs.rs:29-76`. Have change 1's hook store the hook payload's `session_id` in the job spec. Add a SessionStart hook for `resume` and `compact` that lists done, unread jobs for that exact session id (cap 10, rest counted). Why: a result that finishes after the host session ended otherwise has no way back; our jobs deliberately outlive the host, unlike the Codex plugin, which kills them at SessionEnd (`session-lifecycle-hook.mjs:42-75`).

4. **Inject follow-ups into the worker without relying on it to poll.** From mcp_agent_mail `scripts/hooks/check_inbox.sh:29-36, 160-172`. In `ClaudeRuntime._overlay_settings` add a PostToolUse hook to the worker's `--settings` overlay that runs `csa hook inbox` with `CSA_JOB_ID` in the environment. It reads `inbox.jsonl` (a local file stat, so no rate limit needed), and when something is unread it prints a `hookSpecificOutput.additionalContext` JSON envelope and marks it seen. Why: now `subagent_message` reaches a worker only if the worker calls `check_parent_messages`. Use the JSON envelope, not plain stdout (the script comment says plain stdout does not reach the model). Codex workers: only if Codex's PostToolUse hook format is confirmed; mcp_agent_mail's Codex integration uses one, but I read only its deprecation note.

5. **Make progress pings say something.** From clauth `src/mcp/mod.rs:69-75, 1964-1990, 4949-4969`. In `tools.wait_any` and the status wait loop (`tools.py:219-246`), build the ping message from `status["progress"]` (last tool name, turn count, last output line) and send every 2 to 5 seconds instead of "waiting for N subagent job(s)" every 15 s. `runner.py:281` already stores `parser.progress()`. Why: the user sees what the worker is doing; clauth caps this at one per 2 s because Claude Code renders progress on a roughly 700 ms throttle.

6. **Merge Codex `config.toml` by section instead of overwriting it.** From caam `internal/shallow/codex_config.go:15-60, 70-72, 152-345`. Replace the whole-file write in `CodexRuntime._write_config` (`runtimes/codex.py:244-262`) with: keep the account's `[projects.*]`, `[hooks.state.*]`, `[notice.*]` blocks; take every other table and root key from the primary, replacing a table together with its subtables; never delete tables only the account has; splice text so comments and order survive. Keep `strip_own_server` and the `file` credential-store override. Why: the current file is regenerated every run and loses account-local trust and notice state (inference, untested). The "replace tables as a unit" rule prevents the stale `command` next to new `url` error caam hit.

7. **Add the four missing per-account settings keys to the Claude overlay filter.** From clauth `src/settings_sync.rs:70-130`. Add `proxyAuthHelper`, `gcpAuthRefresh`, `otelHeadersHelper`, `forceLoginGatewayUrl` to `DROP_SETTINGS` in `runtimes/claude.py:57-65`. Why: those helpers and the gateway URL choose how an account authenticates or where it sends traffic, the same reason `apiKeyHelper` is already dropped. Do not copy clauth's per-profile `model`; we set the model per job.

8. **Translate the parent's MCP servers for a Codex worker, as an opt-in.** From rulesync `src/features/mcp/codexcli-mcp.ts:33-50, 85-92, 284-335, 485-600`. New `mcp_translate.py` that takes the Claude-side `mcpServers` we already read (`ClaudeRuntime._mcp_servers`) and emits Codex tables: drop `type`, `headers` to `http_headers` (stdio: drop and warn), `Bearer ${VAR}` to `bearer_token_env_var`, whole-value `${VAR}` to `env_http_headers`, sanitize names to `[a-zA-Z0-9_-]` with a hash fallback, skip servers with an empty command and no url, skip our own server. Apply with `-c mcp_servers.<name>.<key>=...` overrides on the worker command (as `codex.py:97-101` already does for our own server) so no account file is edited. Config key `share_parent_mcp = false` by default; `csa doctor` lists servers skipped and why. Why: today a Codex worker started from Claude Code has none of the user's Claude-side MCP servers. Both projects are MIT; reimplement rather than copy, and note the source in a comment.

9. **Optional `csa-runner` agent for hosts or users who cannot use the hook.** From the Codex plugin `agents/codex-rescue.md`, `commands/rescue.md`. Ship a Bash-only forwarder agent that runs one `csa run --wait` call; start it with the Agent tool in the background to get Claude Code's own finished notification. Untested: the Bash call inside a background agent is still subject to Bash timeouts. Lower priority than change 1; build only if change 1 proves unreliable. It costs one extra model turn per job.

10. **Document the channels option and revisit it.** From the channels reference. Add a short section in `docs/design.md` stating that `claude/channel` would give real two-way push (worker question arrives as a `<channel>` tag, answered via an MCP tool) but needs a development flag, interactive sessions only and an admin setting on Team and Enterprise today. Revisit when channels leave research preview and our server can be allowlisted. No code now.

11. **Doctor check for a link-free Windows account folder.** From clauth `runtime.rs:4690-4730` and ccs `symlink-helpers.ts:76-86`. We already fall back from junction to copy, but when `make_dir_link` returns `"copy"` for `skills` or `agents`, the copy goes stale until the next sync. `csa doctor` should report which entries are copies and that they refresh on the next job. Small, no behaviour change.

12. **Keep the manifest-guarded removal rule and add a test for the history case.** From claude-swap `session.py:90-93, 1107-1119`: a stale manifest must never be able to delete real conversation history, so removal there only unlinks symlinks. Our `_remove` already checks that a copy still matches what was copied; add a test that a manifest entry pointing at `projects/` or a `memory` folder is never removed unless it is a link. Why: `_share_memory` links project memory into the account, and a bug there would delete user data.

### Where ours is already as good or better, so do not churn

* Not writing the account's `.claude.json` or `settings.json` (flags instead of file sync). clauth needs a 50-key identity list, a background reconciler and "newest file wins" to do what we avoid by not writing. Do not move to symlinked `settings.json` (ccs, claude-swap): it shares `apiKeyHelper` and writes through to the primary.
* Junction on Windows with copy fallback, `rmdir` for junction removal, hard link for CLAUDE.md with re-link after atomic save, and the manifest for removals. These match or exceed ccs, claude-swap and clauth.
* Project memory shared by linking the account's memory folder to the primary's (`_share_memory`). ccs moves the data into a new shared root; ours moves nothing.
* The message store in `messages.py` and the blocking `ask_parent` with pings. No inspected project has worker-to-parent questions with replies.
* `hosts.py` parent note for cross-CLI workers. No project found does anything comparable; the ones that translate config do it only for MCP servers, not instructions or skills.
* Not killing jobs at host exit. The Codex plugin kills them at SessionEnd; ours survive, which is why change 3 is needed.
* We do not copy caam's whole-HOME redirect (`HOME` swap). It shares shell and git state we do not need and requires linking more of the user's home.

## Sources

All accessed 2026-10-14.

* uwuclxdy/clauth, branch `mommy`: `src/claude_json.rs`, `src/runtime.rs`, `src/settings_sync.rs`, `src/jsonsync.rs`, `src/claude.rs`, `src/hook_jobs.rs`, `src/mcp/mod.rs`, `src/mcp/jobs.rs`, `plugins/hooks/hooks.json` via https://raw.githubusercontent.com/uwuclxdy/clauth/mommy/
* Dicklesworthstone/coding_agent_account_manager, branch `main`: `cmd/caam/cmd/shallow.go`, `internal/shallow/{shallow,claude_config,codex_config}.go`, `internal/claudesettings/isolated.go` via https://raw.githubusercontent.com/Dicklesworthstone/coding_agent_account_manager/main/
* realiti4/claude-swap, branch `main`: `src/claude_swap/{session,claude_locks,locking}.py` via https://raw.githubusercontent.com/realiti4/claude-swap/main/
* kaitranntt/ccs, branch `main`: `src/management/instance-manager.ts`, `src/management/shared-manager/{types,shared-dir-linker,symlink-helpers,project-memory-sync}.ts`, `src/management/profile-context-sync-lock.ts`, `src/auth/shared-resource-policy.ts` via https://raw.githubusercontent.com/kaitranntt/ccs/main/
* Dicklesworthstone/mcp_agent_mail, branch `main`: `scripts/hooks/check_inbox.sh`, `scripts/hooks/codex_notify.sh`, `.claude/settings.json`, `src/mcp_agent_mail/app.py` via https://raw.githubusercontent.com/Dicklesworthstone/mcp_agent_mail/main/
* dyoshikawa/rulesync `src/features/mcp/codexcli-mcp.ts` and intellectronica/ruler `src/agents/CodexCliAgent.ts`, branch `main`, via raw.githubusercontent.com
* BeehiveInnovations/pal-mcp-server `tools/clink.py`, `clink/agents/base.py`, via raw.githubusercontent.com
* Local: `~/.claude/plugins/cache/openai-codex/codex/1.0.6/` (`hooks/hooks.json`, `scripts/codex-companion.mjs`, `scripts/session-lifecycle-hook.mjs`, `scripts/stop-review-gate-hook.mjs`, `scripts/lib/claude-session-transfer.mjs`, `scripts/lib/tracked-jobs.mjs`, `commands/rescue.md`, `commands/status.md`, `commands/transfer.md`, `agents/codex-rescue.md`)
* Claude Code channels reference: https://code.claude.com/docs/en/channels-reference.md
* pi-shell-acp raw-async-delivery README: https://cdn.jsdelivr.net/npm/@junghanacs/pi-shell-acp@0.11.1/scripts/raw-async-delivery/README.md
* bg-dispatch (page summary only): https://skills.cat/skills/peterhiroshi/bg-dispatch and https://github.com/PeterHiroshi/bg-dispatch
* asyncRewake issues (search results only): https://claudeissues.com/issue/44881-asyncrewake-hook-output-renders-as-visible-system-reminder-in-terminal, https://claudeissues.com/issue/20953-bug-async-hooks-fail-to-notify-claude-code-of-completion, https://github.com/anthropics/claude-code/issues/28229
* Messaging-server discovery listings (not read in source): https://glama.ai/mcp/servers/@devinvenable/mcp-talk, https://glama.ai/mcp/servers/cerberusgamelabs/cgl-interagentmail
