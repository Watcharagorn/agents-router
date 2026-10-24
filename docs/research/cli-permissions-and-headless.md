# Permissions, reviewers and headless interfaces of coding-agent CLIs

Research date: 2026-10-14. Everything below comes from official documentation, official repositories (README, changelog, source) or the local `--help` of `codex` 0.162.1. Evidence labels: docs, source, changelog, community (community posts only served as leads and are labelled where they are the only trace). "Not found" means the official material I read does not say; it is not a claim that the feature is absent. Nothing here was run against a model, and no third-party CLI was installed.

## 1. Short answer

Two CLIs have a native automatic reviewer that is documented for headless use: Claude Code (auto mode, usable with `-p` since v2.1.281) and Codex (`codex exec --approve-for-me`, which routes approvals to its auto-review). Five more have a native LLM or classifier reviewer whose headless behaviour is not documented, so it has to be tested before we trust it: Qwen Code (`auto` approval mode), Cursor CLI (`--auto-review`), GitHub Copilot CLI (`/allow-all auto`, also called Assisted Permissions, experimental), Goose (`smart_approve`) and Antigravity CLI (Auto mode, added in 1.3.5, Linux and macOS only, so absent on Windows). Droid has risk-based autonomy levels, but the docs do not say an LLM assigns the risk. Gemini CLI, OpenCode, Amp, Kiro CLI, Kimi CLI, Cline CLI, Crush, Mistral Vibe and Grok Build have no native reviewer; they offer static rules, an approve-everything flag, and (for most) a pre-tool hook where we can plug in our own reviewer. Kimi CLI and Cline CLI approve everything by default in headless mode, so a hook or sandbox is mandatory there.

## 2. Matrix

Abbreviations: yes/no/nf (not found in the official material read). "Hook" means a hook or plugin that can deny a call before it runs. Auth column: "login" = reuses the native login, "key" = API key or token.

