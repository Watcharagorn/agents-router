# Prior art for CLI Subagents

Observed 2026-10-14. It supersedes the competitor notes from the project's first research pass.

## Scope and method

CLI Subagents lets the main session of a coding-agent host start subagents that run in other installed agent CLIs. Each worker runs headless in its own native harness under the user's own login. The project does not translate model APIs and does not relay OAuth tokens. The first use case is Claude Code delegating to `claude -p` processes under different Claude accounts.

Stars and last-push dates come from the GitHub API on the observation date. "Last push" is the API `pushed_at` value, used as a proxy for last commit. Nothing was cloned, installed or run. Every behavioural claim comes from reading files or documentation, and each project carries an evidence label.

* Source read: the listed files were fetched and read. This is not a full audit, and the unread parts are named.
* Docs read: README or manifest only.
* Discovery only: found in search results; no behavioural claim is made.

Line numbers come from raw files fetched on the observation date. Where a reader saw only a summary of a file, the numbers are marked approximate ("~"). Windows statements marked "inference" come from reading code, not from running it.

Transport categories used below: MCP stdio tools, host plugin plus Bash helper, ACP, PTY/terminal emulation, app-server/JSON-RPC, SDK.

## Summary table

| Project | License | Stars | Last push | Language | Transport | Evidence |
|---|---|---|---|---|---|---|
| openai/codex-plugin-cc | Apache-2.0 | 34.2k | 2026-07-08 | JavaScript | Host plugin + Bash helper, over app-server JSON-RPC | Source read |
| uwuclxdy/clauth | MIT | 242 | 2026-10-14 | Rust | MCP stdio (plus TUI and daemon) | Source read, partial |
| shinpr/sub-agents-mcp | MIT | 98 | 2026-10-14 | TypeScript | MCP stdio | Source read, partial |
| mkXultra/ai-cli-mcp | MIT | 27 | 2026-09-30 | TypeScript | MCP stdio | Source read, partial |
| SinanTufekci/agent-intern | MIT | 27 | 2026-10-14 | Python | MCP stdio | Source read, partial |
| BeehiveInnovations/pal-mcp-server (clink) | Apache-2.0 (API says NOASSERTION) | 11.8k | 2025-12-15 | Python | MCP stdio | Source read, partial |
| swjturay/codex-agy-delegator | MIT | 4 | 2026-07-25 | TypeScript | MCP stdio, detached runner | Source read, partial |
| alessai/Hive-MCP | MIT | 2 | 2026-05-10 | TypeScript | MCP stdio | Source read |
| thomaswitt/mcp-agents | README says MIT, no LICENSE file | 19 | 2026-10-14 | JavaScript | MCP stdio or loopback HTTP; Codex via app-server | Source read, partial |
| yejianfei/subagent-cli | Apache-2.0 | 0 | 2026-08-01 | TypeScript | PTY/terminal emulation behind a daemon | Source read, partial |
| BloopAI/vibe-kanban | Apache-2.0 | 28.4k | 2026-09-19 | Rust | Stream-json control protocol, app-server style | Source read, partial |
| steipete/claude-code-mcp | MIT | 1.3k | 2026-05-15 (archived) | TypeScript | MCP stdio | Source read, one file |
| tuannvm/codex-mcp-server | None | 643 | 2026-05-25 | TypeScript | MCP stdio | Source read, partial |
| jamubc/gemini-mcp-tool | NOASSERTION | 2.3k | 2026-07-21 | TypeScript | MCP stdio | Source read, one file |
| coder/agentapi | MIT | 1.5k | 2026-09-13 (deprecated) | Go | PTY/terminal emulation, HTTP and SSE; ACP path | Source read, partial |
| Oortonaut/mcacp | Apache-2.0 | 11 | 2026-02-01 | TypeScript | MCP stdio outward, ACP inward | Source read, partial |
| zed-industries/claude-agent-acp | Apache-2.0 | 2.6k | 2026-10-14 | TypeScript | ACP agent over the Agent SDK | Docs read |
| zed-industries/codex-acp | NOASSERTION | 871 | 2026-07-22 | Rust | ACP agent | Docs read |

Projects from the earlier research that were not re-inspected, and the extra leads found, are in the last section.

## Part 1. Projects from the earlier research

### openai/codex-plugin-cc

https://github.com/openai/codex-plugin-cc. Apache-2.0, 34.2k stars, last push 2026-07-08, JavaScript. Transport: Claude Code plugin whose commands and agent call a Bash helper (`scripts/codex-companion.mjs`). The helper drives Codex through `codex app-server` JSON-RPC over stdio, optionally through a shared broker on a named pipe (Windows) or unix socket (elsewhere) (`lib/broker-endpoint.mjs:11-16`). Evidence: source read of `codex-companion.mjs`, `lib/app-server.mjs`, `lib/codex.mjs`, `lib/state.mjs`, `lib/process.mjs`, `lib/broker-endpoint.mjs`, `lib/broker-lifecycle.mjs`, `app-server-broker.mjs`, `agents/codex-rescue.md`, mostly in part. Not read: render, workspace, hooks, prompts, tests. Paths below are under `plugins/codex/scripts/`.

* Spawn: `spawn("codex", ["app-server"], { stdio: ["pipe","pipe","pipe"], shell: process.platform === "win32" ? (process.env.SHELL || true) : false, windowsHide: true })` (`lib/app-server.mjs:190-196`). It does not use `codex exec`.
* Protocol calls: `thread/start` (`lib/codex.mjs:62-72`), `thread/resume` (`75-83`), `turn/start` (`1136-1142`).
* Output: JSON-RPC lines on stdout (`app-server.mjs:221-223`). The final message comes from accumulated turn state (`codex.mjs:1146-1150`). Reviews use an output schema.
* Async: `task --background` starts a detached worker (`codex-companion.mjs:671-688, 788-802`). `status` waits up to 240 s, polling every 2 s (lines 69-70). `cancel` sends `turn/interrupt` (`codex-companion.mjs:963-1021`, `codex.mjs:960-993`). Job state is on disk under `$CLAUDE_PLUGIN_DATA/state`, falling back to `os.tmpdir()/codex-companion` (`lib/state.mjs:9-13, 40-43`), capped at 50 jobs (80-83).
* Resume: native, by thread id. `--resume-last` finds the newest tracked task thread (`codex-companion.mjs:470-479`).
* Permissions: approval policy `never`, sandbox read-only by default; `--write` selects workspace-write (`codex.mjs:67, 80`, `codex-companion.mjs:491`). No yolo or bypass flag in the files read.
* Config: not isolated. `CODEX_HOME` is honoured, default `~/.codex` (`codex.mjs:653-655`). The environment is inherited.
* Windows and kill: `taskkill /PID <pid> /T /F` (`lib/process.mjs:66-67`). On close the app-server is killed as a tree on win32 and with SIGTERM elsewhere (`app-server.mjs:244-262`).
* Not checked: multi-account and rate-limit handling. Marketplace install flow was not read.

