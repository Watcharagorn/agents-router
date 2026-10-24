# Model capacity errors

Researched 2026-10-19, before implementing agt's capacity retries. These failures describe temporary provider overload. Account quota failures keep their existing cooldown and failover path.

## Codex

The owner's 2026-10-19 `gpt-6.1-sol` job exited with `Selected model is at capacity. Please try a different model.` The same text is the display string for `ServerOverloaded` in OpenAI's [error definitions](https://github.com/openai/codex/blob/70b3bb6d8814ab3f2045238b4ae14c43da99783b/codex-rs/protocol/src/error.rs). Its error-info value is `server_overloaded`, distinct from `usage_limit_exceeded`. The [SSE error parser](https://github.com/openai/codex/blob/70b3bb6d8814ab3f2045238b4ae14c43da99783b/codex-rs/codex-api/src/sse/responses_error.rs) maps backend code `server_is_overloaded` to this error. The API-layer display string is `server overloaded`.

The [exec JSONL processor](https://github.com/openai/codex/blob/70b3bb6d8814ab3f2045238b4ae14c43da99783b/codex-rs/exec/src/event_processor_with_jsonl_output.rs) emits `error.message` and `turn.failed.error.message`; these events flatten the structured error into text. agt recognizes those error messages, the overload codes when present in error fields, and stderr. Assistant output is excluded.

Codex already retries HTTP failures and stream interruptions. Its [provider configuration](https://github.com/openai/codex/blob/70b3bb6d8814ab3f2045238b4ae14c43da99783b/codex-rs/model-provider-info/src/lib.rs) defaults to `request_max_retries = 4` and `stream_max_retries = 5`, also covered by the [official configuration reference](https://developers.openai.com/codex/config-reference/). agt leaves these settings alone and waits for the process to finish. A recovered overload followed by a successful turn stays successful.

OpenAI's source is [Apache-2.0](https://github.com/openai/codex/blob/70b3bb6d8814ab3f2045238b4ae14c43da99783b/LICENSE). This implementation uses the published signals, without copying its retry implementation.

## Claude Code

Anthropic's [API error reference](https://platform.claude.com/docs/en/api/errors) identifies HTTP `529` and `overloaded_error` as temporary overload; `429` and `rate_limit_error` identify limits. The official Agent SDK's [TypeScript declarations](https://unpkg.com/@anthropic-ai/claude-agent-sdk@latest/sdk.d.ts), inspected on 2026-10-19, define `SDKAPIRetryMessage`: `type: system`, `subtype: api_retry`, `attempt`, `max_retries`, `retry_delay_ms`, `error_status`, and `error`. These announce retries already managed by Claude Code. Terminal results can carry `api_error_status`; the [Python SDK types](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py) document that field from CLI v2.1.110.

agt does not restart a worker upon `api_retry`. It retries only a final overload failure. A stale overload retry event followed by a different terminal error must not trigger a capacity retry. A successful result always takes precedence over earlier retry warnings.

## Antigravity

Google's [Antigravity changelog](https://www.antigravity.google/docs/changelog) documents extended retries for `503 Unavailable` in v1.1.28 (2026-09-09), then in-process exponential backoff preserving completed tool outputs in v1.2.1 (2026-09-11). Unrecovered 503 and 429 errors receive user-facing messages. Google's [Gemini troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting) distinguishes `503 UNAVAILABLE` from `429 RESOURCE_EXHAUSTED` and recommends exponential backoff.

There is no verified public Antigravity capacity-specific result schema. agt recognizes explicit overload/no-capacity text and structured 503 errors, or the combined `503 UNAVAILABLE` spelling, only in failed results and stderr. A bare `UNAVAILABLE` or a model's discussion of capacity is insufficient. This is a conservative inference from the documented provider signals; it does not assume every availability failure means quota exhaustion.

## agt policy

Each runtime defaults to three retries, waiting 30, 60 and 120 seconds. `capacity_retries` sets the retry count (zero disables it); `capacity_retry_seconds` sets the positive initial delay, doubled on each retry. The runner retains the same account lease and resumes its session when available, otherwise repeating the current task prompt. It records the waits in events and status. Cancellation and the task timeout remain active during waits. After exhaustion the outcome is `error`, with `model at capacity after 3 retries`; overload never enters quota cooldown or account failover.
