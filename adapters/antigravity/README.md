# Antigravity adapter (superseded)

The original, standalone MCP server for [Antigravity CLI](https://www.antigravity.google/docs/cli/) (`agy`). The core of Agents Router now runs AGY directly as `runtime: "antigravity"` ([guide](../../docs/gemini.md)), and its review gate ([Safety](../../docs/gemini.md#safety), code in [`src/agents_router/agy_review/`](../../src/agents_router/agy_review/)) replaces this adapter's reviewer.

What the core took over, and how it changed:

| Here | In the core |
|---|---|
| `review_gate.py`: rules plus a reviewer call, run as a PreToolUse hook | `agy_review/rules.py`, `hook.py`: answers every tool call with `auto_approve` or `deny`, so headless runs don't stop on a refusal |
| Direct shell commands denied; commands go through `reviewed_exec.cmd` and signed one-use approvals | Commands are judged where they are: read-only ones by rule, edit-job commands run in `agy --sandbox`, and the reviewer judges what wants out of it. No broker, no approval files |
| `reviewer_client.py`: Gemini in plan mode with the task as context | `agy_review/reviewer.py`: Gemini in plan mode (or Claude Haiku) from a bare folder, seeing the action and the policy but not the task |
| `review_policy.md` | `agy_review/policy.md`, rewritten for a reviewer without the task |
| `settings_guard.py`: edits your `~/.gemini` settings (trust, write rule, credit overages) | Your `~/.gemini` is never edited; each worker folder gets its own settings |
| `executor_workspace.py`: a temporary workspace with a hook inside the project | The hook lives in the worker's own folder (`config/hooks.json`) |
| ConPTY capture (`agy_pty.py`) | Not needed: AGY 1.3.5 and later stream JSON through a pipe |

Keep this folder only for AGY versions that print nothing to a pipe. It was last checked against AGY 1.2.5; Python 3.11+, FastMCP, ConPTY via `pywinpty`.

Read Google's terms before using either: the [Antigravity FAQ](https://antigravity.google/docs/faq) says consumer plans don't support wrapping `agy` inside third-party coding agents. Details in [provider terms](../../docs/research/provider-terms.md).

| Doc | Contents |
|---|---|
| [Setup](../../docs/antigravity/setup.md) | Install, local secrets, AGY permissions, host registration |
| [Architecture](../../docs/antigravity/architecture.md) | Execution path, review broker, tool reference, known limitations |
| [Configuration](../../docs/antigravity/configuration.md) | Environment variables and state locations |

Run the tests from this folder:

```powershell
python -B -m unittest discover -s tests
```