### SinanTufekci/agent-intern

https://github.com/SinanTufekci/agent-intern. MIT, 27 stars, last push 2026-10-14, Python. Transport: MCP stdio (FastMCP, `server.py:656`). Evidence: source read of `codex_bridge.py`, `codex_tools.py`, `proc_tree.py` and `server.py`, each in part. Downloaded but not read: `swarm.py`, `antigravity_tools.py`. Runtimes are Codex and Antigravity. It does not spawn `claude`.

* Codex spawn: `codex exec [resume <sid>] [-m model] [-s <sandbox> -C <ws>] --skip-git-repo-check [--json] -o <tmpfile> <prompt>` (`codex_bridge.py:458-468`), stdin from DEVNULL, timeout plus 30 s (`513-520`). On Windows it uses CREATE_NO_WINDOW and resolves npm shims to the native binary (`70-132, 221-236`).
* agy spawn: `agy --print-timeout <N>s --dangerously-skip-permissions [--disable-slash-commands] ... -p <prompt>` (`server.py:2075-2130`).
* Output: the answer is read from the `-o` file, not stdout (`codex_bridge.py:491-492, 528`). `--json` is used only for watch mode. The session id comes from the rollout JSONL `session_meta` (`9-17, 281-321`).
* Async: none. Tools block and emit MCP progress as an elapsed/timeout bar (`server.py:2387-2415`). Default timeout 180 s (`codex_tools.py:45`).
* Resume: native `codex exec resume <id>`, pinned per workspace in memory (`codex_bridge.py:212-217, 251-261`) with an on-disk fallback by rollout cwd (`323-342`).
* Permissions: Codex defaults to `-s read-only` (`143-147`). agy always gets `--dangerously-skip-permissions`.
* Config: `CODEX_HOME` honoured, not isolated.
* Kill: `taskkill /F /T /PID` on Windows, process-group kill elsewhere (`proc_tree.py:61-93`).

### BeehiveInnovations/pal-mcp-server (clink)

https://github.com/BeehiveInnovations/pal-mcp-server. The LICENSE file head reads Apache 2.0; the API reports NOASSERTION. 11.8k stars, last push 2025-12-15, Python. Transport: MCP stdio; clink runs a local CLI as a one-shot subprocess per call, prompt on stdin. Evidence: source read of `tools/clink.py` (partial), `clink/agents/base.py`, `agents/claude.py`, `agents/codex.py`, `agents/gemini.py`, `parsers/claude.py`, `parsers/codex.py`, `parsers/gemini.py` (partial), `constants.py`, `conf/cli_clients/*.json`. Not read: `parsers/base.py`, system prompts, tests, the conversation store.

* Spawn (internal defaults + client config + role args, `registry.py:139-140`, `base.py:193-199`):
  * claude: `claude --print --output-format json --permission-mode acceptEdits --model sonnet` plus `--append-system-prompt` (`constants.py:42-46`, `conf/cli_clients/claude.json:4-9`, `agents/claude.py:14-23`).
  * codex: `codex exec --json --dangerously-bypass-approvals-and-sandbox --enable web_search_request` (`constants.py:36-41`).
  * gemini: `gemini -o json --yolo` (`constants.py:30-35`).
  * Launch: `asyncio.create_subprocess_exec`, binary resolved with `shutil.which`, environment is `os.environ` plus client env (`base.py:72-79, 111-119, 201-204`).
* Output: claude returns one JSON object or array; `result`, `session_id` and usage are read (`parsers/claude.py:16-61`). Not stream-json. Codex is JSONL; it collects `item.completed` agent messages, `error` and `turn.completed` (`parsers/codex.py:16-63`). Gemini reads the `response` field (`parsers/gemini.py:16-57`).
* Async: none; `clink.execute` awaits the run (`tools/clink.py:208-214`). Default timeout 1800 s (`constants.py:8`). On timeout only the direct child is killed (`base.py:128-134`).
* Resume: none wired to native sessions. The claude `session_id` is only returned as metadata. Its own `continuation_id` uses a separate conversation store, which was not read.
* Permissions come from editable config files; the shipped ones relax them (see the permission matrix below).
* Config: inherits the full environment. User overrides via `CLI_CLIENTS_CONFIG_PATH` and `~/.pal/cli_clients` (`constants.py:14`, `registry.py:99-103`).
* Windows: `which`-based resolution only; no taskkill, no shell. `.cmd` shim handling unverified.

### mkXultra/ai-cli-mcp

https://github.com/mkXultra/ai-cli-mcp (default branch `develop`). MIT, 27 stars, last push 2026-09-30, TypeScript. Transport: MCP tools (`src/app/mcp.ts`). Evidence: source read of `cli-builder.ts` (full), `spawn-cli.ts` (full), `process-service.ts` (86-135, 355-420), `process-termination.ts` (partial), `mcp.ts` (tool definitions, partial), `detached-runner.cjs` (1-25). Not read: `parsers.ts` (981 lines), `model-selection.ts`, `cli-utils.ts`. Runtimes: claude, codex, gemini (Antigravity), opencode, grok, pi.

