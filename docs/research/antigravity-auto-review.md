# An automatic reviewer for headless Antigravity workers

Research date: 2026-10-16. Antigravity CLI (`agy`) 1.3.6 on Windows 11, model `gemini-3.8-flash-low` for every run. Each test ran in a scratch home folder (`HOME` and `USERPROFILE` pointed at it, keyring login of the main account) with a logging PreToolUse hook in `.gemini/config/hooks.json` and a one-tool test MCP server in `.gemini/config/mcp_config.json`.

## The problem

Headless `agy -p` can't ask anyone for approval. A tool call that needs a yes is refused, and the run often ends at once: status `SUCCESS`, an empty `response`, a `denied_actions` list, and on stderr `jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied.` This happens for shell commands, MCP tool calls (permission `mcp`) and reads outside the workspace (`read_file`). File edits inside the workspace are allowed by default.

Codex has auto-review (a second model judges the actions that need approval) and Claude Code has auto mode (a classifier judges risky actions). agy's own auto mode is Linux and macOS only. So Agents Router brings its own reviewer.

## What Google documents

- Hooks (https://antigravity.google/docs/hooks): `PreToolUse` gets `toolCall.name`, `toolCall.args`, `stepIdx` and the common fields `conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`, `modelName`. The answer is `decision` (`allow`, `auto_approve`, `deny`, `ask`, `force_ask`, `deny_unless_prior_grant`) plus optional `reason`, `overwrite` and `permissionOverrides`. `allow` hands the call to the next hook or the default policy; `auto_approve` "approves the tool call when the default policy would otherwise prompt" and honours explicit user rules; `deny` blocks. Hook files: workspace `.agents/hooks.json`, global `~/.gemini/config/hooks.json`, `~/.gemini/antigravity-cli/settings.json`, plugin `hooks.json`. `timeout` is in seconds, default 30. Errors, timeouts and exit codes are not documented.
- Permissions (https://antigravity.google/docs/permissions?tab=cli): `permissions.allow`, `deny` and `ask` in `settings.json` with targets like `read_file(path)`, `command(prefix)`, `mcp(server/tool)`.
- Sandbox (https://antigravity.google/docs/sandbox?tab=cli): `enableTerminalSandbox` and `toolPermission: "proceed-in-sandbox"` in settings, or `agy --sandbox` for one session. Project folders are read-write, everything else is "inaccessible", no network by default. On Windows the feature is labelled preview. The page says nothing about headless mode.
- MCP (https://antigravity.google/docs/mcp): stdio servers take `command`, `args`, `env`, `cwd`; remote servers (streamable HTTP or SSE) take `serverUrl` and `headers`. `url` and `httpUrl` are not supported.
- Prior art: onkarbadve/agy-auto (MIT) runs a PreToolUse hook with hard-deny rules, fast approval for read-only commands and workspace writes, and an LLM classifier for the rest. It sets `toolPermission: "always-proceed"` so the hook is the only gate. Agents Router copies the behaviour, not the code.

## Live tests

| Case | Settings and flags | Hook answer | Result |
|---|---|---|---|
| A | defaults | `allow` | Edit inside the workspace ran. `echo` was refused (`denied_actions: [{"action":"command","display_name":"RunCommand"}]`), the run stopped with an empty response, the MCP call and the outside read never happened. `permission_mode` in the init event: `request-review`. |
| B | defaults | `auto_approve` | Everything ran: the edit, `echo`, the MCP call (`pong-B`) and the read outside the workspace. No `denied_actions`. |
| C | defaults | `allow` plus `permissionOverrides` for each action | Same as A: `echo` refused, run stopped. The overrides did nothing in headless mode, and settings.json was not changed. |
| D | `toolPermission: "always-proceed"` | `deny` for one command, `allow` otherwise | The allowed command and the outside read ran. The denied one came back to the model as "tool call denied by pre-tool hook: <reason>" and the run went on. |
| K, K2 | `permissions.allow` with `command(...)`, `read_file(<folder>)`, `mcp(server/tool)` (K: drive letter left out, K2: with drive letter and `mcp(server/*)`) | `allow` | Everything ran in both. Allow rules work headless, with or without the drive letter. |
| H | `always-proceed`, hook timeout 5 s | exit 1 / non-JSON / sleep 15 s | All three refused: "Hook error: command failed: exit status 1", "failed to unmarshal result", and for the timeout again "command failed: exit status 1". A broken hook fails closed, even with `always-proceed`. |
| P | `--mode plan` | `auto_approve` | `git --version`, a workspace read and a workspace write all ran. Plan mode doesn't stop writes once the hook approves them. |
| S1 | `--sandbox` | `auto_approve` | Commands ran; a write to a folder under `%TEMP%` outside the workspace succeeded. |
| S2, S3 | `--sandbox`, `proceed-in-sandbox` (S3 also `enableTerminalSandbox: true`) | `allow` | Commands ran without approval; writes and reads in a `%TEMP%` folder outside the workspace succeeded. |
| S4 | as S3 | `allow` | Targets outside `%TEMP%` (under the user's home): write and read both failed with "Access is denied". Workspace writes by command and by `write_to_file` worked. |
| S5 | `--sandbox` only | `auto_approve` | Write outside `%TEMP%`: "Access is denied". `python --version` and `git --version` worked. `Invoke-WebRequest https://example.com`: "The remote name could not be resolved" (no network). The same write with `BypassSandbox: true`, approved by the hook, ran outside the sandbox and created the file. This run then hung after its last tool call until `--print-timeout`; the service was returning 503 errors at the time. |

Other facts from the hook log:

- The hook runs for every tool call, workspace reads and writes included, with its working folder set to the folder of the hooks file. It inherits agy's environment (a variable set on agy reached the hook).
- Argument names: `view_file{AbsolutePath, StartLine, EndLine}`, `write_to_file{TargetFile, CodeContent, Overwrite, Description}`, `run_command{CommandLine, Cwd, WaitMsBeforeAsync, IsDaemon, BypassSandbox}`, `call_mcp_tool{ServerName, ToolName, Arguments}`. Every call also carries `toolAction` and `toolSummary`, labels the model writes. Before calling an MCP tool the model read the tool's schema file under `.gemini/antigravity-cli/mcp/<server>/<tool>.json`.
- `artifactDirectoryPath` is `.gemini/antigravity-cli/brain/<conversation>` in the home folder. `workspacePaths` uses forward slashes.
- On Windows, run_command runs PowerShell; with `--sandbox` agy prefixes each command with its own setup.
- agy rewrites `antigravity-cli/settings.json` and drops keys that hold their default (`{"toolPermission": "request-review"}` became `{}`); a non-default `always-proceed` stayed.
- A reviewer call is a plain `agy --mode plan --print=...` run with no tools used: about 7 to 9 s and 12,000 input tokens (mostly agy's own system prompt) plus 40 output tokens. With `--json-schema` the same call took 4 turns, called the `finish` tool and used 63,000 input tokens, so the reviewer doesn't use it. `--disable-slash-commands` turns plan mode off ("--mode plan has no effect while slash command expansion is disabled").
- In a home with MCP servers configured, reviewer calls started those servers every time and took longer (28 to 44 s in the tests that succeeded, against 7 to 8 s in a bare home); the service was unstable during these runs, so the numbers are rough.

## Design and why

1. The gate answers `auto_approve` or `deny`, never `allow`. `auto_approve` works in headless mode (B) and `allow` hands the call to the headless default, which refuses it and ends the run (A). `permissionOverrides` does nothing headless (C). A `deny` reaches the model with its reason and the run continues (D), so the worker can adapt.
2. `toolPermission` stays `request-review`. With that setting, a hook that doesn't load leaves agy's headless default in place: commands, MCP calls and outside reads are refused. With `always-proceed` (agy-auto's choice) a missing hook would let everything run. Broken hooks fail closed in both cases (H), but only `request-review` covers a hook that isn't there at all. Agents Router writes this into the settings of the folders it manages.
3. Every job runs in a folder Agents Router manages, so hooks.json, settings.json and mcp_config.json there are its own and the user's `~/.gemini` stays untouched. Extra accounts already had such a folder. The primary account now gets one too (`homes/antigravity/primary`): its login is in the keyring, which agy finds from any home folder. Where the primary's login is a token file in the real home (agy does this when there is no keyring), the job keeps the real home and runs without the gate.
4. Edit jobs run with `--sandbox`. On Windows the sandbox blocks writes and reads outside the workspace and the temp folders and has no network (S4, S5), and `BypassSandbox: true` is how a command asks to leave it. So sandboxed commands are approved by rule, like Codex's workspace-write sandbox, and the reviewer judges the ones that want out. `proceed-in-sandbox` is not used: it approves commands without the hook's say (S2, S3). Temp folders stay writable inside the sandbox by design.
5. Fixed rules first, then a tool-less reviewer. Rules approve reads and edits inside the workspace, reads of the parent session's instruction files and skills, Agents Router' own `agt-parent` MCP tools, plainly read-only commands, and sandboxed commands in edit jobs. They deny credential and login paths, Agents Router' own state, writes to `.git` and agent config, force pushes, publishing and deploying, deletes outside the workspace, credential tools and encoded or downloaded code. The rest goes to the reviewer, which sees only the action, a few facts about the job and the policy, never the task or the conversation. It must answer one JSON object; anything else, a timeout or an error means deny. High and critical risk are always denied.
6. The reviewer runs on the same account, from a bare folder. It is `agy --mode plan` with the cheapest Gemini model (configurable), started with an environment marker that makes the gate deny every tool it might call, so it can't act and can't recurse. Its folder has no MCP servers and no user hooks. For an extra account, only `.gemini/antigravity-cli` is linked into that folder, because agy keeps the account's login file there. Claude Haiku through `claude -p` with no tools, no MCP servers and no hooks is the alternative (`reviewer = "claude"`).
7. Decisions are cached per job by tool and arguments (without the model's labels), so a repeated test command is reviewed once.
8. A run that still ends empty with `denied_actions` is reported as an error, with agy's explanation and, for a reviewed job whose gate never answered, a hint that the hook didn't load.

## Open points

- The sandbox doesn't protect `.git` or agent configuration inside the workspace. The gate sends sandboxed commands that name `.git/hooks`, `.git/config`, `core.hooksPath` and similar to the reviewer, but an obfuscated write would get through.
- The macOS and Linux sandbox and the managed primary folder on macOS (keychain lookup with another `HOME`) are untested.
- Reviewer latency adds up: each reviewed action costs one model call of about 12,000 input tokens.

## Sources

- https://antigravity.google/docs/hooks, https://antigravity.google/docs/permissions?tab=cli, https://antigravity.google/docs/sandbox?tab=cli, https://antigravity.google/docs/mcp, https://antigravity.google/docs/cli/headless
- `agy --help` and `agy --version` (1.3.6)
- onkarbadve/agy-auto (MIT), on GitHub
- Codex auto-review and Claude Code auto mode: see [cli-permissions-and-headless.md](cli-permissions-and-headless.md)
- Live runs on 2026-10-16 as listed above
