---
description: Hand a task to a subagent and bring back the result
argument-hint: <task description>
---

Delegate this task with the CLI Subagents tools:

$ARGUMENTS

1. Turn the request into a self-contained task: the goal, which files or folders matter, what must not be touched, and how to check the result. The subagent can't see this conversation.
2. Call `subagent_start` with that task, `cwd` set to the absolute path of the current project, and `access` set to `read` for analysis or review, or `edit` when files should change.
3. Call `subagent_wait` with the job ID. Answer any `open_questions` with `subagent_message`, then wait again until the job finishes. A subagent can also send progress notes; they show up in the wait result.
4. Check the work (read changed files, run the relevant tests) and report what the subagent did.