* claude (`cli-builder.ts:160-174`): `claude --dangerously-skip-permissions --output-format stream-json --verbose [-r <id> --fork-session] [--effort E] -p <prompt> [--model M]`.
* codex (`83-99`): `codex exec [resume <id>] [-c model_reasoning_effort=E] [--model M] --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --json <prompt>`.
* gemini/agy (`112-128`): `--dangerously-skip-permissions --disable-slash-commands --output-format stream-json --print-timeout=2h [--conversation=id] [--model] [--effort] --print=<prompt>`.
* The prompt travels on argv in every case. `spawn-cli.ts:22-55` resolves bare commands on win32 and uses `cross-spawn`.
* Async job tools: `run` returns a PID at once; then `list_processes`, `get_result`, `wait` (default 180 s), `peek` (default 10 s, max 60), `kill_process`, `cleanup_processes` (`mcp.ts:160-310, 426-434`). State is an in-memory Map (`process-service.ts:87`), not persisted.
* Resume: native. Codex `exec resume <id>`; claude `-r <id> --fork-session`, which forks instead of continuing in place; gemini `--conversation=<id>`.
* Permissions: every runtime gets a skip-permissions or bypass flag. No sandbox option.
* Config: environment inherited, no HOME or `CLAUDE_CONFIG_DIR` handling (`process-service.ts:107-111`).
* Kill: grok, gemini and pi use `terminateProcessTree` (taskkill `/pid /t /f` on win32, process-group SIGTERM then SIGKILL elsewhere, `process-termination.ts:29-30, 71-84`). Other runtimes use plain `process.kill('SIGTERM')` (`process-service.ts:403-404`), which on Windows likely reaches only the direct child. This is an inference.

### alessai/Hive-MCP

https://github.com/alessai/Hive-MCP. MIT, 2 stars, last push 2026-05-10, TypeScript. Transport: MCP stdio (`src/index.ts:413-414`). Evidence: source read of `src/agents/{base,claude,codex,gemini,factory}.ts`, `src/index.ts` (partial), `src/tools/hive.ts`, `src/jobs/manager.ts` (partial), `src/config/internal-defaults.ts`, `conf/cli_clients/*.json`, `src/parsers/{claude,codex}.ts`, `src/cli/detect.ts`.

* Spawn: output args plus additional args, prompt on stdin, no shell, `detached: true` (`base.ts:142-150, 221-226`).
  * claude: `--print --output-format json --no-session-persistence` (`internal-defaults.ts:11-17`) plus `--permission-mode bypassPermissions` from `conf/cli_clients/claude.json`.
  * codex: `exec --json`, no sandbox or approval flag.
  * gemini: `-o json`, `-m gemini-2.5-flash` (`internal-defaults.ts:4-10`). It also has qwen (`--approval-mode yolo`) and kilo (`--auto`). No cursor-agent or agy.
* Output: the claude parser accepts a JSON object, an event array or JSONL, and throws on `is_error` (`parsers/claude.ts:8-35`). Codex collects `item.completed` agent messages (`parsers/codex.ts:14-43`).
* Jobs: the tool waits `HIVE_SYNC_WAIT_SECONDS` (default 45, max 300), then returns a job id; `hivejob` has get, list, cancel, collect (`index.ts:77-85, 191-200`). In-memory Map capped at 100 (`jobs/manager.ts:37-38`), so jobs are lost on restart (inference). Cancel is SIGTERM, then SIGKILL after 3 s (`manager.ts:116-129`).
* Resume: none native. `continuation_id` replays earlier turns as text, saved only on success (`tools/hive.ts:51-58, 147-163`).
* Timeouts per client: 1800 s claude, 600 s codex, 300 s gemini; tool argument max 7200 s.
* Env: strips `CLAUDECODE`, colour variables and secret-like variables (`base.ts:81-98, 184-190`). HOME is untouched, so user config is used.
* Windows: no win32 branch; `process.kill(-pid)` errors are swallowed (`base.ts:274-282`); binary lookup joins PATH entries with the bare name and no PATHEXT (`cli/detect.ts:20-38`). Inference: `.cmd` shims would not be found.

### swjturay/codex-agy-delegator

https://github.com/swjturay/codex-agy-delegator. MIT, 4 stars, last push 2026-07-25, TypeScript. Transport: MCP stdio; work runs in a detached runner process (`src/index.ts:362-373`, `src/delegateToAgent.ts:207-236`). Evidence: source read of `agentBackends.ts`, `shell.ts`, `runAgentTask.ts`, `index.ts` (28-140, 355-378), `delegateToAgent.ts` (partial). Not read: `runArtifacts.ts`, `cleanupAgentRun.ts`.

* Spawn (`agentBackends.ts:159-208`):
  * agy: `--sandbox --mode plan|accept-edits`, or `--dangerously-skip-permissions` for full access, plus `--model`, `--print-timeout <N>s`, `--print <prompt>` on argv.
  * codex: `exec --ephemeral --ignore-user-config --sandbox <mode> --cd <cwd> --output-last-message <file> [--model] -`, prompt on stdin.
  * claude: `--print --output-format json --permission-mode plan|acceptEdits|bypassPermissions`, prompt on stdin.
  * custom: user argv with a `{{prompt}}` placeholder.
* Defaults: workspace-write (`index.ts:121`). Full-access and custom backends need an explicit `allowUnsafe` (`agentBackends.ts:129-136`).
* Output: Codex via the output file; claude via JSON `result` (`agentBackends.ts:220-232`).
* Jobs: background by default, `waitForCompletion` opt-in (`delegateToAgent.ts:431-438`). Tools: `get_agent_run_report`, `apply_agent_run`, `cleanup_agent_run`. State is on disk in a run directory. A git worktree is created per run by default (`delegateToAgent.ts:312, 384-397`).
* Timeouts: agent default 900 s, range 1 s to 24 h. Background mode avoids the MCP tool timeout.
* Resume: none in the files read.
* Config: children inherit the environment. Codex runs with `--ignore-user-config`, so the user's Codex setup is deliberately dropped.
* Kill: POSIX `process.kill(-pid,'SIGKILL')`; Windows `taskkill /PID /T /F` (`shell.ts:46-76`).
* Windows: explicit taskkill branch and a PowerShell installer. Spawn uses `shell:false` with bare names (`shell.ts:96-103`), so `.cmd` shim failure is a risk (inference). The README install path pipes a remote script into `Invoke-Expression`.

### thomaswitt/mcp-agents

https://github.com/thomaswitt/mcp-agents. 19 stars, last push 2026-10-14, JavaScript; `server.js` is 11,264 lines. License: the README says MIT (`README:1159-1161`) but the repository has no LICENSE file and the API reports none. Treat it as unlicensed until confirmed. Transport: MCP stdio or loopback HTTP (`server.js:607-615`); Codex through app-server JSON-RPC. Evidence: source read of the ranges cited, plus `package.json`. Not read: `codex-legacy.js` and most of `server.js`.

