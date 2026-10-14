# Gemini through Antigravity CLI

CLI Subagents runs Gemini (and the other models Antigravity offers) through Google's Antigravity CLI, `agy`, with `runtime: "antigravity"`.

Check Google's terms first. The [Antigravity FAQ](https://antigravity.google/docs/faq#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching) says consumer plans don't support wrapping `agy` inside third-party coding agents or automating account rotation, and points that use to a Gemini Enterprise or AI Studio API key. Use this runtime with an account route Google allows. Details are in [provider terms](research/provider-terms.md).

## Setup

1. Install Antigravity CLI from [Google's documentation](https://www.antigravity.google/docs/cli/) and run `agy` once to sign in.
2. Install `csa` (see the [Claude Code guide](claude-code.md#1-install)).
3. Check: `csa accounts --runtime antigravity` lists the binary, its version and whether it can list models.

There is nothing else to configure. Antigravity runs on its one signed-in account today: it has no setting to move its config folder, and the terms above rule out rotation on consumer plans, so extra accounts and failover are refused for this runtime. Multi-account rotation is planned as an opt-in feature, clearly marked as use at your own risk; see [design](design.md#build-order).

## Using it

```bash
csa run --runtime antigravity --model gemini-3.8-flash-high --access read "Review src/auth.py for bugs"
```

From a host, pass `runtime: "antigravity"` to `subagent_start`. `agy models` lists the model IDs your account can use.

| `access` | Antigravity mode |
|---|---|
| `read` | `--mode plan` |
| `edit` | `--mode accept-edits`: file edits allowed; shell commands are refused in headless runs |
| `full` | `accept-edits` plus `--dangerously-skip-permissions` |

Antigravity's own auto mode (a reviewer agent for commands) isn't available on every account or on Windows. Where your account has it, set `edit_permission_mode = "auto"` under `[antigravity]` in the config; AGY falls back to its default mode with a warning when it isn't available.

The prompt travels as a command-line argument, so it's limited to 24,000 characters. Point to files instead of pasting them. The job's `timeout_seconds` is passed to `agy` as `--print-timeout`.

## Messages

Antigravity workers have no channel back to the main session, so `message_parent` and `ask_parent` aren't available to them. Claude and Codex workers get those tools from an MCP server added for that one run, and `agy` has no flag for that. Follow-ups still reach an Antigravity worker. A `subagent_message` sent while the job runs is given to the worker by the runner as a follow-up turn once its current turn ends. A `subagent_message` sent to a finished job continues the same conversation as a new job.

## The standalone adapter

[`adapters/antigravity/`](../adapters/antigravity/) is the original Windows MCP server for AGY. It runs `agy` through ConPTY (older versions printed nothing to a pipe) and adds its own command reviewer, so headless runs can execute shell commands after a separate model approves each one. Use it if you need reviewed shell commands on Windows; otherwise the core runtime is simpler and needs no setup.
