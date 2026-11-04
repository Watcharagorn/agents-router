# AGENTS.md

Agents Router (`agt`) keeps agents, skills, MCP servers and instruction files in sync between Claude Code, Codex CLI, Kiro CLI and Antigravity, and runs subagents in those CLIs on your own accounts. These are notes for AI agents that install it for a user or work on this repository.

## Installing for a user

Run these yourself; none of them is a slash command.

```bash
uv tool install --managed-python git+https://github.com/Watcharagorn/agents-router   # or: pipx install git+...
agt guide setup
```

If `agt` is installed but has no `agt guide` command (older than 0.10), run `agt update` first.

`agt guide setup` prints the steps for the installed version, and you follow them:
- the dry run;
- one combined question;
- the plugin in every CLI and Claude Code profile;
- the hand-off list;
- `agt doctor`;
- repair and uninstall.

The same guide is in [skills/agents-router-setup/SKILL.md](skills/agents-router-setup/SKILL.md) for an agent without a shell. The commands `agt setup` runs per host are in [docs/hosts.md](docs/hosts.md). Agents Router isn't on PyPI; the PyPI package named `agt` is a different project.

Only the user can do these:
- browser logins;
- approving permission prompts;
- restarting the CLI or typing `/reload-plugins` so the plugin loads.

Tell them which ones are left. To run work on another Claude profile, use the relay agent with `account=<profile>`, never `env -u CLAUDE_CONFIG_DIR` or `agt run` in your shell.

Don't read, copy or print credential files. Don't edit files in `~/.agents-router/homes/`; `agt` rebuilds them. Don't run `agt doctor --live` or real jobs unless the user agrees, because they use quota.

## Working on the code

Python 3.11+, standard library only, except `ui.py`: it alone imports rich and rich-argparse, inside functions, for a person at a terminal. Agents, pipes, tests and hooks get its plain renderer and never load rich. Run the tests from the repository root (the color tests run when rich is installed):

```bash
# Windows (cmd): set PYTHONPATH=src;tests    PowerShell: $env:PYTHONPATH="src;tests"
export PYTHONPATH=src:tests
python -m unittest discover -s tests -t tests
```

When you change anything the plugin ships (agents, skills, commands, hooks, the MCP server or the `agt` code it calls), raise `version` in `.claude-plugin/plugin.json`, `pyproject.toml` and `src/agents_router/__init__.py` together. Claude Code only updates an installed plugin when that version changes.