* claude (`server.js:404-420, 589-595`): `--model ... --fallback-model ... --effort xhigh --no-session-persistence -p --output-format json`, prompt on stdin. A review job uses `--input-format stream-json --output-format stream-json --verbose --setting-sources project --settings <file> --disable-slash-commands --strict-mcp-config --tools Bash,Glob,Grep,Read --permission-mode plan` (`2045-2068`).
* agy (labelled gemini): `agy --sandbox -p <prompt>` in a temp directory (`421-430, 2942-2943`).
* codex: `codex app-server --stdio` with `CODEX_HOME` and `CODEX_SQLITE_HOME` pointing at per-generation temp homes. `auth.json` and `models_cache.json` are copied in and refreshed auth is written back (`3276-3290, 3332-3405, 8565-8586, 8607-8615`). Defaults sandbox workspace-write, approval never (`69-73`). A minimum Codex version is checked.
* Output: single JSON object or event array; the final `type:"result"` is taken (`487-500`).
* Jobs: `claude-start/status/result/cancel` and `codex-start/status/result/cancel` (`2073-2161`, `3549-3660`), in an in-memory Map with retention 32 (`111, 191, 2201`).
* Resume: Codex native by `threadId` (`3524-3530`); claude none by design.
* Timeouts: claude 900 s default; job deadline and Codex 7200 s; Codex idle 600 s (`65-92`). Start/status polling avoids the MCP timeout.
* Rate limit: a claude quota rejection triggers one retry on the default model (`531-565`). This is a model fallback, not an account change.
* Env: `{...process.env, NO_COLOR:"1"}` (`1882-1889`).
* Kill: `process.kill(-pid,'SIGKILL')` per detached group, no taskkill. Durable Codex goals are disabled on win32 (`6433`). CLI spawns have no win32 handling (inference).

### yejianfei/subagent-cli

https://github.com/yejianfei/subagent-cli. Apache-2.0, 0 stars, last push 2026-08-01, TypeScript (webpack build; npm `@yejianfei.billy/subagent-cli` 0.1.25). Transport: PTY/terminal emulation (`node-pty` plus `@xterm/headless`) behind a local daemon and CLI. Evidence: source read of `adapter.ts` (selected ranges), `adapters/claude_code.ts` (full), `pty_xterm.ts` (1-70), `config.ts` (20-84), `app.ts` (selected), `package.json`, README (grep). Not read: `cli.ts`, `client.ts`, `session_history.ts`, full Codex and Gemini adapters.

* Spawn (`config.ts:53-80`): `claude` with no arguments plus `ANTHROPIC_MODEL=haiku`; `codex --ask-for-approval untrusted -m gpt-5.5`; `gemini` with no arguments. Started with `pty.spawn(command, args, {name:'xterm-256color', cwd, env})` (`pty_xterm.ts:36-43`).
* Prompt delivery: typed into the TUI through bracketed paste (`pty_xterm.ts:68-70`, `claude_code.ts:43-48`).
* Output: screen text from the headless emulator, not structured events.
* State detection: a 1000 ms interval flushes the emulator and matches word lists for asking, running and idle states (`adapter.ts:580-630`; Claude rules at `claude_code.ts:96-117`). Tool approvals are parsed from the screen with a regex on the tool-call line (`claude_code.ts:57-94`). Approve is Enter; allow is Down plus Enter; reject is Down, Down, Enter.
* Job model: long-lived sessions with open, prompt (blocks until done or an approval is needed), approve, reject, allow, cancel, exit, close. A pending approval returns HTTP 409 `APPROVAL_NEEDED`. Metadata under `~/.subagent-cli`.
* Resume: native. `claude --resume <uuid>` parsed from exit output, `codex resume <uuid>`, `gemini --resume <uuid>`.
* Permissions: no bypass flags by default; approvals are handled by screen scraping.
* Timeouts: idle 300 s, check interval 30 s. Kill sends SIGKILL to the PTY pid only (`pty_xterm.ts:61-64`).
* Windows: README states Windows is not supported. A preflight that spawns `echo` through the PTY likely fails there (inference).

### polyagent-mcp, native-agent-router, and the remaining earlier leads

These were docs-level in the earlier research and were not re-read this time.

* JaimeJunr/polyagent-mcp (MIT, about 3 stars, docs read): headless Codex, Grok, Claude, OpenCode and opt-in Cursor over MCP. Linux bubblewrap with an empty HOME isolates the worker from the user's config, and a missing sandbox fails closed by default.
* BerineYang/native-agent-router (MIT, about 3 stars, docs read): `agents`, `run`, `wait`, `inspect`, `cancel`, explicit agent selection, workspace locks, verification and repair budgets, ACP routing.

## Part 2. Newly inspected projects

### uwuclxdy/clauth

https://github.com/uwuclxdy/clauth (default branch `mommy`). MIT, 242 stars, last push 2026-10-14, Rust. Transport: MCP stdio (`rmcp`) as `clauth mcp`, shipped as a Claude Code plugin; also a TUI and a daemon. Evidence: source read of `src/mcp/mod.rs` (first 100k of 271k characters), `src/mcp/jobs.rs`, `src/start.rs`, README. Not read: `runtime.rs` (config directory construction), `fallback.rs`, `watchdog.rs`, `usage/`. Line numbers were not captured. Readers saw the files through fetch summaries, so exact flag spellings were not cross-checked against the raw text.

This is the closest existing project to the first use case.

