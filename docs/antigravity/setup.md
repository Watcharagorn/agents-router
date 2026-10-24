# Antigravity adapter: setup on Windows

This sets up the Antigravity (AGY) adapter as a local MCP server. The repository doesn't ship AGY, account credentials or any secrets; you create those on your own machine.

Before you start, check the [provider terms](../research/provider-terms.md). Google's Antigravity FAQ says consumer plans don't support running `agy` inside third-party coding agents. A headless flag existing doesn't settle whether your account may be used this way.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- Git
- Antigravity CLI, installed from [Google's documentation](https://www.antigravity.google/docs/cli/) and signed in interactively once
- A clone path without spaces, quotes or parentheses (AGY's hook runner breaks on them)

```powershell
git clone https://github.com/Watcharagorn/agents-router.git C:\GitHub\agents-router
Set-Location C:\GitHub\agents-router\adapters\antigravity
python --version
Get-Command agy -ErrorAction SilentlyContinue
Test-Path (Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe')
```

## 1. Create the virtual environment

The command broker looks for `.venv` next to `reviewed_exec.cmd`, so create it inside `adapters/antigravity`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements-windows.lock.txt` lists the full set of package versions the adapter was last tested with, if you need to reproduce that environment.

## 2. Generate local state and host config

`scripts/bootstrap_local.py` uses only the standard library. It doesn't install anything, start AGY or call a model. Without `--write` it only reports what it would do:

```powershell
python .\scripts\bootstrap_local.py --workspace-root C:\path\to\your\project
python .\scripts\bootstrap_local.py --workspace-root C:\path\to\your\project --write
```

`--write` creates any missing secrets and eight broker IDs under `%LOCALAPPDATA%\Codex\antigravity-bridge\`. Existing valid state is reused. Malformed state stops the script instead of being replaced. Secret values are never printed.

It also writes three files to `adapters/antigravity/.local/` (git-ignored):

| File | Use |
|---|---|
| `codex-mcp.toml` | A `[mcp_servers.antigravity_agents]` block for `~/.codex/config.toml`. |
| `mcp.json` | A generic `mcpServers` entry for other MCP hosts. |
| `agy-settings.patch.json` | The exact AGY permission entries the adapter needs. |

Pass `--agy-cmd C:\path\to\agy.exe` if AGY wasn't found. Pick a narrow project root, never a drive root, your user profile or a credential folder.

## 3. Grant AGY permissions

With no AGY sessions running:

```powershell
python .\scripts\bootstrap_local.py --workspace-root C:\path\to\your\project --write --configure-agy
```

This merges the broker command grants (`command(<adapter>/reviewed_exec.cmd <id>)`), one project-scoped `write_file(...)` rule and project trust into AGY's `settings.json`, and sets `useG1Credits` to `false`. Other settings stay as they are. It does not allow arbitrary terminal commands.

## 4. Register the MCP server

Codex: merge `.local/codex-mcp.toml` into `%USERPROFILE%\.codex\config.toml`. If an `antigravity_agents` block already exists, update it instead of adding a second one.

Claude Code: copy the `antigravity_agents` entry from `.local/mcp.json` into the `mcpServers` object of a project `.mcp.json`, or add it at user scope with `claude mcp add-json antigravity_agents '<entry JSON>' --scope user` from a shell that passes JSON arguments intact (Windows PowerShell 5.1 strips the inner quotes; use PowerShell 7 or Git Bash).

Other hosts: use the same entry in their MCP config format. The command must be the venv Python running `launch_mcp.py`, with the environment variables from the generated file. Restart the host after changing the registration.



## 5. Verify without spending quota

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

From the host, call `antigravity_current_settings` with your project path, then `antigravity_capabilities` (leave `include_live_probe` off) and `antigravity_list_models`. These run `agy` for version, help and model listing but don't send a task.

`antigravity_smoke_test`, `include_live_probe=true` and every delegate tool make real model calls and use quota.

## Troubleshooting

| Symptom | Check |
|---|---|
| `runtime-secret.txt` missing | Run the bootstrap with `--write`, and register `launch_mcp.py`, not `mcp_server.py`. |
| Broker can't find `.venv` | Create `.venv` inside `adapters/antigravity`. |
| Hook command rejects whitespace | Clone to a path without spaces. |
| Workspace untrusted | Set `ANTIGRAVITY_BRIDGE_WORKSPACE_ROOT` / `ANTIGRAVITY_BRIDGE_TRUSTED_ROOTS` to the project, or mark it trusted in Codex. |
| Every command denied | Each broker ID needs its exact `command(...)` grant in AGY settings. Rerun step 3 rather than widening permissions. |
| Empty output | Check that `pywinpty` imports. Plain pipes return nothing from `agy` on Windows. |
| Old default model after an update | Restart the MCP server, or call `antigravity_delegate_model` with an explicit model. |
| Tests fail on temp folder access | Run them from a normal terminal; some sandboxes block new temp directories. |