| CLI | Native auto-reviewer (headless?) | Static modes / approve-all flag | External-reviewer hook | Structured headless output | ACP | Resume in headless | Automation auth |
|---|---|---|---|---|---|---|---|
| Claude Code | Auto mode, classifier; works in `-p` (v2.1.281+); blocked actions in `-p` are denied | `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`; `--dangerously-skip-permissions` | PreToolUse hooks; `--permission-prompt-tool` (MCP tool); `--permission-prompts none` | `--output-format json` or `stream-json`, `--input-format stream-json`, SDK | Through adapter (`claude-agent-acp`) | `-c`, `-r <id>`, `--session-id` | login or key; `claude setup-token` for CI |
| Codex CLI | Auto-review (guardian); `--approve-for-me` on `codex exec`; fails closed | `sandbox_mode` read-only / workspace-write / danger-full-access; `approval_policy`; `--dangerously-bypass-approvals-and-sandbox` (`--yolo`) | PreToolUse and PermissionRequest hooks (exec support nf); execpolicy `.rules` | `exec --json` (JSONL), `--output-schema`, `app-server` JSON-RPC, SDK | Through adapter (`codex-acp`) | `exec resume --last` or `<id>` | login or `CODEX_API_KEY` |
| Gemini CLI | None | `default`, `auto_edit`, `plan`, `yolo`; policy engine TOML | BeforeTool hook (deny or exit 2); policy engine | `-o json`, `-o stream-json` | Native (`--experimental-acp`) | `--resume latest`, index or id | key or enterprise; consumer login ended 2026-06-18 |
| Antigravity CLI (`agy`) | Auto mode in 1.3.5 (separate reviewer agent for commands leaving the sandbox), macOS and Linux only; headless nf | `--mode accept-edits` / `plan`; `toolPermission` setting; `--dangerously-skip-permissions`; allow/deny/ask lists | PreToolUse hook (`decision`: allow, deny, ask, ...) | `--output-format json` / `stream-json`, `--input-format stream-json`, `--json-schema` | nf | `-c`, `--conversation <id>` | login (cached); wrapping not allowed on consumer plans |
| Cursor CLI | Auto-review (allowlist, then sandbox, then classifier); flag documented, headless nf | `--mode plan/ask`, `--sandbox`, `permissions.allow/deny`; `-f` / `--force` / `--yolo` | Hooks incl. `beforeShellExecution` (deny); CLI support of non-session hooks unclear | `-p` with `json` or `stream-json` | Native (`agent acp`) | `--resume [id]`, `--continue` | `CURSOR_API_KEY` or login |
| GitHub Copilot CLI | `/allow-all auto` (LLM judge), "Assisted Permissions"; experimental; headless nf | `/permissions` default / assisted / allow-all; `--allow-tool`, `--deny-tool`, `--allow-all`, `--allow-all-tools` | `preToolUse` hook (command or http); managed policy | `--output-format json` (JSONL), `-s` | Native (`--acp`) | `--resume`, `--continue` (headless id use nf) | token env var (`COPILOT_GITHUB_TOKEN`); login |
| OpenCode | None | `permission` rules allow/ask/deny; `--auto` (`opencode run --auto`) | Plugin `tool.execute.before` (throw to block) | `run --format json`; `serve` HTTP server | Native (`opencode acp`) | `-c`, `-s <id>`, `--fork` | provider keys (`opencode auth`) |
| Amp | None | Permission rules; `--dangerously-allow-all` | Plugin `tool.call` hook; legacy `delegate` rule to an external program | `-x` with `--stream-json` | Not in ACP list | nf | `AMP_API_KEY` |
| Factory Droid | Risk-based autonomy (`--auto low/medium/high`); who rates risk is nf | `--auto <level>`; `--skip-permissions-unsafe`; `permissionRules` allow/ask/block | PreToolUse hook (exec support nf) | `droid exec -o json`, `stream-jsonrpc` | Native | `-s <id>`, `--fork` | `FACTORY_API_KEY` |
| Qwen Code | `auto` approval mode, LLM classifier; in the headless mode list, fallback behaviour nf | `plan`, `default`, `auto-edit`, `auto`, `yolo`; `--yolo` | PreToolUse hook (deny works; "ask" becomes deny headless) | `-p -o json` / `stream-json` | Native (daemon, `qwen serve`) | `--continue`, `--resume [id]` | key (`/auth`) |
| Kiro CLI | None | Capability rules allow/ask/deny; headless treats ask as deny; `--trust-all-tools`, `--trust-tools` | PreToolUse hook (blocking semantics nf) | `chat --no-interactive --output-format stream-json` | Native (`kiro-cli acp`) | nf (ACP `session/load`) | `KIRO_API_KEY` |
| Goose | Smart Approval (`smart_approve`); headless nf | `auto`, `approve`, `chat`, `smart_approve` | nf | `goose run --output-format json` / `stream-json` | Native (`goose acp`) | `-r`, `-n` | provider keys |
| Kimi CLI | None; `--print` auto-approves everything | `--yolo`, `--afk`, `--plan` | PreToolUse hook (beta; fails open on other exit codes) | `--print --output-format stream-json` | Native (`kimi acp`) | `-C`, `-S/-r <id>` | login or key (nf) |
| Cline CLI | None; headless default is auto-approve | `--auto-approve true/false` | `beforeTool` plugin hook (`skip: true`), no built-in deny list | `--json` (JSONL) | Native (docs link) | nf | `cline auth`, provider keys |
| Crush | None | allow/deny tool lists; `--yolo` | PreToolUse hook (exit 2 blocks) | nf | Not in ACP list | nf | provider keys |
| Mistral Vibe | None | `ask`, `plan`, `accept-edits`, `auto-approve` agents; `--auto-approve` / `--yolo`; per-tool `permission` | `pre_tool` hook | `-p --output streaming` | Native (`vibe-acp`) | `-c`, `--resume <id>` | `MISTRAL_API_KEY` |
| Grok Build | None found | `--always-approve`; permission modes nf from official page; sandbox profiles | nf | `--output-format json` / `streaming-json` | Documented in auth page | `-c`, `-r`, `-s <id>` | `grok login` or `XAI_API_KEY` |

## 3. Notes per CLI

### Claude Code (comparison only)