* Tools: `profiles`, `switch_profile`, `delegate`, `monitor`. `delegate` takes `profiles[]` for fan-out (maximum 8), `prompt` (inline or file path, 64 KiB cap), `isolated`, `background`, `model`, `cwd`, `session_id`, `subagent_type`, `allowed_tools`, `permission_mode`, `result`, `env`, `args`. `monitor` takes `job_ids` and `cancel`.
* Spawn: `claude -p ...` with typed arguments mapped to `--agent`, `--allowedTools`, `--permission-mode`, `--model`. Caller `args` are appended. Raw `--resume`, `--session-id` and `--fork-session` in `args` are refused. The environment is the scrubbed profile environment plus caller `env`, with `CLAUDE_CONFIG_DIR`, `CLAUTH_MCP_DEPTH` and `CLAUTH_DELEGATE_SESSION_ID` overwritten.
* Permissions: no bypass by default. A writing delegate has to pass `--dangerously-skip-permissions` through `args` explicitly.
* Recursion guard: `CLAUTH_MCP_DEPTH` limits delegation to one level and fails closed.
* Job model (`mcp/jobs.rs`): one JSON file per job at `~/.clauth/jobs/<id>.json`, written atomically with 0600 permissions. States Running and Done. A result is claimed by renaming to `.claim`, then recorded in a `.delivered` ledger. Done records are garbage-collected after 24 hours. A locked `mcp_live/<pid>` marker detects dead owners.
* Resume: native through a pinned session id.
* Timeouts: delegates have no wall-clock limit. A 2 s heartbeat keeps Claude Code's 30-minute stdio idle abort from firing. The README says the host parks blocking calls as a task at about 120 s.
* Isolation: `clauth start <profile>` runs claude under a per-profile `CLAUDE_CONFIG_DIR` described as a temporary `~/.claude` mirror. `--isolated` keeps authentication but drops CLAUDE.md, plugins, hooks, skills and MCP servers (adds `--strict-mcp-config`) and uses a throwaway transcript tree. How files are linked into the mirror was not read.
* Limit detection and fallback: not found in the files read. The README says a daemon or the TUI switches along a fallback chain on usage thresholds, using a cached usage API. `start.rs` refuses `--with-fallback` together with isolated mode and requires the daemon. Fallback does not appear to be wired into `delegate`.
* Windows: README lists support through Git Bash or MSYS2. Install by `cargo install clauth` or a curl script with background self-update.

### shinpr/sub-agents-mcp

https://github.com/shinpr/sub-agents-mcp. MIT, 98 stars, last push 2026-10-14, TypeScript. Transport: MCP stdio, run with `npx -y sub-agents-mcp`. One runtime per server instance, chosen by the required `AGENT_TYPE` variable. Evidence: source read of `src/execution/AgentExecutor.ts`, `src/config/ServerConfig.ts`, `src/session/SessionManager.ts`. Not read: `StreamProcessor`, `RunAgentTool`. Readers saw summaries, so line numbers are not available.

* Spawn: `spawn(cmd, args, {shell:false, stdio:['ignore','pipe','pipe']})`. claude: `--output-format stream-json --verbose --append-system-prompt ... -p <prompt>`, optional `--settings <path>`. codex: `exec --json --skip-git-repo-check <prompt>`, effort via `-c model_reasoning_effort=`. gemini: `--skip-trust --output-format stream-json -p ...`, system prompt through `GEMINI_SYSTEM_MD`. cursor-agent: `--output-format json -p <prompt>`. Also agy, opencode, grok, and GLM or Kimi through claude with a base URL override.
* The permission matrix (`AGENT_PERMISSION`) is reproduced in the permission table below. The default level was not seen.
* Output: newline-split JSONL into a stream processor with early exit on the result line.
* Async: none. Blocking, default timeout 5 minutes (`EXECUTION_TIMEOUT_MS`), SIGTERM then SIGKILL after 1 s, exit code 124 on timeout, 130 on abort, 16 MiB output cap, E2BIG handled.
* Sessions: JSON per session and agent in `.mcp-sessions` (0600, one-day retention). History is stored; no native resume flag appears in the executor, so it is probably replay (unconfirmed).
* Config: inherits the user's configuration. It overrides `CODEX_HOME` and `CURSOR_CONFIG_DIR` only when `AGENTS_SETTINGS_PATH` is set. The environment builder can delete keys.
* Windows: no explicit handling. `shell:false` with bare command names means `.cmd` shims will probably fail (inference). No rate-limit handling.

### steipete/claude-code-mcp

https://github.com/steipete/claude-code-mcp. MIT, 1.3k stars, last push 2026-05-15, archived, TypeScript. Transport: MCP stdio, one tool `claude_code`. Evidence: source read of `src/server.ts` only; no line numbers captured.

* Spawn: `spawn(claudeCliPath, args, {shell: needsShell, timeout, cwd, stdio:['ignore','pipe','pipe']})`. Arguments: `--dangerously-skip-permissions` when the permission mode is `bypassPermissions` (the default), otherwise `--permission-mode <m>`; with a session id it adds `--output-format json --resume <id>`; the prompt goes with `-p`.
* Binary lookup order: `CLAUDE_CLI_PATH`, `CLAUDE_CLI_NAME`, `~/.claude/local/claude(.cmd/.exe)`, the Windows npm path, then PATH.
* Windows: `shell:true` for `.cmd` and `.bat` with unescaped arguments, which is a command-injection risk.
* Async: none; blocking with `CLAUDE_CLI_TIMEOUT_SECONDS` default 3600.
* Output: plain stdout, or the `type==='result'` JSON object when a session is used.
* Sessions: native `--resume`, with a mapping in `~/.config/claude-code-mcp/sessions.json` (24 hour TTL, 1000 entries). History is replayed only when no mapping exists.
* Config inherited. No rate-limit handling.

### tuannvm/codex-mcp-server

https://github.com/tuannvm/codex-mcp-server. No license, 643 stars, last push 2026-05-25, TypeScript. Do not reuse code. Transport: MCP stdio. Evidence: source read of `src/tools/handlers.ts` and `src/utils/command.ts`; not read: `session/storage.ts`, `definitions.ts`.

* Spawn: `spawn(file, escapedArgs, {shell: isWindows, env})`, stdin closed. Exec arguments: `exec --model --sandbox --full-auto -C --skip-git-repo-check <prompt>`, with `-c model_reasoning_effort`. Resume: `exec --skip-git-repo-check resume <conversationId> <prompt>`, with sandbox and working directory not applied on resume.
* Output: plain stdout. The conversation id is extracted from stderr by regex.
* Windows: handled by `%` doubling and quoting.
* Pitfall: a run counts as success if the exit code is 0 or any output exists, which hides failures. No timeout (10 MB output cap only).
* Session: native resume through the captured id, with a fallback that replays the last two turns, truncated.

### jamubc/gemini-mcp-tool

