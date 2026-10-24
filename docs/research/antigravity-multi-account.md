# Antigravity CLI with more than one Google account

Research date: 2026-10-16. Checked on Windows 11 with Antigravity CLI (`agy`) 1.3.6, against Google's Antigravity docs, the code of other multi-account tools, Google's issue tracker, and the strings in the `agy` binary. Credential contents were never read; only names were listed.

## Where agy keeps the login

- Google's install and auth page says the CLI keeps its login in the OS keyring: Apple Keychain, Linux Secret Service or Windows Credential Manager (https://antigravity.google/docs/install?tab=cli). On the test PC the only matching Credential Manager entry is `gemini:antigravity`, and `~/.gemini` holds no token file.
- The keyring entry is shared by every folder. Pointing `HOME` and `USERPROFILE` at an empty folder gives agy a fresh `.gemini` tree there, but it still finds the login in the keyring and answers. `GEMINI_HOME` is ignored by agy 1.3.6 (it belongs to the older Gemini CLI). So a separate folder alone doesn't separate logins.
- agy also has file-based token storage. The binary contains "Using file-based token storage because %s detected", next to the names `SSH_CONNECTION`, `SSH_TTY`, `WSL_DISTRO_NAME` and `CLOUD_SHELL`. A community skill for driving agy from Claude (oaustegard/claude-skills, `invoking-antigravity`) sets `SSH_CONNECTION`, `SSH_CLIENT` and `SSH_TTY` for the same reason, and names the file `~/.gemini/antigravity-cli/antigravity-oauth-token`.
- Verified: with `HOME` and `USERPROFILE` set to an empty folder and the SSH variables set, headless `agy -p` didn't use the keyring login. It printed "Authentication required. Please visit the URL to log in:" with a Google sign-in link and waited for a code. After an interactive sign-in in that environment, agy wrote `.gemini/antigravity-cli/antigravity-oauth-token` in the account folder, and the keyring entry of the main account stayed in place and kept working.
- This depends on agy's SSH detection, which Google doesn't document as a multi-account feature. A future agy version could change it. Two open issues on Google's tracker ask for a proper setting: google-antigravity/antigravity-cli #155 (a `GEMINI_CLI_HOME` equivalent) and #381 (a documented profile or auth-store setting); neither had a reply from Google on the research date.
- One tool (agys) strips the SSH variables because agy's remote-terminal detection sends escape-sequence queries that can leave stray characters in an interactive prompt. Headless runs aren't affected; the interactive login may show a few odd characters.
- `AGY_ACCOUNT` (also in the binary) picks one of several connected third-party accounts (for example GitHub through the Plugins Manager) for sandboxed shell commands. It doesn't select a Google login.
- agy has no login subcommand. A login starts when `agy` runs with no saved login: in an SSH-style session it shows the sign-in link and asks for the code from the redirect page instead of opening a browser.

## How other tools do it

| Tool | Technique | Touches tokens? |
|---|---|---|
| agys (MIT, macOS/Linux) | Per-account `HOME`/`USERPROFILE` and XDG folders; on macOS it rewrites the `gemini`/`antigravity` keychain item from a token file | Yes |
| caam (MIT with a rider) | Per-identity `HOME`; the agy token files are real, everything else is linked back | Yes, it copies token files |
| Antigravity Manager, CLIProxyAPI | Their own OAuth login plus a local proxy that rotates accounts | Yes, and they forward them |
| Older Gemini CLI | `GEMINI_CLI_HOME` per account, documented | No |
| Agents Router | Per-account folder plus the SSH variables, so agy keeps that account's login in a file it writes itself | No |

## What agy writes into a fresh home

`.gemini/antigravity-cli/`: `installation_id`, `jetski_state.pbtxt`, `cli.log`, `conversation_summaries.db`, `jetbox_summaries_proto.pb`, `last_check.timestamp`, `annotations/`, `bin/`, `brain/`, `builtin/`, `cache/`, `conversations/`, `crashes/`, `implicit/`, `log/`, `presence/`, `updater/`. `.gemini/config/`: `.migrated`, `mcp_config.json`, `projects/default-cli-project.json`.

## The user's setup, shared with every account

| Item | Path under `~/.gemini` | Shared? |
|---|---|---|
| MCP servers | `config/mcp_config.json` (key `mcpServers`) | Yes (written per job, with the parent channel added) |
| Hooks | `config/hooks.json` | Yes |
| Rules | `GEMINI.md`, `AGENTS.md`, `config/GEMINI.md`, `config/AGENTS.md`, `config/rules/`, `antigravity-cli/rules/` | Yes |
| Skills | `config/skills/`, `antigravity-cli/skills/` | Yes |
| Plugins | `antigravity-cli/plugins/` | Yes |
| CLI settings | `antigravity-cli/settings.json` | Yes, unless it sets `modelProvider` |
| Config | `config/config.json` | Yes, unless it sets `enableAdc` |
| Per account | the state listed above, `config/projects/`, `config/.migrated`, `antigravity/mcp_oauth_tokens.json` (MCP logins) | No |
| Identity | the keyring entry, or the token file in file mode | Never touched |

## Other login routes

- API key: `GEMINI_API_KEY` plus `"modelProvider": "gemini"` in `antigravity-cli/settings.json`. Google's FAQ points third-party use to an API key or Gemini Enterprise. It bills the API, not the user's plan.
- Application Default Credentials: `AGY_ADC_AUTH=true` after `gcloud auth application-default login` (changelog 1.3.6).
- Business sign-in with a Google Cloud project.

## Usage and limits

- The JSON result reports token counts only, with no remaining quota or reset time. `agy models` lists names only.
- Google's plans page says Pro and Ultra refresh every 5 hours with weekly limits on top; other plans refresh weekly (https://antigravity.google/docs/plans). The exact quota error wording wasn't found.
- Login errors: `You are not logged into Antigravity.`, `Authentication expired. Please log back in.`, and the headless `Authentication required. Please visit the URL to log in:`, after which a headless run waits until its print timeout. Agents Router stops such a run early.

## Terms

Google's FAQ says consumer plans don't support wrapping agy in third-party coding agents or automating account rotation (https://antigravity.google/docs/faq). Rotation stays off by default and is marked "use at your own risk" when enabled.

## Sources

- Google: Antigravity install and auth, MCP, rules, skills, plugins, hooks, plans and FAQ pages under https://antigravity.google/docs/; `agy changelog`; `agy --help`; issues #155 and #381 on github.com/google-antigravity/antigravity-cli.
- quaywin/agys `profile.go`; Dicklesworthstone/coding_agent_account_manager `internal/shallow/shallow.go`, `authfile.go`; oaustegard/claude-skills `invoking-antigravity/SKILL.md`; READMEs of lbjlaq/Antigravity-Manager and router-for-me/CLIProxyAPI.
- Local checks on 2026-10-16: names-only listings of `~/.gemini`, Credential Manager target names, a string search of the agy 1.3.6 binary, headless runs with a relocated home (with `GEMINI_HOME` only, with `HOME`/`USERPROFILE`, and with the SSH variables added), and a real sign-in of a second account.
