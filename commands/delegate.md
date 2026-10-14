---
description: Hand a task to a subagent on another of your accounts and bring back the result
argument-hint: <task description>
---

Delegate this task with the CLI Subagents tools:

$ARGUMENTS

1. Turn the request into a self-contained task: the goal, which files or folders matter, what must not be touched, and how to check the result. The subagent can't see this conversation.
2. Call `subagent_start` with that task, `cwd` set to the absolute path of the current project, and `access` set to `read` for analysis or review, or `edit` when files should change. Use `wait_seconds: 60`.
3. If the job is still running, keep working on anything that doesn't depend on it, then call `subagent_status` with `wait_seconds` until it is done.
4. Call `subagent_result`, check the work (read changed files, run the relevant tests), and report what the subagent did and which account ran it.