https://github.com/jamubc/gemini-mcp-tool. License NOASSERTION, 2.3k stars, last push 2026-07-21, TypeScript. Transport: MCP stdio. Evidence: source read of `src/utils/geminiExecutor.ts` only. Not read: `commandExecutor.js`, `constants.js`.

* Arguments: `-m model`, `--sandbox`, and `-p prompt`; the prompt goes on stdin when change mode or an `@file` reference is present, which avoids cmd.exe quoting and command-line length limits. No yolo flag in this file.
* Quota handling: on `QUOTA_EXCEEDED` it retries with the flash model. That is a model fallback, not an account rotation.
* Output: plain text, plus a change-mode edit parser with a 10-minute chunk cache.
* Spawn options, timeouts and async handling were not read.

### BloopAI/vibe-kanban

https://github.com/BloopAI/vibe-kanban. Apache-2.0, 28.4k stars, last push 2026-09-19, Rust. A full kanban application, not a plugin; useful as a reference for stream-json handling. Transport: stream-json control protocol over stdio, similar to an app-server. Evidence: source read of `crates/executors/src/executors/claude.rs` (first 100k of 130k characters). Its executors directory also holds codex (JSON-RPC app-server), gemini, cursor and `acp/` modules, which were not read.

* Spawn: `npx -y @anthropic-ai/claude-code@<pinned>` with `-p --verbose --output-format=stream-json --input-format=stream-json --include-partial-messages --replay-user-messages`. The prompt is sent after an `initialize()` handshake. It adds `--permission-prompt-tool=stdio --permission-mode=...` or `--disallowedTools=AskUserQuestion`; `--dangerously-skip-permissions` is opt-in. Environment: `NPM_CONFIG_LOGLEVEL=error`, optional `env_remove("ANTHROPIC_API_KEY")`, `kill_on_drop`, `group_spawn_no_window`.
* Resume: native, `--resume <id>` plus `--resume-session-at <uuid>`.
* Rate limits: the `RateLimitEvent` is parsed and then ignored in `normalize_entries`. No failover.

### coder/agentapi

https://github.com/coder/agentapi. MIT, 1.5k stars, last push 2026-09-13. The README says it is deprecated and no longer maintained. Go. Transport: PTY/terminal emulation exposed as HTTP on port 3284 (`/message`, `/messages`, `/status`, SSE `/events`). Evidence: source read of `lib/httpapi/setup.go` and the README. Not read: `termexec`, `msgfmt`, `screentracker`.

* It runs `agentapi server --type=claude -- claude <flags>`, turns API calls into keystrokes, and diffs the screen into messages. Supports claude, codex, gemini, cursor, goose, aider, amp and others. Flags are whatever the user passes after `--`, so there are no default bypass flags.
* A second path, `SetupACP`, uses `exec.CommandContext` over stdio pipes (`setup.go` ~66-124), with SIGTERM then a kill after 5 s.
* Job model: none; a stable/running status poll, state in memory, no resume. Windows source files exist (`process_windows.go`, `signals_windows.go`); the README does not mention Windows.
* Risk: TUI parsing breaks when a CLI changes its screen. Poor fit for headless `-p` semantics.

### Oortonaut/mcacp

https://github.com/Oortonaut/mcacp (default branch `master`). Apache-2.0, 11 stars, last push 2026-02-01, TypeScript. An ACP client exposed as an MCP server. Evidence: source read of `src/acp/lifecycle.ts` and `src/registry/index.ts`; the rest of the description comes from the README and package listing pages. Not read: `transport.ts`, `sessions/`, `permissions/`.

* Transport: MCP stdio towards the host, ACP stdio JSON-RPC towards each agent. Spawn is `new AcpTransport({command, args, env, requestTimeoutMs: 60000})` with the command taken from config or an installed-agent record. The registry installs agents through `npx -y <pkg>` or by downloading binaries, recorded in `agents.json`.
* Advertises filesystem read/write and terminal client capabilities. Idle reaping through `autoReapMs`. Sessions persist to disk with native `load_session` (README; code not read).
* Permissions are handled with elicitation forms. Under Claude Code it falls back to an "operator mode" where the host agent decides (README).
* The ACP adapters it launches use the Agent SDK, not `claude -p`. No Windows handling seen, no multi-account support.

### zed-industries/claude-agent-acp and codex-acp

Docs read only. https://github.com/zed-industries/claude-agent-acp (previously named claude-code-acp): Apache-2.0, 2.6k stars, last push 2026-10-14, TypeScript. It exposes the Claude Agent SDK to any ACP client; npm package `@agentclientprotocol/claude-agent-acp`. The README says nothing about authentication, `CLAUDE_CONFIG_DIR` or Windows. https://github.com/zed-industries/codex-acp: license NOASSERTION (check before any reuse), 871 stars, last push 2026-07-22, Rust. Neither was read at source level.

Observation relevant to the design: an ACP agent for Claude runs the Agent SDK, so a plain `claude -p` process under a given account is a different thing from the ACP route.

### Account-rotation tools for Claude (discovery only)

These came from search results and documentation pages. No source was read.

* realiti4/claude-swap (MIT, 3.4k stars, Python): `cswap auto` switches at a 90 percent threshold on the 5-hour and 7-day windows, with a cooldown, by swapping credentials in place rather than using per-account config directories.
* ipfans/ccswap (Go, auto switching at 80 percent), GG-Santos/ccswitch (daemon), Mike-7777777/cx (`cx run` picks the lowest 5-hour usage), claude-smart/csm (per-profile config homes, relaunch on rate limit), umuplus/clauth (6 stars, a different project from uwuclxdy/clauth), CCS (pool failover), ccas (autoswitch with cooldown).
* claude-rotate (doxaras) is a proxy, so it is out of scope.
* A feature request for profiles and failover exists at anthropics/claude-code#34341; its status was not checked.
* Transcripts are keyed by project path, not by account, so a resumed conversation after an account switch continues the same conversation.
* None of these documented switching accounts in the middle of a `claude -p` run. Failover is per launch, so a wrapper has to detect the limit and retry.

## Carried over from the earlier research, not re-inspected

