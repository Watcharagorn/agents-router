# AGENTS.md

Agents Router (`agt`) keeps agents, skills, MCP servers and instruction files in sync between Claude Code, Codex CLI, Kiro CLI and Antigravity, and runs subagents in those CLIs on your own accounts. These are notes for AI agents that install it for a user or work on this repository.

## Installing for a user

Run these yourself; none of them is a slash command.

```bash
pipx install git+https://github.com/Watcharagorn/agents-router   # or: pip install --user git+...
agt --version
agt setup --dry-run --account <name>=<folder> ...
agt setup --account <name>=<folder> ...
agt doctor                       # every runtime that's installed
```

The full flow, with the `--if-plan` confirmation step, is in the README's [For AI agents](README.md#for-ai-agents) section. Agents Router isn't on PyPI; the PyPI package named `agt` is a different project.

Plain `agt doctor` checks every runtime that's installed. `agt doctor --runtime <runtime>` checks one (`claude`, `codex`, `antigravity`), and `--json` prints the report as JSON.

`agt setup` installs the plugin into every installed host (`--host claude|codex|antigravity` limits it), registers and syncs account folders that are already logged in, and prints a "Still needed from you" list. It is safe to run again. Use a `codex:` or `claude:` prefix on `--account` when the folder's runtime is ambiguous.

After setup, offer the optional [delegation rules](docs/delegation.md) template. It tells the main agent which models to use per tier and keeps work projects with the home vendor. Personal projects can try another CLI first, with helper limits and a fallback to the home model when there is no room. Ask whether the user wants it and which home and try-first models they want for each tier. If they agree, run `agt rules --show` with their values, then install; otherwise skip it. Example: `agt rules --install --target claude --home-high "Opus 5.5" --try-low "Gemini 3.8 Flash high" --try-medium "Sol 6.1 high"`. Use their host and models. Nothing installs the rules without `--install`.

What it runs per host, if you need to do it by hand:

- Claude Code: `claude plugin marketplace add Watcharagorn/agents-router`, then `claude plugin install agents-router@agents-router`. Check with `claude plugin list`.
- Codex: `codex plugin marketplace add Watcharagorn/agents-router`, then `codex plugin add agents-router@agents-router`. Check with `codex plugin list`.
- Antigravity: `agy plugin install https://github.com/Watcharagorn/agents-router`. Check with `agy plugin list`.

When `claude` isn't found (the Claude desktop app often has no `claude` on PATH) or its plugin commands fail, `agt setup` merges `extraKnownMarketplaces.agents-router` (GitHub repo `Watcharagorn/agents-router`) and `enabledPlugins["agents-router@agents-router"] = true` into `settings.json` in `CLAUDE_CONFIG_DIR` or `~/.claude`. It keeps a backup next to the file, and Claude Code installs the plugin on its next start. `--claude-route settings` forces this route.

To update, run `agt update` (`--dry-run` first if you like, `--no-package` for plugins only). It reinstalls the package with pipx, uv or pip, whichever installed it, and runs `claude plugin marketplace update agents-router` then `claude plugin update agents-router@agents-router`, `codex plugin marketplace upgrade agents-router` then `codex plugin add agents-router@agents-router`, or `agy plugin install <repo URL>` again. On Windows it prints the package command instead of running it; run that command yourself. Then the user restarts the CLI.

If the plugin can't be installed at all, register only the MCP server: `claude mcp add --scope user agents-router -- agt mcp` or `codex mcp add agents-router -- agt mcp`. That loses the skill, `/agents-router:delegate` and the wake-up hooks.

Only the user can do these: browser logins (`agt accounts login <name>`, `claude` then /login, `codex login`, the first `agy` sign-in), approving permission prompts, and restarting the CLI or typing `/reload-plugins` so the plugin loads. Tell them which ones are left.

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

## Layout

| Path | Contents |
|---|---|
| `src/agents_router/cli.py` | The `agt` command |
| `src/agents_router/setup.py` | `agt setup` and `agt update`: plugin install and update per host, account registration |
| `src/agents_router/jobs.py`, `runner.py` | Job records and the background runner |
| `src/agents_router/accounts.py`, `config.py` | Account pool, cooldowns, the settings in effect |
| `src/agents_router/levels.py` | The settings files: `~/.agt/settings.toml`, `<workspace>/.agt/settings.toml` and `settings.local.toml`, and finding the workspace |
| `src/agents_router/config_cli.py`, `synclog.py` | `agt config show/set/unset`, and agt's log with `agt log` |
| `src/agents_router/share/` | `agt share`: keeping agents and skills the same across the CLIs. `triggers.py` sets up what runs it without you (the launcher, the macOS LaunchAgent, agt's hook entries), and `watch.py` is what they run, `instructions.py` shares the instruction files, `scope.py` a repo's own folders, `mcp.py` the user MCP servers (Claude's only through `claude mcp`, never by editing `.claude.json`), `plugins.py` the plugins another CLI could install (only `agt share plugins apply` installs them), and `hooks.py` the hooks you approve per CLI, which `hook_run.py` runs and translates (standard library only: the launcher runs it with `python -I`) |
| `src/agents_router/sync.py`, `fsutil.py` | Linking the shared setup into account folders |
| `src/agents_router/mcp_server.py`, `tools.py`, `messages.py`, `hooks.py` | MCP server, tools, parent messages, Claude Code hooks |
| `src/agents_router/runtimes/` | Claude Code, Codex, Antigravity and Kiro adapters (Kiro's is built from its docs and `tests/fake_kiro.py`; never run `kiro-cli login`, `logout` or `chat` in tests) |
| `src/agents_router/agy_review/` | The review gate for Antigravity workers (PreToolUse hook, fixed rules, reviewer, policy). Self-contained: no imports from the rest of the package |
| `.claude-plugin/`, `plugin.json`, `mcp_config.json`, `hooks/`, `commands/`, `skills/` | Plugin files for Claude Code, Codex and Antigravity |
| `agents/` | Claude Code relay agents (`agents-router:agt-claude` and so on) that run `agt run --follow` |
| `src/agents_router/follow.py` | `agt follow`: one readable line per worker event, with end markers for the relay agents |
| `adapters/antigravity/` | The older standalone Antigravity server |
| `docs/` | Guides, design notes and research |
