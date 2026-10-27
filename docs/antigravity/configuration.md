# Antigravity adapter: configuration

Superseded: the core Antigravity runtime replaces this adapter; its settings are in [configuration](../configuration.md).

The bootstrap script writes machine-specific config into `adapters/antigravity/.local/`, which git ignores. Keep personal paths and secrets out of tracked files.

| Variable | Meaning |
|---|---|
| `AGY_CMD` | Absolute path to `agy.exe`, or a command name on `PATH`. |
| `ANTIGRAVITY_BRIDGE_WORKSPACE_ROOT` | Default project root. Set it explicitly; the built-in fallback is the repository root. |
| `ANTIGRAVITY_BRIDGE_TRUSTED_ROOTS` | Extra trusted project roots, separated by `;` on Windows. Keep them narrow. |
| `ANTIGRAVITY_BRIDGE_BROKER_SECRET` | Path to `broker-secret.bin` (not the secret itself). |
| `ANTIGRAVITY_BRIDGE_BROKER_TOKENS` | Path to `broker-tokens.json`. |
| `ANTIGRAVITY_BRIDGE_RUNTIME_SECRET_FILE` | Path to `runtime-secret.txt`, read by `launch_mcp.py`. |
| `ANTIGRAVITY_BRIDGE_APPROVALS_DIR` | Path to the approvals folder. |
| `ANTIGRAVITY_BRIDGE_RUNTIME_SECRET` | Set by `launch_mcp.py` at startup. Never put it in a config file. |
| `AGY_PTY_ROWS`, `AGY_PTY_COLS` | ConPTY size. Defaults are 40 and 240. |

A project counts as trusted if it is inside `ANTIGRAVITY_BRIDGE_WORKSPACE_ROOT`, inside one of `ANTIGRAVITY_BRIDGE_TRUSTED_ROOTS`, or marked `trust_level = "trusted"` in `~/.codex/config.toml`.

Run records and token leases always go to `%LOCALAPPDATA%\Codex\antigravity-bridge\`, even when the secret file paths are overridden. The server always reads AGY settings from `%USERPROFILE%\.gemini\antigravity-cli\settings.json`. The bootstrap's `--agy-settings` option only changes where the bootstrap writes; it doesn't move the file the server reads.

The server speaks MCP over stdio. Register `launch_mcp.py`, not `mcp_server.py`.
