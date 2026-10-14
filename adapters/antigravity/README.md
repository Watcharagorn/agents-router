# Antigravity adapter

The original, standalone MCP server for [Antigravity CLI](https://www.antigravity.google/docs/cli/) (`agy`). The core of CLI Subagents now runs AGY directly as `runtime: "antigravity"` ([guide](../../docs/gemini.md)); this adapter remains for Windows setups that want shell commands approved by its own reviewer, and for AGY versions that print nothing to a pipe.

It runs AGY as a subagent. The host sends a task; AGY works on it in its own harness with its own login; the host gets back a summary and the path to the full answer.

Status: works on Windows; last checked against AGY 1.2.5. Python 3.11+, FastMCP, ConPTY via `pywinpty`.

- Synchronous and background delegation with any model AGY offers (Gemini, Claude, GPT-OSS)
- Command review: AGY's terminal commands go through a separate tool-less reviewer and signed one-use approvals
- Project scoping: work stays inside a trusted project root; credential stores and agent config are blocked
- Live model and flag discovery from the installed `agy`

Read Google's terms before using it: the [Antigravity FAQ](https://antigravity.google/docs/faq) says consumer plans don't support wrapping `agy` inside third-party coding agents. Details in [provider terms](../../docs/research/provider-terms.md).

| Doc | Contents |
|---|---|
| [Setup](../../docs/antigravity/setup.md) | Install, local secrets, AGY permissions, host registration |
| [Architecture](../../docs/antigravity/architecture.md) | Execution path, review broker, tool reference, known limitations |
| [Configuration](../../docs/antigravity/configuration.md) | Environment variables and state locations |

Run the tests from this folder:

```powershell
python -B -m unittest discover -s tests
```
