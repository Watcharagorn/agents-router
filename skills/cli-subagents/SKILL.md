---
name: cli-subagents
description: Delegate a bounded coding, review or research task to a subagent that runs in another installed agent CLI under one of the user's own accounts (CLI Subagents MCP tools subagent_start, subagent_status, subagent_result). Use when parallel work, a second opinion, or spreading load across the user's accounts would help. Skip it for trivial answers and tiny edits.
---

# CLI Subagents

The `subagent_*` tools start a separate agent CLI process (Claude Code today) on one of the user's accounts. Every account has the same MCP servers, plugins, skills and settings as the main setup, so a subagent can do anything this session could. You stay responsible for the plan, for checking the work and for the final answer.

## When to delegate

- Independent pieces of work that can run in parallel with yours.
- A review or second opinion on a design, diff or bug.
- Long research or reading that would fill your own context.
- Heavy work the user wants to run on their other accounts instead of this one.

Don't delegate a task you can't describe with a clear outcome, and never give the same files to two subagents that both edit.

## Starting a job

Call `subagent_start` with:

- `prompt`: a complete, self-contained task. The subagent can't see this conversation. State the goal, the files or folders involved, what it must not touch, facts it shouldn't re-check, how to verify the result, and what to report back.
- `cwd`: the absolute path of the project. Never a drive root, home folder or config folder.
- `access`: `read` for analysis and review (no edits), `edit` for changes (the CLI's own auto-mode safety checks stay on). Use `full` only when the user explicitly asked to skip all permission checks.
- Optional: `model` (`haiku`, `sonnet`, `opus` or a full ID), `effort`, `max_turns`, `timeout_seconds` (default 1800), `account` to pin one account, `add_dirs` for extra folders.
- `wait_seconds`: up to 600. With a value, short jobs return their result directly.

`subagent_start` returns a `job_id` right away. Start several jobs before waiting on any of them if the work is independent.

## Following up

- `subagent_status` with `wait_seconds` (for example 120) waits efficiently. Don't poll in a tight loop.
- `subagent_result` returns `outcome`, `result` (the subagent's final answer), `account`, `session_id`, `cost_usd` and `warnings`.
- `resume_job` on a new `subagent_start` continues an earlier job's session on the same account, for follow-up instructions.
- `subagent_cancel` stops a job and everything it started.
- `subagent_accounts` shows accounts, login state, last known usage and cooldowns, without using any quota.

## Outcomes

| outcome | Meaning | What to do |
|---|---|---|
| `success` | The subagent finished | Check the work before you rely on it |
| `usage_limit` | The account hit its limit and failover is off | Start again later, pin another account, or ask the user |
| `no_account` | Every account is cooling down or busy | Tell the user when the earliest one resets |
| `auth` | The account needs a new login | Ask the user to run `csa accounts login <name>` |
| `timeout`, `max_turns` | Stopped by a limit you set | Narrow the task or raise the limit |
| `error`, `lost` | The CLI or runner failed | Read `message` and `warnings`; retry once at most |

`success` only means the subagent produced a final answer. Read the diff, run the tests, and confirm the requested files exist.

## Rules

- Never send secrets, credentials or unrelated personal data in a prompt.
- Subagents can't start their own subagents.
- Report which account ran the work when the user cares about usage.
