# AGENTS.md

Notes for AI agents that install CLI Subagents for a user or work on this repository.

## Installing for a user

Run these yourself; none of them is a slash command.

```bash
pipx install git+https://github.com/lunati228/CLI-Subagents   # or: pip install --user git+...
csa --version
csa setup --dry-run --account <name>=<folder> ...
csa setup --account <name>=<folder> ...
csa doctor
```

`csa setup` installs the plugin into every installed host (`--host claude|codex|antigravity` limits it), registers and syncs account folders that are already logged in, and prints a "Still needed from you" list. It is safe to run again. Use a `codex:` or `claude:` prefix on `--account` when the folder's runtime is ambiguous.

What it runs per host, if you need to do it by hand:

- Claude Code: `claude plugin marketplace add lunati228/CLI-Subagents`, then `claude plugin install cli-subagents@cli-subagents`. Check with `claude plugin list`.
- Codex: `codex plugin marketplace add lunati228/CLI-Subagents`, then `codex plugin add cli-subagents@cli-subagents`. Check with `codex plugin list`.
- Antigravity: `agy plugin install https://github.com/lunati228/CLI-Subagents`. Check with `agy plugin list`.

When `claude` isn't found (the Claude desktop app often has no `claude` on PATH) or its plugin commands fail, `csa setup` merges `extraKnownMarketplaces.cli-subagents` (GitHub repo `lunati228/CLI-Subagents`) and `enabledPlugins["cli-subagents@cli-subagents"] = true` into `settings.json` in `CLAUDE_CONFIG_DIR` or `~/.claude`. It keeps a backup next to the file, and Claude Code installs the plugin on its next start. `--claude-route settings` forces this route.

To update, run `csa update` (`--dry-run` first if you like, `--no-package` for plugins only). It reinstalls the package with pipx, uv or pip, whichever installed it, and runs `claude plugin marketplace update cli-subagents` then `claude plugin update cli-subagents@cli-subagents`, `codex plugin marketplace upgrade cli-subagents` then `codex plugin add cli-subagents@cli-subagents`, or `agy plugin install <repo URL>` again. On Windows it prints the package command instead of running it; run that command yourself. Then the user restarts the CLI.

If the plugin can't be installed at all, register only the MCP server: `claude mcp add --scope user cli-subagents -- csa mcp` or `codex mcp add cli-subagents -- csa mcp`. That loses the skill, `/cli-subagents:delegate` and the wake-up hooks.

Only the user can do these: browser logins (`csa accounts login <name>`, `claude` then /login, `codex login`, the first `agy` sign-in), approving permission prompts, and restarting the CLI or typing `/reload-plugins` so the plugin loads. Tell them which ones are left.

Don't read, copy or print credential files. Don't edit files in `~/.cli-subagents/homes/`; `csa` rebuilds them. Don't run `csa doctor --live` or real jobs unless the user agrees, because they use quota.

## Working on the code

Python 3.11+, standard library only. Run the tests from the repository root:

```bash
# Windows (cmd): set PYTHONPATH=src;tests    PowerShell: $env:PYTHONPATH="src;tests"
export PYTHONPATH=src:tests
python -m unittest discover -s tests -t tests
```

The tests use fake CLIs (`tests/fake_claude.py`, `fake_codex.py`, `fake_agy.py`, `fake_host.py`) and temporary folders. Keep it that way: tests must never call the real `claude`, `codex` or `agy`, or touch the real `~/.claude`, `~/.codex` or `~/.gemini`. Set `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `CLI_SUBAGENTS_HOME` to temporary folders when a test needs them.

## Layout

| Path | Contents |
|---|---|
| `src/cli_subagents/cli.py` | The `csa` command |
| `src/cli_subagents/setup.py` | `csa setup` and `csa update`: plugin install and update per host, account registration |
| `src/cli_subagents/jobs.py`, `runner.py` | Job records and the background runner |
| `src/cli_subagents/accounts.py`, `config.py` | Account pool, cooldowns, `~/.cli-subagents/config.toml` |
| `src/cli_subagents/sync.py`, `fsutil.py` | Linking the shared setup into account folders |
| `src/cli_subagents/mcp_server.py`, `tools.py`, `messages.py`, `hooks.py` | MCP server, tools, parent messages, Claude Code hooks |
| `src/cli_subagents/runtimes/` | Claude Code, Codex and Antigravity adapters |
| `.claude-plugin/`, `plugin.json`, `mcp_config.json`, `hooks/`, `commands/`, `skills/` | Plugin files for Claude Code, Codex and Antigravity |
| `adapters/antigravity/` | The older standalone Antigravity server |
| `docs/` | Guides, design notes and research |
