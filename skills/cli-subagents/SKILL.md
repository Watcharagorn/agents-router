---
name: cli-subagents
description: How to run subagents through CLI Subagents (the cli-subagents-* agent types in Claude Code; subagent_start, subagent_wait, subagent_result, subagent_message elsewhere). Use it like your built-in subagent tool whenever work can be delegated, run in parallel, reviewed independently, or moved to another of the user's accounts or another agent CLI (Claude Code, Codex, Antigravity). Skip it for trivial answers and tiny edits.
---

# CLI Subagents

These tools are your subagent tool. Use them the way you would use a built-in "spawn a subagent" tool: hand over a self-contained task, keep working, collect the result, check it. Behind them, each subagent is a separate agent CLI (Claude Code, Codex or Antigravity) running on one of the user's own accounts with the same MCP servers, plugins, skills and instructions as the main setup. Choosing an account, and switching when one runs out of quota, happen on their own; you don't manage them.

## In Claude Code: use the agents

With the plugin installed, Claude Code also has three agent types: `cli-subagents:cli-subagents-claude`, `cli-subagents:cli-subagents-codex` and `cli-subagents:cli-subagents-antigravity`. Start one with your Agent tool, in the background, like any other subagent. It shows up in the user's agent list with its own color, the user can open it and watch every step the worker takes (commands, edits, messages, reasoning), and you get the usual notice when it finishes. Prefer them over `subagent_start` in Claude Code.

- Description: `<model> · <access> · <task in a few words>`, for example `Opus 5.5 · edit · fix the login test`. The user reads it to see what runs where.
- Prompt: optional option lines first (`model=opus`, `access=read`, `account=NAME`, `effort=high`, `cwd=PATH`, `timeout=SECONDS`, `override=USER_WORDS` only under the Limits rule below), then the complete task written as below. `access` defaults to `edit` in the Claude relay and `read` in the Codex and Antigravity relays. MCP starts default to `edit`.
- Its final message is the worker's result, unchanged. If the worker asks something, the agent stops and tells you; answer with SendMessage to that agent and it passes the answer on. SendMessage "cancel" stops the job; anything else goes to the worker as a follow-up.

The MCP tools below still work everywhere, and they are the only option in Codex and Antigravity.

## The loop with the MCP tools

1. Start: `subagent_start` with a complete task (`prompt`), the project folder (`cwd`, absolute) and `access` (`read` for analysis and review, `edit` for changes). Claude workers on `read` get read-only tools (file reading, search, web) plus shell commands with read-only options, including `git diff`; a worker hook rejects writes, execution helpers and named secret paths. Start several jobs before waiting if the work is independent.
2. Wait: `subagent_wait` with the job IDs. It returns when a job finishes or a subagent sends a message or question. Call it again for the jobs still running.
3. Answer: if a result shows `open_questions`, reply with `subagent_message` and `reply_to` set to the question id. To give a running subagent new instructions, send `subagent_message` without `reply_to`. In Claude Code a hook also wakes an idle session when a job finishes or asks something, so you can end your turn after starting jobs.
4. Collect and check: finished jobs come back with `outcome` and `result`. `success` means the subagent finished. Collecting a result claims it and suppresses later automatic notifications. Explicit `subagent_result` or `csa follow` calls can return it again. Still read the diff, run the tests and confirm the files exist before relying on it.
5. Follow up: `subagent_message` to a finished job continues the same conversation as a new job.

## Writing the task

The subagent can't see this conversation. State the goal, the files or folders involved, what it must not touch, decisions already made, how to verify the result, and what to report back. Point to files instead of pasting them.

Optional arguments: `runtime` (`claude` by default, `codex`, `antigravity`) when a specific agent or a second opinion from another vendor helps; `model`; `effort`; `max_turns` (Claude workers only); `timeout_seconds` (default 1800); `resume_job` to continue an earlier job's conversation; `account` to pin one account; `add_dirs`.

`access: "full"` skips the CLI's own permission checks. Use it only when the user asked for that.

Codex and Antigravity workers have their own web search and page reading, which reach sites such as Reddit that block other tools, so they suit web research. Antigravity workers find YouTube videos and read their title and description, but can't watch them or read transcripts.

## Limits

Limits are enforced by csa. If a start is refused, follow the suggestion in the message; don't count quotas yourself.

Never set override on your own judgment; use it only when the user asked for more jobs in this conversation, or confirmed after you warned them and told them how much of the 5-hour window is left. Use csa's reported usage and reset time; if the window is unknown, say so. Otherwise ask the user or use the home model.

Put the user's own words in `override`, for example the relay line `override=user asked for 6 Codex agents`, the `override` field on `subagent_start`, or `--override "user asked for 6 Codex agents"` on `csa run` or `csa start`. Empty values are rejected. This bypasses only csa's job caps, quota steps, usage ceiling, pool budgets and queueing. Account and pool cooldowns after real usage-limit errors, `models`, `max_effort`, login, depth and access still apply. Override jobs count toward later starts. The text is stored and shown in job lists, follow headers and status/result summaries. A new start needs its own override; a follow-up to a finished job does not inherit it.

## Outcomes

| outcome | What to do |
|---|---|
| `success` | Check the work |
| `usage_limit` | The selected account hit a limit. Unpinned jobs try other accounts when failover is enabled. Check the message for reset times. |
| `no_account` | All accounts are busy or cooling down; wait or tell the user |
| `auth` | For an extra account, ask the user to run `csa accounts login <name>`; add `--runtime codex` or `--runtime antigravity` for non-Claude accounts. For a primary login, use the vendor CLI (`claude auth login`, `codex login` or `agy`). |
| `timeout`, `max_turns` | Narrow the task or raise the limit |
| `error`, `lost` | Read `message` and `warnings`; retry once at most |

## Per host

Read the note for the CLI you are running in once: [Claude Code](hosts/claude-code.md), [Codex](hosts/codex.md) or [Antigravity](hosts/antigravity.md). It covers how waiting works there and where to put new skills, MCP servers and instructions so every subagent shares them.

- Claude Code: `subagent_wait` can run for up to an hour; Claude Code moves it to the background and notifies you when it returns.
- Codex: waits end after 240 seconds; call `subagent_wait` again until the jobs finish.
- Antigravity: keep waits to a few minutes and call again.

## What subagents get

Subagents in the same CLI as you load the same setup you have. Subagents in another CLI get a short note listing your instruction files and skill folders to read. Claude and Codex workers get `message_parent` (progress notes), `ask_parent` (a question; it waits for your answer) and `check_parent_messages` (follow-ups you send). Antigravity workers get them only with an effective account cap of exactly 1 and a managed worker folder; the primary file-login fallback has no parent channel. See [Gemini messages](../../docs/gemini.md#messages). Subagents can't start subagents of their own. Tools built into the Claude desktop app itself (its browser or terminal panes, for example) aren't available to them; everything in your config folders and plugins is.

Never put secrets or credentials in a prompt.