Auto mode: a second model, the classifier, reviews actions instead of the user. It is the default mode in interactive terminal sessions from v2.1.283. In `-p`, Agent SDK and similar sessions it requires v2.1.281 or later and a direct Anthropic API connection; sessions that do not fetch feature flags can start in `auto` from v2.1.285 [docs: https://code.claude.com/docs/en/permission-modes]. A non-interactive `-p` run without `--permission-prompt-tool` has no prompt to fall back to; after repeated blocks the action is not run and Claude keeps working [docs, same page]. `--permission-mode dontAsk` plus `--allowedTools` is the documented locked-down CI recipe. Flags: `--permission-prompt-tool`, `--permission-prompts none`, `--output-format`, `--resume`, `--session-id`, `--no-session-persistence` [docs: https://code.claude.com/docs/en/cli-reference]. Auth: unmodified binary with the end user's own subscription or key is allowed; developers may not route consumer plan credentials on behalf of users [docs: https://code.claude.com/docs/en/legal-and-compliance]. ACP only through the Zed adapter [docs: https://agentclientprotocol.com/overview/agents].

### OpenAI Codex CLI

Auto-review is the user-facing name; "guardian" appears only as a path to the open-source policy. Enable with `approvals_reviewer = "auto_review"` together with `approval_policy = "on-request"` (default reviewer is `user`). It judges only actions that already need approval (sandbox escalations, blocked network, `request_permissions`, side-effecting app or MCP calls), looks for exfiltration, credential probing, persistent security weakening and destructive actions, denies critical risk, and fails closed on build, review, parse or timeout errors [docs: https://learn.chatgpt.com/docs/agent-approvals-security]. For headless use, `codex exec` has `--approve-for-me`, described as routing approval requests through automatic review with the workspace-write sandbox [source: `codex exec --help`, codex-cli 0.162.1; docs: https://learn.chatgpt.com/docs/non-interactive-mode]. `--full-auto` is no longer supported. Default `exec` sandbox is read-only; `workspace-write` allows edits.

Static controls: `sandbox_mode`, `approval_policy` (`on-request`, `never`, `granular`), permission profiles (`default_permissions`), execpolicy `.rules` (`--ignore-rules` skips them), yolo flag `--dangerously-bypass-approvals-and-sandbox` [docs, source]. Windows sandbox is called MXC, with `prefer_mxc` under `[features]` [docs].

Hooks: `PreToolUse` can deny (`permissionDecision: "deny"`, exit code 2, or older `decision: "block"`), rewrite input with `updatedInput`, and covers shell, `apply_patch`, MCP and other local tools; `PermissionRequest` can allow or deny when an approval is about to be requested; hooks need trust (`--dangerously-bypass-hook-trust` skips it); whether hooks run in `exec` is not stated [docs: https://learn.chatgpt.com/docs/hooks].

Headless: `exec --json` (event types `thread.started`, `turn.*`, `item.*`), `--output-schema`, `-o`, `exec resume --last|<id>`, `app-server` (experimental) [docs, source]. ACP through the `codex-acp` adapter [docs: ACP agents page]. Auth: `exec` reuses the saved login; the docs recommend an API key (`CODEX_API_KEY`) for automation and say not to use ChatGPT-managed `auth.json` in public or open-source repositories [docs: non-interactive page].

### Gemini CLI

No automatic reviewer: the policy engine page does not mention an LLM reviewer, and the release page checked (latest stable v0.64.0, 2026-10-13) lists none [docs: https://geminicli.com/docs/reference/policy-engine/; changelog: https://github.com/google-gemini/gemini-cli/releases]. Modes `default`, `autoEdit` (CLI value `auto_edit`), `plan`, `yolo` (`--approval-mode`; `--yolo` is deprecated) [docs: cli-reference.md in the repo]. Policy rules are TOML (`toolName`, `commandPrefix`, `argsPattern`, `decision` allow, deny or ask_user, `modes`, `interactive`); in non-interactive mode `ask_user` is treated as deny, and the `interactive` field lets rules target non-interactive runs only [docs]. Hooks: BeforeTool returns `decision: "deny"` (alias `block`) or exits 2; other non-zero exits are warnings and the CLI continues, so a crashing reviewer fails open [docs: https://geminicli.com/docs/hooks/reference/]. Headless: `-p`, `-o json|stream-json` with events `init`, `message`, `tool_use`, `tool_result`, `error`, `result`; exit codes 0, 1, 42, 53 [docs: https://geminicli.com/docs/cli/headless/]. `--resume` accepts `latest`, an index or a session id; `--experimental-acp` starts ACP [docs]. Auth: Google ended Gemini CLI for consumer plans and free Code Assist on 2026-06-18; enterprise and paid API keys continue [docs: https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/].

### Google Antigravity CLI (`agy`)

Auto mode appears in the CLI changelog for 1.3.5: it approves file edits and safe commands, and commands that would leave the terminal sandbox are reviewed by a separate agent; Linux and macOS only for now [changelog: https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md]. The execution-modes page and the permissions page do not describe it, and nothing says whether it works with `-p` [docs: https://antigravity.google/docs/cli/modes, https://antigravity.google/docs/permissions]. The updated permission system is available on macOS and Linux; Windows still uses the previous system [docs: permissions page]. So on Windows there is no native reviewer.

Static controls: modes `default`, `accept-edits`, `plan` (`--mode=`, not `--permission-mode`); `toolPermission` setting (`request-review`, `proceed-in-sandbox`, `always-proceed`, `strict`); allow, deny and ask lists in `~/.gemini/antigravity-cli/settings.json`; `--sandbox`; `--dangerously-skip-permissions` (since 1.3.2 it skips only tool prompts, not plan review) [docs: modes page, https://antigravity.google/docs/cli/reference; changelog]. In `-p`, workspace file reads and writes are allowed, shell commands default to Ask and are soft-denied: the run continues, exits 0 and prints a notice on stderr [docs: https://antigravity.google/docs/cli/headless]. Refused actions appear as `denied_actions` in JSON output (1.1.27) [changelog].

Hooks: `PreToolUse` (also SessionStart, PostToolUse, PreInvocation, PostInvocation, Stop) returns `decision`: `deny`, `allow`, `ask`, `force_ask`, `auto_approve`, `deny_unless_prior_grant`, plus `reason`, `overwrite`, `permissionOverrides`; config in `.agents/hooks.json`, `~/.gemini/config/hooks.json` or the CLI settings file; exit codes not documented [docs: https://antigravity.google/docs/hooks]. Headless: `--output-format text|json|stream-json`, `--input-format stream-json`, `--json-schema`, `--print-timeout` (default 5m), `-c`, `--conversation <id>`; statuses `SUCCESS`, `ERROR`, `CANCELED`, `INTERRUPTED`, `INVALID`, `WAITING`, `RUNNING`; headless uses cached credentials and fails with "authentication required" otherwise [docs: headless page]. ACP: not found. Auth rule: the FAQ says headless mode is "intended for direct scripting and CI workflows" and that wrapping `agy` in third-party coding agents is not supported on consumer plans; it recommends a Gemini Enterprise or AI Studio API key for that case [docs: https://antigravity.google/docs/faq]. Windows and stdout: a user issue reports `agy -p` hanging with redirected stdout on 1.0.6 [community: https://github.com/google-antigravity/antigravity-cli/issues/318]; the changelog says headless runs with piped output no longer hang on exit in 1.1.24 [changelog]. Windows terminal sandbox is a preview feature [docs: permissions page]; sandbox no longer needs administrator rights since 1.2.17 [changelog].

### Cursor CLI (`cursor-agent` / `agent`)

Auto-review: an allowlist, then the sandbox, then a classifier that allows, retries another approach or asks. Enabled with `--auto-review`, `/config` or `/auto-review`; steered by allow and block instructions in `permissions.json`; admins can disable it per team; v2026.10.08 added approval for recursive, force, wildcard and mirror deletes [changelog: https://cursor.com/docs/cli/changelog; the first description of the pipeline is from a search summary of the same changelog, community-mirrored at techdevnotes.com]. Headless behaviour of auto-review: not found. Print mode without `--force` only proposes changes; `-f/--force/--yolo` allows commands unless denied; `--trust` applies to headless only; `--sandbox enabled|disabled`; `--mode plan|ask`; `--approve-mcps` [docs: https://cursor.com/docs/cli/headless, https://cursor.com/docs/cli/reference/parameters]. Permissions: `Shell(...)`, `Read(...)`, `Write(...)`, `WebFetch(...)`, `Mcp(...)` in `~/.cursor/cli-config.json` or `.cursor/cli.json`; deny beats allow [docs: https://cursor.com/docs/cli/reference/permissions]. Hooks: `beforeShellExecution` and `beforeMCPExecution` accept allow, deny, ask; exit 2 is deny; invalid output blocks; `preToolUse` accepts ask but it is not enforced; the hooks page does not say which events fire in the CLI, though the changelog says hooks arrived in the CLI in January 2026 [docs: https://cursor.com/docs/hooks; changelog]. A forum report claims `~/.cursor/hooks.json` hooks only worked in the IDE [community]. ACP: `agent acp`, JSON-RPC over stdio; tool approvals arrive as `session/request_permission` with `allow-once`, `allow-always`, `reject-once`, and tools can block if the client does not answer [docs: https://cursor.com/docs/cli/acp]. Resume: `--resume [chatId]`, `--continue` [docs: parameters page]. Auth: `CURSOR_API_KEY`, `agent login`; admins can disable headless mode for an org (v2026.06.09) [docs, changelog]. Windows: PowerShell installer; enterprise hooks path `C:\ProgramData\Cursor\hooks.json` [docs].

### GitHub Copilot CLI

Two names for what looks like one feature: `/allow-all auto` (changelog 1.0.x: "auto allow-all mode that auto-approves requests an LLM judge evaluates as acceptable") and Assisted Permissions with a "permission judge" (1.0.94). `/allow-all auto` requires experimental mode (`--experimental`), the judge model is picked automatically, and enterprise policy can allow auto while blocking full allow-all or disable Assisted Permissions [changelog: https://github.com/github/copilot-cli/blob/main/changelog.md, entries up to 1.0.97 of 2026-10-13]. The official command reference documents `/permissions` modes `default`, `assisted`, `allow-all` and the `/allow-all [off|auto|show]` form [docs: https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference]. Headless behaviour of the judge: not found. Programmatic use needs `--allow-tool`, `--allow-url` or `--allow-all-tools`; `--no-ask-user`, `-s` [docs: https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically]. Hooks: `preToolUse` (command, http) returns `permissionDecision` allow, deny, ask plus `modifiedArgs`; in autopilot `ask` is denied; command hooks fail closed on crashes and non-zero exits, timeouts fail open; locations include `~/.copilot/hooks/*.json`, `.github/hooks/*.json` and a Windows policy directory `C:\ProgramData\GitHub\Copilot\policy.d` [docs: https://docs.github.com/en/copilot/reference/hooks-configuration]. `--output-format json` outputs JSONL [docs]. ACP: `copilot --acp`; listed as direct, public preview [changelog; ACP agents page]. Auth: `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`; fine-grained token with the "Copilot Requests" permission; classic `ghp_` tokens unsupported [docs: command reference].

### OpenCode

No LLM reviewer. `permission` rules (`allow`, `ask`, `deny`, patterns, last match wins); most permissions default to allow, only `doom_loop` and `external_directory` ask; `--auto` approves everything not explicitly denied [docs: https://opencode.ai/docs/permissions/]. Behaviour of `opencode run` when a rule asks: not found. Hook: plugin `tool.execute.before` can change args or throw to stop a call (the docs show it through an example); `permission.ask` is not documented [docs: https://opencode.ai/docs/plugins/]. `run --format json`, `-c`, `-s`, `--fork`, `--attach`, `serve` (HTTP, optional basic auth) and `acp` (nd-JSON over stdio) [docs: https://opencode.ai/docs/cli/]. Auth: provider keys via `opencode auth login`.

### Amp

No automatic reviewer. Permissions rules end in allow, reject, ask or delegate; `delegate` hands the decision to a program on `$PATH`; by default Amp runs tools without approval; the docs recommend the `tool.call` plugin hook over the legacy rules [docs: https://ampcode.com/manual/appendix/legacy-permissions-rules.txt; search summary of https://ampcode.com/manual, evidence partly community]. `tool.call` results: allow, reject-and-continue, modify, synthesize, error; plugins live in `.amp/plugins/` or `~/.config/amp/plugins/` and run on Bun [docs: https://ampcode.com/manual/plugin-api]. Whether plugins run in execute mode: use `--plugin-ready-timeout` so they load before the turn [docs: https://ampcode.com/docs/cli/execute-mode]. `-x`, `--stream-json`, `--stream-json-input`, `--dangerously-allow-all` [community summary plus docs]. Auth: `AMP_API_KEY` (`sgamp_` token), because the `amp login` token expires within an hour [docs: execute-mode page]. Resume in execute mode and ACP: not found.

### Factory Droid

Autonomy levels Off, Low, Medium, High cap the risk of what runs without a pause; execute commands and MCP tools carry a risk level, and commands with no policy match run if the risk is at or below the level; the docs do not say who assigns the risk or whether an LLM does [docs: https://docs.factory.com/cli/user-guides/auto-run]. `droid exec`: read-only by default; `--auto low|medium|high`; actions beyond the level fail fast with a non-zero exit; `--skip-permissions-unsafe` skips prompts but not command blocks; `ask` rules cannot be satisfied in a one-shot run [docs: https://docs.factory.com/cli/droid-exec/overview]. Output `text`, `json` (with `session_id`), `stream-jsonrpc`; resume `-s <id>`, `--fork <id>`; `FACTORY_API_KEY`. Hooks: PreToolUse returns `permissionDecision` allow, deny, ask and `updatedInput`; exit 2 blocks; whether `droid exec` runs hooks: not found [docs: https://docs.factory.com/cli/configuration/hooks-guide]. ACP: native [ACP agents page].

### Qwen Code

Auto mode (`auto`, set with `tools.approvalMode` or `/approval-mode`): an LLM classifier reviews shell commands, network calls and out-of-workspace edits; it leans toward blocking, blocks if unreachable, and falls back to manual approval after two unavailable calls or three consecutive blocks; tunable under `permissions.autoMode` (`hints.allow`, `hints.deny`, `classifyAllShell`) [docs: https://qwenlm.github.io/qwen-code-docs/en/users/features/approval-mode/]. The headless page lists `auto` among the modes for `--approval-mode`; what the fallback to manual approval does with nobody present is not found [docs: https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/]. Hooks: 22 events; PreToolUse deny works, "allow" does not skip permission checks, "ask" falls back to deny headless; exit 2 blocks [source: https://github.com/QwenLM/qwen-code/blob/main/docs/users/features/hooks.md]. `-p`, `-o json|stream-json`, `--continue`, `--resume [id]`, budget flags (exit 55, 53). ACP through `qwen serve` per the README; the ACP agents page lists it as direct [source, docs]. `--yolo` does not add a sandbox.

### Kiro CLI

No LLM approval. Capability rules (`fs_read`, `fs_write`, `shell`, `web_fetch`, `mcp`, `sandbox_network`) with allow, ask, deny; deny wins; in headless mode every ask is a deny; the user file is `~/.kiro/settings/permissions.yaml` [docs: https://kiro.dev/docs/permissions/]. `--trust-all-tools` and `--trust-tools=<ids>`; hook confirmations are denied unless `--trust-all-tools` is set [docs: https://kiro.dev/docs/cli/headless/]. Headless: `kiro-cli chat --no-interactive`, `--output-format stream-json`, `KIRO_API_KEY` (paid plans; admins must enable keys). PreToolUse hooks can block until preconditions are met; exit-code semantics not found [docs: https://kiro.dev/docs/hooks/]. `kiro-cli acp` with `session/load` (`loadSession: true`); permission requests in ACP not covered [docs: https://kiro.dev/docs/cli/acp/].

### Goose

Permission modes `auto`, `approve`, `chat`, `smart_approve` (switched with `/mode`); Smart Approval auto-approves low-risk actions and asks for others, with per-tool permissions; docs say Autonomous is the default [docs: https://goose-docs.ai/docs/guides/goose-permissions, as summarised by search; page body did not render for the fetcher, so details of how risk is judged come from community pages (deepwiki, instagit), not used]. Headless behaviour of `smart_approve`: not found. `goose run` has `--output-format json|stream-json`, `-r`, `-n`, `--no-session`, `--max-turns`; `goose acp` (stdio) [docs: https://goose-docs.ai/docs/guides/goose-cli-commands]. A hook point: not found.

### Kimi CLI

`--print` implicitly enables `--afk`, which auto-approves all tool calls, so print mode is yolo; `--yolo` and `--afk` exist; `--plan` is read-only [docs: https://moonshotai.github.io/kimi-cli/en/reference/kimi-command.html]. PreToolUse hooks (beta): exit 2 blocks, JSON `permissionDecision: "deny"` blocks, any other exit code allows, timeouts fail open (default 30 s); defined in `~/.kimi/config.toml` [docs: https://moonshotai.github.io/kimi-cli/en/customization/hooks.html]. Whether hooks run under `--print`: not found. `--output-format stream-json`, `--continue`, `--session/--resume <id>`, `kimi acp` (`--acp` deprecated).

### Cline CLI

Headless mode starts when `--json` is used, stdin is piped or stdout is redirected; `--auto-approve` defaults to true; `-p` means plan mode, not print [docs: https://docs.cline.bot/cline-cli/overview]. No built-in command allow or deny list; the docs point to a PreToolUse hook that cancels `run_commands` [same page]. The plugin page documents `beforeTool` returning `{ skip: true, reason }`, a 3-second hook timeout and `hookErrorMode` default `"ignore"` (a failed hook lets the tool run), so use `"throw"` for enforcement [docs: https://docs.cline.bot/sdk/plugins]. Resume and per-hook CLI coverage: not found. ACP: listed as direct [ACP agents page].

### Crush

`--yolo` skips all prompts; `permissions allow` / `deny` lists; deny hides tools [docs: https://github.com/charmbracelet/crush]. One hook, `PreToolUse`, Claude-compatible, exit 2 blocks, hooks can auto-approve and rewrite input; timeouts and missing interpreters mean "no opinion" so the agent proceeds; runs through an embedded POSIX shell, works on Windows without WSL; `.ps1` files must be invoked explicitly [source: https://github.com/charmbracelet/crush/blob/main/docs/hooks/README.md]. Non-interactive `crush run`, resume and output formats: not found in the files read. License FSL-1.1-MIT.

### Mistral Vibe

Per-tool `permission` in `config.toml`; agents `ask`, `plan`, `accept-edits` (default), `auto-approve`; `--auto-approve` / `--yolo`. Hooks `pre_tool` (deny or rewrite), `post_tool`, `post_agent` in `.vibe/hooks.toml` or `~/.vibe/hooks.toml`. `-p`, `--output streaming|json|text`, `--max-turns`, `--max-price`, `-c`, `--resume <id>`, `vibe-acp`, `MISTRAL_API_KEY`; Windows has `git_bash` and `powershell` tool sections [docs: https://github.com/mistralai/mistral-vibe, as summarised by the fetcher].

### Grok Build

Headless `grok -p` with `--output-format plain|json|streaming-json`, `-s`, `-r`, `-c`, `--always-approve`; `grok login` or `XAI_API_KEY`; ACP authentication is described [docs: https://docs.x.ai/build/cli/headless-scripting]. The permissions page did not load; mode names seen in cheat sheets are unverified [community]. Sandbox uses Landlock on Linux and Seatbelt on macOS, applied at startup [docs: https://docs.x.ai/build/enterprise.md, via search summary]. No reviewer found.

### Other CLIs noticed

The ACP agents page lists many more agents (Junie, Augment Code, Pi, OpenHands, Qoder CLI and others) that I did not research [docs: https://agentclientprotocol.com/overview/agents]. Aider has `--yes-always` and no classifier mentioned [community].

## 4. Implications for Agents Router

When to rely on the native reviewer

1. Claude Code: use `auto` with `-p` (v2.1.281+), pass `--permission-prompts none` or a `--permission-prompt-tool` so denied actions return to the model instead of waiting. Keep our own reviewer off unless the user wants a second opinion. The classifier needs a direct Anthropic API connection.
2. Codex: use `codex exec --approve-for-me` (workspace-write sandbox) instead of our reviewer. It judges only actions that already need approval and fails closed. Test once on the installed version that a denied action ends the turn cleanly rather than hanging.
3. Qwen Code, Cursor, Copilot, Goose: a native reviewer exists but headless behaviour is unconfirmed. Probe each (a harmless command that needs approval, a clearly denied one) before relying on it, and treat the result as per-version. Until proven, treat them as having none.
4. Antigravity on Windows: no native Auto mode, so our reviewer stays. On macOS and Linux, Auto mode could replace it once headless behaviour is confirmed.

When to inject our own reviewer, and where

1. Hook-based (the current AGY design): AGY `PreToolUse` (`decision` allow or deny; avoid `auto_approve`), Gemini CLI `BeforeTool`, Copilot `preToolUse`, Droid `PreToolUse`, Qwen `PreToolUse` (deny only; allow does not bypass checks), Kimi `PreToolUse`, Crush `PreToolUse`, Mistral Vibe `pre_tool`, Kiro `PreToolUse`, Cursor `beforeShellExecution`, Claude and Codex hooks as a fallback. Confirm per CLI that the hook fires in the headless entry point; this is unconfirmed for Codex `exec`, Droid `exec`, Cursor `-p`, Kimi `--print` and Crush.
2. Plugin-based: OpenCode `tool.execute.before` (throw), Amp `tool.call` (reject-and-continue), Cline `beforeTool` (`skip`, with `hookErrorMode: "throw"`). These run inside the CLI's runtime (Node, Bun), so the broker has to be reachable from there.
3. Permission delegation: Claude `--permission-prompt-tool`; ACP `session/request_permission` for Cursor, and in principle any ACP agent (Gemini, Copilot, OpenCode, Kiro, Goose, Kimi, Qwen, Droid, Cline, Vibe). Only Cursor's request message is confirmed in the pages read. An ACP client is a uniform place to put a reviewer, at the cost of running each CLI in ACP mode instead of its print mode.
4. Policy engines (no LLM): Gemini policy TOML, Kiro rules, Droid rules, Cursor permissions, Codex execpolicy. Use them as a cheap first layer and let our reviewer judge only what is left.

Fail-open and fail-closed behaviour differs, and the reviewer path must fail closed itself: Gemini treats unknown non-zero exits as warnings, Kimi allows on other exit codes and on timeout, Cline ignores hook errors by default, Crush treats timeouts as "no opinion", Copilot command hooks fail closed but timeouts fail open. Our hook should print an explicit deny on any internal error and make the wrapper treat a missing verdict as deny.

Defaults that approve everything: Kimi `--print` and Cline headless (default `--auto-approve true`) approve without asking; OpenCode allows most tools by default. A hook or a sandbox is required for these three, not optional.

Which CLIs need a PTY

No official page says a PTY is required. The only evidence is for `agy`: a user report of `-p` hanging with redirected stdout (1.0.6) and a changelog fix for piped-output hangs in 1.1.24. The existing project uses ConPTY for `agy`; recheck whether plain pipes now work on current versions before dropping it. Gemini CLI headless mode starts on a non-TTY or `-p`, Cline starts headless on piped stdin or redirected stdout, and Amp turns on execute mode when stdout is redirected, so these three expect pipes rather than a TTY. For all others, not found; test on Windows before assuming pipes work.

Auth and provider rules that affect the product

Claude (unmodified binary, each user with their own login) and Codex (saved login, API key recommended for CI) allow native delegation. Antigravity consumer plans do not support wrapping `agy` in another coding agent. Gemini CLI consumer login ended on 2026-06-18. Cursor, Droid, Amp, Kiro and Vibe use keys or tokens for automation. I did not find explicit statements about use inside other agents for the rest.

## 5. Sources (accessed 2026-10-14)

Claude Code
- https://code.claude.com/docs/en/permission-modes
- https://code.claude.com/docs/en/cli-reference
- https://code.claude.com/docs/en/legal-and-compliance

Codex
- https://learn.chatgpt.com/docs/agent-approvals-security (redirect from developers.openai.com/codex/agent-approvals-security)
- https://learn.chatgpt.com/docs/non-interactive-mode
- https://learn.chatgpt.com/docs/hooks
- local `codex --help`, `codex exec --help` (codex-cli 0.162.1)

Gemini CLI
- https://geminicli.com/docs/reference/policy-engine/ (raw: github.com/google-gemini/gemini-cli/blob/main/docs/reference/policy-engine.md)
- https://geminicli.com/docs/hooks/reference/
- https://geminicli.com/docs/cli/headless/
- https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/cli-reference.md
- https://github.com/google-gemini/gemini-cli/releases
- https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/

Antigravity CLI
- https://github.com/google-antigravity/antigravity-cli (README, CHANGELOG.md; latest 1.3.5)
- https://antigravity.google/docs/cli/modes
- https://antigravity.google/docs/cli/headless
- https://antigravity.google/docs/cli/reference
- https://antigravity.google/docs/hooks
- https://antigravity.google/docs/permissions
- https://antigravity.google/docs/faq
- https://github.com/google-antigravity/antigravity-cli/issues/318 (community)

Cursor
- https://cursor.com/docs/cli/headless
- https://cursor.com/docs/cli/reference/parameters
- https://cursor.com/docs/cli/reference/permissions
- https://cursor.com/docs/cli/acp
- https://cursor.com/docs/cli/changelog
- https://cursor.com/docs/hooks
- https://forum.cursor.com/t/cursor-cli-hooks/148511 (community)

GitHub Copilot CLI
- https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference
- https://docs.github.com/en/copilot/reference/hooks-configuration
- https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically
- https://github.com/github/copilot-cli/blob/main/changelog.md

OpenCode
- https://opencode.ai/docs/permissions/
- https://opencode.ai/docs/cli/
- https://opencode.ai/docs/plugins/

Amp
- https://ampcode.com/manual/plugin-api
- https://ampcode.com/docs/cli/execute-mode
- https://ampcode.com/manual/appendix/legacy-permissions-rules.txt
- https://ampcode.com/news

Droid
- https://docs.factory.com/cli/droid-exec/overview
- https://docs.factory.com/cli/configuration/hooks-guide
- https://docs.factory.com/cli/user-guides/auto-run

Qwen Code
- https://qwenlm.github.io/qwen-code-docs/en/users/features/approval-mode/
- https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/
- https://github.com/QwenLM/qwen-code (README, docs/users/features/hooks.md)

Kiro
- https://kiro.dev/docs/permissions/
- https://kiro.dev/docs/cli/headless/
- https://kiro.dev/docs/hooks/
- https://kiro.dev/docs/cli/acp/

Goose
- https://goose-docs.ai/docs/guides/goose-permissions
- https://goose-docs.ai/docs/guides/goose-cli-commands
- https://goose-docs.ai/docs/guides/running-tasks

Kimi
- https://moonshotai.github.io/kimi-cli/en/reference/kimi-command.html
- https://moonshotai.github.io/kimi-cli/en/customization/hooks.html

Cline
- https://docs.cline.bot/cline-cli/overview
- https://docs.cline.bot/sdk/plugins

Crush
- https://github.com/charmbracelet/crush (README)
- https://github.com/charmbracelet/crush/blob/main/docs/hooks/README.md

Mistral Vibe
- https://github.com/mistralai/mistral-vibe

Grok Build
- https://docs.x.ai/build/cli/headless-scripting
- https://docs.x.ai/build/enterprise.md

ACP
- https://agentclientprotocol.com/overview/agents