| Project | Status in the earlier record |
|---|---|
| jarrodwatts/claude-delegator | Archived README read. Older Codex-over-MCP delegation; says `codex mcp-server` was deprecated at 0.149.1 and removed after 0.153 and points to the official plugin. Version-drift evidence. |
| ken-jo/agent-connector | Apache-2.0. Claims 42 host adapters and nine plugin formats plus shared MCP artifacts. Builds host packages; not itself a delegation engine. Counts not tested. |
| M0T0-2020/Codex-Antigravity | README only. Codex writer with AGY scout and QA, standard-library Python MCP and skills. |
| andreilungeanu/codex-delegate-mcp | MIT, about 2 stars. README and changelog only: native Codex delegation, multi-host packaging, doctor, status, cancel, resume, JSONL handling. |
| osanoai/multicli | Discovery only. |
| fakiho/neohive | Discovery only. Shared-filesystem collaboration claim unverified. |
| josstei/maestro-orchestrate | Discovery only. Cross-CLI orchestration skills. |
| lastmile-ai/mcp-agent | Established agent-workflow framework; a different category and a naming collision. |
| confeden/Delegator, yicheng47/runner, woonyong-choi/tmux-agents, speakeasy-api/kit | Discovery only. |

Other leads found in this pass, not inspected:

* smtg-ai/claude-squad: AGPL-3.0, 8.6k stars, last push 2026-08-20, Go. A tmux plus worktree TUI, not a library. The AGPL license rules out copying code.
* just-every/code: Apache-2.0, 4.0k stars, last push 2026-09-29, Rust. A Codex-derived CLI with its own multi-agent features.
* manishiitg/llm-provider-mcp: 1 star, Go, license NOASSERTION. Job id plus polling, requires tmux 3.x.
* gemini_mcp (shenyunhuan): Python, Gemini over ACP with a long-lived child.
* Vercel AI SDK ACP community provider: wraps ACP agents as a language model, the opposite direction.
* agent-mcp-hub: bridges codex, cursor, opencode, claude and antigravity over stdio; URL and stars not resolved.
* A GitHub search for the delegation keywords returned nothing useful, and "acp-mcp" and successors of steipete's project were not found, so coverage of small repositories is incomplete.

API-translation proxies, a different category and not inspected: musistudio/claude-code-router, lidge-jun/opencodex, thyjeff/opencodex.

## Comparison across projects

Permission defaults. This is the main safety difference between projects.

| Project | claude | codex | gemini | cursor-agent / agy |
|---|---|---|---|---|
| ai-cli-mcp | `--dangerously-skip-permissions` always | `--dangerously-bypass-approvals-and-sandbox` always | skip-permissions always | same |
| PAL clink | `--permission-mode acceptEdits` | bypass flag | `--yolo` | none |
| Hive-MCP | `bypassPermissions` | no flag | none | none |
| steipete/claude-code-mcp | `bypassPermissions` by default | n/a | n/a | n/a |
| agent-intern | n/a | `-s read-only` default | n/a | agy always skip-permissions |
| codex-agy-delegator | `plan`, `acceptEdits` or `bypassPermissions` | `--sandbox <mode>` | n/a | agy `--sandbox`; skip only with `allowUnsafe` |
| codex-plugin-cc | n/a | approval never, read-only sandbox by default, `--write` for workspace-write | n/a | n/a |
| clauth | none unless the caller passes it | n/a | n/a | n/a |
| sub-agents-mcp read-only level | `--permission-mode plan` | `-s read-only` | `--approval-mode plan` | cursor `--mode plan --sandbox enabled` |
| sub-agents-mcp safe-edit level | `--permission-mode acceptEdits` | `-s workspace-write -c approval_policy=never` | `auto_edit` | cursor `--trust --sandbox enabled` |
| sub-agents-mcp yolo level | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `-y` | cursor `-f --trust` |

Job models, resume and config.

| Project | Async jobs | Persistence | Native resume | Config |
|---|---|---|---|---|
| codex-plugin-cc | start, status (wait 240 s), cancel | disk, 50 jobs | yes (`thread/resume`) | inherited |
| clauth | delegate with background, monitor, cancel | disk, atomic files, 24 h GC | yes (pinned id) | per-profile `CLAUDE_CONFIG_DIR`, optional isolated mode |
| ai-cli-mcp | run, wait, peek, get_result, kill | memory | yes | inherited |
| Hive-MCP | sync wait 45 s then job id | memory, 100 jobs | no (replay) | inherited |
| mcp-agents | start, status, result, cancel | memory, 32 jobs | Codex only | Codex isolated per generation, claude inherited |
| codex-agy-delegator | background default, report, apply, cleanup | disk | none | Codex ignores user config |
| agent-intern | none, blocking with progress | memory pin plus disk fallback | Codex yes | inherited |
| PAL clink, sub-agents-mcp, steipete, tuannvm, gemini-mcp-tool | none, blocking | session files in some | mixed | inherited |
| subagent-cli | long-lived sessions | metadata on disk | yes | inherited |

## The five best references for our design

Our project is Apache-2.0. MIT, Apache-2.0 and BSD sources can be studied and re-implemented freely, and code copied from them can be kept with its original copyright and license notice. The cleanest course is still to re-implement the idea and credit the source in a comment or the docs. Projects with no license, NOASSERTION or AGPL (codex-mcp-server, mcp-agents until confirmed, gemini-mcp-tool, claude-squad) should be read for ideas only.

1. clauth (MIT). Copy the job store layout: one atomic 0600 JSON file per job, a claim-by-rename result handoff, a delivered ledger, a 24 hour garbage collection and a liveness marker for dead owners. Copy the per-account `CLAUDE_CONFIG_DIR` approach, the `--isolated` mode with `--strict-mcp-config`, the depth environment variable that stops recursive delegation, the 2 second heartbeat that keeps host idle timeouts from firing, and the refusal of raw `--resume` flags. Its missing piece, detecting a usage limit and rotating inside `delegate`, is the part we must build. It is Rust, so we re-implement rather than copy.
2. openai/codex-plugin-cc (Apache-2.0). Copy the app-server driver for Codex (thread start, resume and turn interrupt, `taskkill /T /F` on Windows, a broker so several calls share one server), disk-backed job state keyed by plugin data directory, and the status-with-wait plus cancel command shape. This is also the model for a Claude Code plugin with a Bash helper, which avoids MCP timeouts altogether.
3. shinpr/sub-agents-mcp (MIT). Copy the three-level permission matrix across claude, codex, gemini and cursor-agent, the per-runtime flag sets for stream-json, the 16 MiB output cap and the SIGTERM then SIGKILL timeout handling with distinct exit codes.
4. mkXultra/ai-cli-mcp (MIT). Copy the async tool surface (run returns an id, then wait with a default and a peek with a cap) and the claude stream-json argument set. Do not copy its always-on bypass flags, its in-memory-only state, or its plain `SIGTERM` kill path on Windows.
5. SinanTufekci/agent-intern (MIT), with codex-agy-delegator (MIT) as a secondary source. From agent-intern: reading Codex's final answer from `--output-last-message`, extracting the session id from the rollout file, MCP progress notifications during blocking calls, stdin from DEVNULL, resolving npm shims to the native binary on Windows, and a process-tree kill module. From codex-agy-delegator: the worktree per writer with an explicit apply step, `allowUnsafe` gating for dangerous modes, and an explicit Windows taskkill branch.

