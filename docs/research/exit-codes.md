# Worker CLI exit codes

Checked 2026-10-19. Local checks used only `--version` and `--help` (`codex exec --help` for Codex): Claude Code 2.1.299, codex-cli 0.162.1, Antigravity CLI 1.4.2. No model requests were made. Earlier account and stream-format research is in [claude-code-headless.md](claude-code-headless.md), [codex-multi-account.md](codex-multi-account.md), and [antigravity-multi-account.md](antigravity-multi-account.md).

## Claude Code

`claude -p --output-format stream-json` must provide a successful result and exit 0. The [CLI reference](https://code.claude.com/docs/en/cli-reference) says reaching `--max-turns` exits with an error. The [Agent SDK reference](https://code.claude.com/docs/en/agent-sdk/python#resulterror) documents process failures from error results, including `error_max_turns` and API failures. A failed final API request can still use `subtype: "success"`; `is_error: true` distinguishes it. Therefore the subtype alone cannot establish success.

Anthropic's [Python SDK subprocess transport](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py) reads the stream, waits for process completion, and raises `ProcessError` on nonzero exit. The CLI implementation is not published in that repository. These sources establish nonzero failure semantics; they do not promise a numeric code for every Claude error subtype. Tests model max-turn exhaustion as exit 1 and retain CSA's `max_turns` outcome. Auth and usage errors retain their specific outcomes, including when their result subtype is `success`.

## Codex

The [non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode) describes the JSONL event stream. The Apache-2.0 [Codex source at rust-v0.162.0](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/exec/src/lib.rs) supplies the exit policy: a terminal, non-retried server error, failed/interrupted primary turn, or failed server-request handling sets `error_seen`; the command exits 1 when that flag is set. A completed turn does not reset it. Some shutdown errors only log warnings, so those do not independently require failure.

The [JSONL processor](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs) emits `turn.completed` for completed turns and `turn.failed` for failed turns. An interrupted turn initiates shutdown without a completion event. Retries, reroutes, and other recoverable errors may appear alongside an eventual successful completion. Keep them as warnings. Later fatal stream/stderr errors can still explain a nonzero process exit.

OpenAI's [TypeScript SDK](https://github.com/openai/codex/blob/rust-v0.162.0/sdk/typescript/src/exec.ts) waits after consuming stdout and rejects nonzero exits or termination signals. CSA follows that policy and retains the final assistant text. No upstream implementation code was copied.

## Antigravity

Google's [headless-mode documentation](https://antigravity.google/docs/cli/headless#handle-exit-codes-and-errors) says successful runs exit 0; failures exit nonzero, write a reason to stderr, and expose `status`/`error` in JSON modes. Its unknown-model example exits 1 with `status: "ERROR"`. Other reported statuses include `CANCELED`, `INTERRUPTED`, `INVALID`, `WAITING`, and `RUNNING`; none establishes success. The CLI implementation is not published in the issue repository.

CSA requires `status: "SUCCESS"` plus exit 0. It retains its existing exception for an empty response with denied actions: that is an error even at exit 0. Auth/quota diagnostics keep their specific outcomes and any response text survives a later process failure.

## Runner stops and acceptance rule

No researched source documents a nonzero exit that should count as success. A valid partial answer remains useful, but the job fails if the CLI exits nonzero or its exit status is unknown (`None`). Generic errors name the exit status.

The runner's `_pump` returns `timeout` or `cancelled` when it kills a worker. `_attempt` creates that outcome directly instead of calling `parser.finish`, so these stop reasons already override success-looking events. Successful result text is also retained in `parser.last_text` for this path. Parser progress, including Codex warnings, is still written to job status. Regression tests exercise the fake CLIs after success events, including real runner kills, normal exits, late auth/quota failures, max-turn exhaustion, and recovered Codex errors.
