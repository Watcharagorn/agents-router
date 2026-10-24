# What a Claude Code session knows, and how a worker gets it

Checked 2026-10-14 against the Claude Code docs (memory, settings, permissions, claude-directory, sub-agents, skills, mcp, plugins, cli-reference, authentication). "Worker" means `claude -p` started with a different `CLAUDE_CONFIG_DIR` (another account's folder) and the same working directory. The rule for Agents Router: never move or rewrite the user's own files; only point at them.

U is the user config folder (`~/.claude`, or `CLAUDE_CONFIG_DIR`). P is the project. M is the managed-policy folder (`C:\Program Files\ClaudeCode\`, `/Library/Application Support/ClaudeCode/`, `/etc/claude-code/`).

| Context | Where it lives | Worker gets it by itself? | How Agents Router provides it |
|---|---|---|---|
| Managed CLAUDE.md, settings, MCP | M (plus registry or plist) | Yes | Nothing needed |
| Project CLAUDE.md, `.claude/CLAUDE.md`, `CLAUDE.local.md`, `.claude/rules`, `.claude/skills`, `.claude/agents`, `.mcp.json`, `.claude/settings.json` | P and parent folders | Yes, same cwd | Nothing needed |
| User CLAUDE.md | U/CLAUDE.md | No | Hard link to the same file |
| User rules, agents, commands, workflows, output styles | U/rules, U/agents, U/commands, U/workflows, U/output-styles | No | One link per folder (junction on Windows) |
| User skills | U/skills/<name> | No | One link per skill (the account keeps `skills/synced` for itself) |
| Auto memory for the project | U/projects/<project>/memory | No | Linked to the user's memory folder for that project |
| User settings (hooks, permissions, env, model, output style) | U/settings.json | No | `--settings` with a generated overlay, minus login-related keys and plugin installs |
| User and local-scope MCP servers | `~/.claude.json` (or U/.claude.json with `CLAUDE_CONFIG_DIR`) | No | `--mcp-config` with a generated file |
| Installed plugins (their skills, agents, hooks, MCP servers) | U/plugins/cache/<marketplace>/<plugin>/<version> | No | `--plugin-dir` per enabled plugin, loaded in place |
| claude.ai connectors | The claude.ai account | Only the worker account's own | Can't be shared; disable with `connectors = "off"` for an identical set |
| OAuth logins for remote MCP servers | Per config folder | No | One `/mcp` login per account |
| Login | U/.credentials.json or the macOS Keychain entry for U | No, and must not be | Each account logs in itself |
| Folder trust, history, sessions, state | U/.claude.json, U/history.jsonl, U/projects | No | Not shared; the worker writes these. Sessions are copied only when a conversation moves to another account |
| Keybindings, themes, status line | U | No | Not needed for headless workers |

Things to know:

- Under `-p`, a folder the worker account never trusted skips the project's `permissions.allow` and `additionalDirectories` (with a warning); project hooks still run. Permission rules from the `--settings` layer aren't gated.
- `--settings` sits above user, project and local settings and below managed policy, so user scalar values win over project ones in workers.
- An alternative to links is `--add-dir <home>` with `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`, but that gives the worker read access to the whole home folder.
- Not documented: whether `@import` lines work in `--append-system-prompt-file`, whether hooks from several layers all run, and whether auto memory runs in `-p` sessions started by another session.
