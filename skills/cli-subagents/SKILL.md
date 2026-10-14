---
name: cli-subagents
description: How to run subagents through CLI Subagents (subagent_start, subagent_wait, subagent_result, subagent_message). Use it like your built-in subagent tool whenever work can be delegated, run in parallel, reviewed independently, or moved to another of the user's accounts or another agent CLI (Claude Code, Codex, Antigravity). Skip it for trivial answers and tiny edits.
---

# CLI Subagents

These tools are your subagent tool. Use them the way you would use a built-in "spawn a subagent" tool: hand over a self-contained task, keep working, collect the result, check it. Behind them, each subagent is a separate agent CLI (Claude Code, Codex or Antigravity) running on one of the user's own accounts with the same MCP servers, plugins, skills and instructions as the main setup. Choosing an account, and switching when one runs out of quota, happen on their own; you don't manage them.

## The loop

1. Start: `subagent_start` with a complete task (`prompt`), the project folder (`cwd`, absolute) and `access` (`read` for analysis and review, `edit` for changes). Start several jobs before waiting if the work is independent.
2. Wait: `subagent_wait` with the job IDs. It returns when a job finishes or a subagent sends a message or question. Call it again for the jobs still running.
3. Answer: if a result shows `open_questions`, reply with `subagent_message` and `reply_to` set to the question id. To give a running subagent new instructions, send `subagent_message` without `reply_to`.
4. Collect and check: finished jobs come back with `outcome` and `result`. `success` means the subagent finished; still read the diff, run the tests and confirm the files exist before relying on it.
5. Follow up: `subagent_message` to a finished job continues the same conversation as a new job.

## Writing the task

The subagent can't see this conversation. State the goal, the files or folders involved, what it must not touch, decisions already made, how to verify the result, and what to report back. Point to files instead of pasting them.

Optional arguments: `runtime` (`claude` by default, `codex`, `antigravity`) when a specific agent or a second opinion from another vendor helps; `model`; `effort`; `max_turns`; `timeout_seconds` (default 1800); `account` to pin one account; `add_dirs`.

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
- Codex and Antigravity: waits end after a few minutes; call `subagent_wait` again until the jobs finish.

## What subagents get

Subagents in the same CLI as you load the same setup you have. Subagents in another CLI get a short note listing your instruction files and skill folders to read. Every subagent can send you progress notes, ask you a question (it waits for your answer), and read follow-ups you send. Subagents can't start subagents of their own.

Never put secrets or credentials in a prompt.