Also worth reading but not copying wholesale: PAL clink for its parsers for claude, codex and gemini JSON output (Apache-2.0, but verify the license file), and vibe-kanban for the bidirectional stream-json protocol if we later want permission prompts relayed to the host.

## Common pitfalls seen

* Permission bypass as a default. ai-cli-mcp, PAL clink, Hive-MCP and the archived claude-code-mcp all start workers with bypass or yolo flags unless the user changes the config. A worker that can run any command under the user's login is a poor default. Default to read-only or accept-edits, and make bypass an explicit per-call opt-in.
* Config isolation. Most projects inherit the environment, which keeps the user's setup but also leaks extra tools and hooks into workers. codex-agy-delegator, mcp-agents (Codex) and polyagent-mcp isolate, and lose the user's normal configuration and sometimes their login. clauth gives both: a per-account config directory by default and an isolated mode on request. Whatever the default, document it.
* Timeouts. Blocking MCP calls hit host limits (Claude Code parks long calls and aborts idle stdio). Projects that survive long tasks either return a job id (ai-cli-mcp, Hive-MCP, mcp-agents, clauth, codex-plugin-cc) or send heartbeats or progress notifications (clauth, agent-intern). tuannvm's server has no timeout at all, and its "any output counts as success" rule hides failures.
* Windows. Several projects use `shell:false` with bare names, which cannot start npm `.cmd` shims; steipete's project uses `shell:true` with unescaped arguments, which is an injection risk. Killing only the direct child leaves the process tree running on Windows (ai-cli-mcp for non-Gemini runtimes, mcp-agents, subagent-cli). Working pieces exist: `taskkill /T /F` (codex-plugin-cc, agent-intern, codex-agy-delegator), native binary resolution (agent-intern), prompts on stdin to avoid quoting and length limits (gemini-mcp-tool, Hive-MCP, codex-agy-delegator).
* Stdout buffering and parsing. Single-JSON `--output-format json` gives nothing until the end and cannot show progress; `stream-json` needs `--verbose` for claude. Reading the final answer from a file (`-o` for Codex) is more robust than parsing stdout. PTY scraping (subagent-cli, agentapi) breaks when a CLI changes its screen text and is the weakest option except for CLIs that print nothing when piped, which is the Antigravity case.
* Zombies and orphans. In-memory job tables lose track of children when the server restarts. Detached process groups need a group kill. Disk records with a liveness marker (clauth) and an exit hook that kills children (Hive-MCP) address this; neither alone is enough.
* Resume semantics. Replaying context (Hive-MCP, the fallback in codex-mcp-server) is not native resume and should be labelled as such. `claude -r <id> --fork-session` forks rather than continuing.
* Usage limits. No inspected project detects a usage-limit error from a `claude -p` run and retries on another account. mcp-agents and gemini-mcp-tool fall back to a cheaper model instead. Account switchers exist (claude-swap and others), but they act per launch, not during a run, and some swap credentials in place, which is unsafe when several workers run at once.
* Recursion. A worker that has the delegation tool installed can delegate again. clauth's depth variable is the only guard seen.
* Licensing. Several popular projects have no license or an ambiguous one.

## Architecture options

| Option | How it works | Strengths | Weaknesses | Fit |
|---|---|---|---|---|
| MCP server with tools | One stdio server exposing start, status, result, cancel, list tools; hosts call it | Works in any host that speaks MCP; one implementation; job state is in our control | Tool-call timeouts and idle limits in hosts; tool schemas add tokens to every session; per-host differences in progress notifications; install means editing each host's MCP config | Best common denominator for many hosts if jobs are async and heartbeats are sent |
| Host plugin plus Bash helper | A host-specific package (commands, agents, skills) that calls a local CLI helper; the helper owns jobs on disk | No MCP timeout problem because each call is short; no tool schema overhead; idiomatic install and marketplace in that host; clear user-visible commands. Proven by codex-plugin-cc | One package per host and each host's plugin format differs; hosts without Bash or without plugins are not covered; the model must be taught the commands through skills or agent prompts | Best first target for Claude Code; the helper can be the shared core |
| ACP client (our process speaks Agent Client Protocol to workers) | Spawn ACP adapters per runtime and talk JSON-RPC; sessions, permissions and cancellation are in the protocol | Structured events, cancellation, session load and permission requests in one protocol; growing adapter list | Adapters exist for some runtimes only; the Claude adapter runs the Agent SDK rather than `claude -p`, which changes the account and harness story; extra layer of versions; permission relaying needs a host UI | Good optional backend per runtime where an official adapter exists; not a base for every runtime |
| Per-runtime native headless mode (`claude -p`, `codex exec`, `codex app-server`, `agy -p`) | We spawn the native binary and parse stream-json or JSONL | Keeps the native harness and login; simplest to reason about; account rotation by `CLAUDE_CONFIG_DIR` | Each runtime has its own flags, formats and bugs; version drift | The core of the design |
| PTY or terminal emulation | Drive the interactive TUI through a pseudo-terminal | Works when headless output is broken (Antigravity on Windows); can answer approval prompts | Fragile screen parsing; Windows ConPTY quirks; prompt and approval synchronisation | Fallback only, per runtime |

A shared core that owns the job store, spawning, parsing and account rotation can be exposed through both an MCP server and a thin Bash helper, with host plugins as packaging around the helper. That matches what the sources show: clauth and codex-plugin-cc keep state on disk in the core, and the MCP surface is a thin layer over it.