The tests use fake CLIs (`tests/fake_claude.py`, `fake_codex.py`, `fake_agy.py`, `fake_host.py`) and temporary folders. Keep it that way: tests must never call the real `claude`, `codex`, `agy` or `kiro-cli`, or touch the real `~/.claude`, `~/.codex` or `~/.gemini`. Every test module imports `tests/helpers.py`, which points the whole test process at throwaway folders (`HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `AGT_HOME`), drops the variables of the agent session running the tests, and takes an installed `agt` off `PATH`. It also swaps `launchctl` and `osascript` for `tests/fake_launchd.py` (`AGT_LAUNCHCTL`, `AGT_OSASCRIPT`) and records agt's background spawns instead of starting them (`AGT_SPAWN_RECORD`), so no test reaches the real launchd. `fake_host.py` also stands in for `claude mcp add-json|remove`, which keep `.claude.json` where Claude Code does, and for `kiro-cli whoami`. A new test module imports it too; a test that needs its own folders still sets them.

The setup guide has one source, `src/agents_router/guides/setup.md`, which `agt guide setup` prints. After editing it, run `python3 scripts/sync_guide.py` to copy it into `skills/agents-router-setup/SKILL.md`; `tests/test_guide.py` fails while the two differ. The procedure for working here (branch, docs to move, tests, review, release) is the project skill `.claude/skills/agt-maintainer/` (Codex reads it through `.codex/skills/agt-maintainer`).

### Live checks in the sandbox

Live checks run real jobs, so they use quota and need the user's OK. Run them only in the sandbox, never against the real setup. The one exception is Kiro: the sandbox's `HOME` swap logs Kiro out, so Kiro is checked on the real Kiro login with a scratch `AGT_HOME`, as `.claude/skills/agt-maintainer/references/live-check.md` §7 describes:

```bash
scripts/sandbox.sh create      # ~/agt-sandbox: its own HOME, agt from this clone, the plugin from this clone
claude-agt-a                   # Claude Code on sandbox profile a (claude-agt-b: profile b)
agt-sandbox agt doctor         # one command inside the sandbox; agt-sandbox alone opens a sandbox shell
scripts/sandbox.sh reinstall   # after a code change (agt and the plugin again)
scripts/sandbox.sh check       # shows the sandbox and the real setup apart
scripts/sandbox.sh destroy     # logs the sandbox profiles out, then removes it and its launchers
```

The launchers `claude-agt-a`, `claude-agt-b` and `agt-sandbox` are linked into `~/.local/bin` and work from any terminal. Each sets the sandbox up for its own process only, so `claude` and `agt` in that terminal stay the user's real ones. They start in the sandbox's work folder, `agt-sandbox-work-<number>` in `$TMPDIR` (`scripts/sandbox.sh check` prints it), unless started inside the sandbox. It lies outside the home folder because Claude Code reads every `CLAUDE.md` from a session's folder up to `/`, so a folder under the home would load the user's own `~/.claude/CLAUDE.md` into sandbox sessions. Claude Code also lists the folder a session starts in as a project, and `agt share` offers those, so a session started in a real repo would put that repo in the sandbox's reach. `scripts/sandbox.sh add-profile NAME` adds a profile `.claude-NAME` with its launcher `claude-agt-NAME`.

What the sandbox keeps apart:
- The sandbox's `PATH` holds its own `agt` and links to the real `claude`, `codex` and `agy`.
- `AGT_PLUGIN_SOURCE` points `agt setup` at this clone instead of GitHub.
- `launchctl` and `osascript` are `tests/fake_launchd.py`; agt also refuses the real ones while `HOME` is borrowed.
- The background watcher is off.

The sandbox uses only named profiles (`.claude-a`, `.claude-b`). Claude Code keeps logins in the macOS keychain, and with the sandbox's `HOME` macOS finds no login keychain at all, so a login fails with "Couldn't save your login". `create` therefore makes the sandbox its own keychain, `home/Library/Keychains/agt-sandbox.keychain-db` (password `agt-sandbox`), and `env.sh` unlocks it. Sandbox logins live there and never reach the real keychain.
- The sandbox's `bin/claude` is a wrapper that runs only with a `CLAUDE_CONFIG_DIR` inside the sandbox, for every caller, workers included, so nothing reaches a real profile folder.
- `bin/codex` refuses a `CODEX_HOME` outside the sandbox.
- agt never uses the real launchd while `HOME` isn't the account's own home folder. The scenarios to run are in `.claude/skills/agt-maintainer/references/live-check.md`.

## Layout

| Path | Contents |
|---|---|
| `src/agents_router/cli.py` | The `agt` command |
| `src/agents_router/setup.py` | `agt setup` and `agt update`: plugin install and update per host and per Claude Code profile, account registration |
| `src/agents_router/guides/` | Guides `agt guide` prints (`setup.md`: install, set up, repair and uninstall, for an agent) |
| `scripts/` | `sandbox.sh` (the live-check sandbox) and `sync_guide.py` (the guide's copy in the setup skill) |
| `src/agents_router/jobs.py`, `runner.py` | Job records and the background runner |
| `src/agents_router/accounts.py`, `config.py` | Account pool, cooldowns, the settings in effect |
| `src/agents_router/roles.py`, `roles_cli.py`, `roles_grid.py`, `role_run.py`, `role_agents.py`, `roles_propose.py` | Roles: `[roles.*]` settings, `agt roles show/propose/config` and its grid, `agt run --role` (read roles fan out under a group id, edit and full roles by priority), the `agt-<role>` agent files and the proposed role table (docs/roles.md) |
| `src/agents_router/settings_write.py` | What `agt config set` and `agt roles config` share: who asks, backup, lock |
| `src/agents_router/levels.py` | The settings files: `~/.agt/settings.toml`, `<workspace>/.agt/settings.toml` and `settings.local.toml`, and finding the workspace |
| `src/agents_router/config_cli.py`, `synclog.py` | `agt config show/set/unset`, and agt's log with `agt log` |
| `src/agents_router/share/` | `agt share`: keeping agents and skills the same across the CLIs. `triggers.py` sets up what runs it without you (the launcher, the macOS LaunchAgent, agt's hook entries), and `watch.py` is what they run, `instructions.py` shares the instruction files, `scope.py` a repo's own folders, `mcp.py` the user MCP servers (Claude's only through `claude mcp`, never by editing `.claude.json`), `plugins.py` the plugins another CLI could install (only `agt share plugins apply` installs them), and `hooks.py` the hooks you approve per CLI, which `hook_run.py` runs and translates (standard library only: the launcher runs it with `python -I`) |
| `src/agents_router/sync.py`, `fsutil.py` | Linking the shared setup into account folders |
| `src/agents_router/mcp_server.py`, `tools.py`, `messages.py`, `hooks.py` | MCP server, tools, parent messages, Claude Code hooks |
| `src/agents_router/runtimes/` | Claude Code, Codex, Antigravity and Kiro adapters (Kiro's is built from its docs and `tests/fake_kiro.py`; never run `kiro-cli login`, `logout` or `chat` in tests) |
| `src/agents_router/agy_review/` | The review gate for Antigravity workers (PreToolUse hook, fixed rules, reviewer, policy). Self-contained: no imports from the rest of the package |
| `.claude-plugin/`, `plugin.json`, `mcp_config.json`, `hooks/`, `commands/`, `skills/` | Plugin files for Claude Code, Codex and Antigravity (skills include orchestrator-setup, orchestrator-rebalance and orchestrator-customize) |
| `tests/skill-evals/` | skill-creator eval cases for the roles skills, run isolated in `.agt-eval/` sandboxes; the release gate is in `.claude/skills/agt-maintainer/references/release.md` |
| `agents/` | Claude Code relay agents (`agents-router:agt-claude` and so on) that run `agt run --follow` |
| `src/agents_router/follow.py` | `agt follow`: one readable line per worker event, with end markers for the relay agents |
| `adapters/antigravity/` | The older standalone Antigravity server |
| `docs/` | Guides, design notes and research |
