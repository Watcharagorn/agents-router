---
name: cli-subagents
description: How to run subagents through CLI Subagents (the cli-subagents-* agent types in Claude Code; subagent_start, subagent_wait, subagent_result, subagent_message elsewhere). Use it like your built-in subagent tool whenever work can be delegated, run in parallel, reviewed independently, or moved to another of the user's accounts or another agent CLI (Claude Code, Codex, Antigravity). Skip it for trivial answers and tiny edits.
---

# CLI Subagents

These tools are your subagent tool. Use them the way you would use a built-in "spawn a subagent" tool: hand over a self-contained task, keep working, collect the result, check it. Behind them, each subagent is a separate agent CLI (Claude Code, Codex or Antigravity) running on one of the user's own accounts with the same MCP servers, plugins, skills and instructions as the main setup. Choosing an account, and switching when one runs out of quota, happen on their own; you don't manage them.

## In Claude Code: use the agents

With the plugin installed, Claude Code also has three agent types: `cli-subagents:cli-subagents-claude`, `cli-subagents:cli-subagents-codex` and `cli-subagents:cli-subagents-antigravity`. Start one with your Agent tool, in the background, like any other subagent. It shows up in the user's agent list with its own color, the user can open it and watch every step the worker takes (commands, edits, messages, reasoning), and you get the usual notice when it finishes. Prefer them over `subagent_start` in Claude Code.

- Description: `<model> · <access> · <task in a few words>`, for example `Opus 5.5 · edit · fix the login test`. The user reads it to see what runs where.
- Prompt: optional option lines first (`model=opus`, `access=read`, `account=NAME`, `effort=high`, `cwd=PATH`, `timeout=SECONDS`), then the complete task written as below. `access` defaults to `edit`.
- Its final message is the worker's result, unchanged. If the worker asks something, the agent stops and tells you; answer with SendMessage to that agent and it passes the answer on. SendMessage "cancel" stops the job; anything else goes to the worker as a follow-up.

The MCP tools below still work everywhere, and they are the only option in Codex and Antigravity.

## The loop with the MCP tools

1. Start: `subagent_start` with a complete task (`prompt`), the project folder (`cwd`, absolute) and `access` (`read` for analysis and review, `edit` for changes). Claude workers on `read` can use read-only tools and shell commands only. Start several jobs before waiting if the work is independent.
2. Wait: `subagent_wait` with the job IDs. It returns when a job finishes or a subagent sends a message or question. Call it again for the jobs still running.
3. Answer: if a result shows `open_questions`, reply with `subagent_message` and `reply_to` set to the question id. To give a running subagent new instructions, send `subagent_message` without `reply_to`. In Claude Code a hook also wakes an idle session when a job finishes or asks something, so you can end your turn after starting jobs.
4. Collect and check: finished jobs come back with `outcome` and `result`. `success` means the subagent finished. Each result is delivered once, so keep what you need from it. Still read the diff, run the tests and confirm the files exist before relying on it.
5. Follow up: `subagent_message` to a finished job continues the same conversation as a new job.

## Writing the task

The subagent can't see this conversation. State the goal, the files or folders involved, what it must not touch, decisions already made, how to verify the result, and what to report back. Point to files instead of pasting them.

Optional arguments: `runtime` (`claude` by default, `codex`, `antigravity`) when a specific agent or a second opinion from another vendor helps; `model`; `effort`; `max_turns`; `timeout_seconds` (default 1800); `resume_job` to continue an earlier job's conversation; `account` to pin one account; `add_dirs`.

`access: "full"` skips the CLI's own permission checks. Use it only when the user asked for that.

## Outcomes

| outcome | What to do |
|---|---|
| `success` | Check the work |
| `usage_limit` | Every eligible account is out of quota (switching already happened); tell the user when the earliest resets |
| `no_account` | All accounts are busy or cooling down; wait or tell the user |
| `auth` | Ask the user to run `csa accounts login <name>` |
| `timeout`, `max_turns` | Narrow the task or raise the limit |
| `error`, `lost` | Read `message` and `warnings`; retry once at most |

## Per host

Read the note for the CLI you are running in once: [Claude Code](hosts/claude-code.md), [Codex](hosts/codex.md) or [Antigravity](hosts/antigravity.md). It covers how waiting works there and where to put new skills, MCP servers and instructions so every subagent shares them.

- Claude Code: `subagent_wait` can run for up to an hour; Claude Code moves it to the background and notifies you when it returns.
- Codex: waits end after 240 seconds; call `subagent_wait` again until the jobs finish.
- Antigravity: keep waits to a few minutes and call again.

## What subagents get

Subagents in the same CLI as you load the same setup you have. Subagents in another CLI get a short note listing your instruction files and skill folders to read. Every subagent has `message_parent` (progress notes), `ask_parent` (a question; it waits for your answer) and `check_parent_messages` (follow-ups you send). Subagents can't start subagents of their own. Tools built into the Claude desktop app itself (its browser or terminal panes, for example) aren't available to them; everything in your config folders and plugins is.

Never put secrets or credentials in a prompt.
