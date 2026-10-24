# Limits: how other tools cap concurrency, and what the plans publish

Last checked: 2026-10-16. Vendors change these pages often; check the source before you rely on a number.

## Result in one paragraph

No vendor publishes a concurrency number for CLI agents. They publish usage windows (5 hours, sometimes weekly). Concurrency only matters because parallel jobs burn the window faster and can hit rate limits. So the plan presets in `plans.py` are **our estimates**, they apply only when the user sets `plan = "..."`, and every number can be overridden with `max_jobs`, `max_effort` and `models`. With no `plan`, nothing new is capped. People on bigger plans (a $200 or $500 tier) must not inherit numbers meant for Plus, so a plan name that has no preset adds no cap.

## What the plans publish

| Plan | Published | Not published | Source |
|---|---|---|---|
| ChatGPT Plus (Codex) | Estimated local messages per 5-hour window by model, e.g. GPT-6 Luna 350-3,000, GPT-6.1 Sol 15-160, GPT-6 Astra 5-45. "Weekly limits may also apply." | Weekly numbers, concurrency | https://learn.chatgpt.com/docs/pricing |
| ChatGPT Pro (Codex) | "Pro plans currently have no five-hour limit." Priced $100-$500. | Weekly numbers, concurrency | same page |
| ChatGPT Free (Codex) | Only the model (GPT-6 Luna in the desktop app, subject to rollout). CLI access is not listed for Free there. | Limits, reasoning effort, concurrency | same page |
| ChatGPT Business, Enterprise | Not checked in detail | Concurrency | https://learn.chatgpt.com/docs/pricing |
| Claude Pro, Max 5x, Max 20x | A 5-hour session limit and a weekly limit, shared by Claude and Claude Code. Pro is "at least 5x" Free per session. Max is 5x or 20x Pro. | Message counts, concurrency | https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan and https://claude.com/pricing |
| Claude Team, Enterprise | Not checked in detail | Concurrency | https://claude.com/pricing |
| Google AI Pro, AI Ultra (Gemini CLI, Antigravity) | Gemini CLI quota page: 1,500 requests per day for AI Pro, 2,000 for AI Ultra (1,000 for Code Assist Individual). Antigravity: quotas refresh every 5 hours. AI Pro is "4x higher than standard limits"; AI Ultra is 5x or 20x AI Pro. | Per-minute limits, concurrency, any statement on running several accounts | https://geminicli.com/docs/resources/quota-and-pricing/, https://blog.google/feed/new-antigravity-rate-limits-pro-ultra-subsribers/, https://support.google.com/gemini/answer/16275805 |

Some OpenAI and Anthropic help pages answered with 403 or 404 when I checked, so a few details come from the pricing pages only. Treat the numbers as indicative.

## What other tools do

| Tool | Concurrency per account | Usage threshold | Cooldown | Queue or refuse | License |
|---|---|---|---|---|---|
| [caam](https://github.com/Dicklesworthstone/coding_agent_account_manager) | One active account per tool; isolated profiles run several accounts side by side | `stealth.rotation.drain_headroom_ceiling` 95 (percent used) | `wrap.cooldown_duration` 60m, backoff and jitter | Neither. It switches account, waits and reruns; activating a cooling profile is refused unless `--force` | MIT with an OpenAI/Anthropic rider (do not reuse code) |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | No per-account cap in the shipped config (a `credentials.concurrency` block exists but is commented out) | A Codex quota threshold was requested (issue 5816); not confirmed | `routing.cooldown`, default 60 s legacy cooldown | Not stated | MIT |
| [OpenCodex](https://github.com/lidge-jun/opencodex) | Sends new sessions to the lowest-usage healthy account; threads keep their account | Quota routing policy; key names not found | 429 triggers cooldown and failover | Not found | MIT |
| [ai-cli-mcp](https://github.com/mkXultra/claude-code-mcp) | None; runs CLIs in the background | None | None | Neither | MIT |
| [mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail) | None per account; advisory file reservations (default lease 3600 s) | None | None | Reports conflicts | MIT with the same rider |

What we took from this:

- A usage threshold near 95 percent is common (caam), so `usage_ceiling` defaults to 0.95.
- Nobody caps concurrent jobs per account in a way a user can read. We make the cap, its source and the alternatives visible.
- A cooldown is for accounts that hit a limit. Admission control is a step earlier: it keeps us from reaching the limit with a burst of parallel jobs.

## Presets (recommended defaults, our estimates)

A preset sets the job cap and, where the plan offers fewer efforts, the highest effort.

| Runtime | Plan | Jobs per account | Highest effort |
|---|---|---|---|
| Codex | free | 1 | high |
| Codex | go | 1 | high |
| Codex | plus | 4 | none |
| Codex | pro | 8 | none |
| Codex | business | 6 | none |
| Codex | enterprise | 8 | none |
| Claude | free | 1 | medium |
| Claude | pro | 2 | none |
| Claude | max-5x (alias `max`) | 4 | none |
| Claude | max-20x | 8 | none |
| Claude | team | 3 | none |
| Claude | enterprise | 6 | none |
| Antigravity | free | 1 | none |
| Antigravity | pro | 2 | none |
| Antigravity | ultra | 4 | none |

Codex plus (4) and free (1) are the project owner's numbers. The rest scale with the plan's published usage multiple and are guesses. Raise or lower any of them with `max_jobs`.

Claude Code also reports each account's 5-hour and weekly usage in its `rate_limit_event`. Codex writes a `rate_limits` snapshot into its rollout files. agt reads those two sources and nothing else. Antigravity exposes no usage; see plans.md 3f.

## Model pools and quota steps (2026-10-16)

The owner supplied the Argon quota split: Google AI Pro and Ultra give Gemini 4 Argon a separate weekly-only quota. Gemini 3.8 Flash uses the normal 5-hour and weekly bucket. agy exposes no remaining-quota figures, so pool controls use local job starts and usage-limit errors. The `gemini-4-argon-*` model IDs are assumed and have not been tested live. See plans.md 3i.

`max_jobs_by_room` uses the existing Codex rollout snapshots and Claude rate-limit windows, ignoring reset windows. Pool budgets count job records over 7 days; pool slots use the same file locks and orphan-owner records as account slots. No vendor API calls or third-party code are needed. The account-rotation comparisons above informed the admission and cooldown design; these controls are opt-in and are not vendor concurrency limits.
