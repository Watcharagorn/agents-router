---
name: agents-router
description: How to run subagents through Agents Router (the agt-* agent types in Claude Code; subagent_start, subagent_wait, subagent_result, subagent_message elsewhere). Use it like your built-in subagent tool whenever work can be delegated, run in parallel, reviewed independently, or moved to another of the user's accounts or another agent CLI (Claude Code, Codex, Antigravity). Also use it to share agents and skills between the user's CLIs (agt share), change agt's settings (agt config), or explain what agt changed (agt log). Skip it for trivial answers and tiny edits.
---

# Agents Router

These tools are your subagent tool. Use them the way you would use a built-in "spawn a subagent" tool: hand over a self-contained task, keep working, collect the result, check it. Behind them, each subagent is a separate agent CLI (Claude Code, Codex or Antigravity) running on one of the user's own accounts with the same MCP servers, plugins, skills and instructions as the main setup. Choosing an account, and switching when one runs out of quota, happen on their own; you don't manage them.

## In Claude Code: always start a relay agent

In Claude Code, start every Agents Router job with your Agent tool and one of the three relay agent types. This holds at every depth: a subagent that hands work to Codex or Gemini starts a relay agent the same way.

| Runs in | `subagent_type` |
|---|---|
| Codex | `agents-router:agt-codex` |
| Antigravity (Gemini) | `agents-router:agt-antigravity` |
| Claude Code on another account | `agents-router:agt-claude` |

Don't run `agt run`, `agt start` or `agt follow` in your own shell, and don't call `subagent_start`. A job started that way never appears in the user's agent list, and your turn is blocked while you wait on it. A relay agent appears with its own color, the user can open it and watch every step the worker takes (commands, edits, messages, reasoning), and you get the usual notice when it finishes.

```text
Agent tool:
  subagent_type: agents-router:agt-codex
  description: Sol 6.1 high · edit · fix the login test
  run_in_background: true
  prompt: |
    model=gpt-6.1-sol
    effort=high
    access=edit

    <the complete, self-contained task>
```

- Description: `<model> · <access> · <task in a few words>`. The user reads it to see what runs where.
- Prompt: option lines first, one per line (`model=...`, `effort=...`, `access=read|edit`, and only when needed `account=NAME`, `cwd=PATH`, `timeout=SECONDS`, `override=USER_WORDS` under the Limits rule below), then a blank line and the complete task written as below. `access` defaults to `edit` in the Claude relay and `read` in the Codex and Antigravity relays.
- Start it in the background and keep working. Don't poll or wait in a shell; the notice arrives when it finishes.
- Its final message is the worker's result, unchanged. If the worker asks something, the agent stops and tells you; answer with SendMessage to that agent and it passes the answer on. While the job runs, SendMessage "cancel" stops it and anything else goes to the worker as a follow-up. After the job has finished, SendMessage to the same relay continues the worker's conversation as a new job; the relay follows it and returns its result.

The MCP tools below work everywhere. In Codex, `agt setup --host codex` installs the same three relays as native agents (`agt-codex` and so on); start them with Codex's own subagent tool as [the Codex note](hosts/codex.md) shows, and use the MCP tools there only when those agents are missing. In Antigravity, `agt setup --host antigravity` installs them as a companion plugin of subagents; start them as [the Antigravity note](hosts/antigravity.md) shows. In Claude Code, too, use the MCP tools only when the relay agent types are missing.

## The loop with the MCP tools

1. Start: `subagent_start` with a complete task (`prompt`), the project folder (`cwd`, absolute) and `access` (`read` for analysis and review, `edit` for changes). Claude workers on `read` get read-only tools (file reading, search, web) plus shell commands with read-only options, including `git diff`; a worker hook rejects writes, execution helpers and named secret paths. Start several jobs before waiting if the work is independent.
2. Wait: `subagent_wait` with the job IDs. It returns when a job finishes or a subagent sends a message or question. Call it again for the jobs still running.
3. Answer: if a result shows `open_questions`, reply with `subagent_message` and `reply_to` set to the question id. To give a running subagent new instructions, send `subagent_message` without `reply_to`. In Claude Code a hook also wakes an idle session when a job finishes or asks something, so you can end your turn after starting jobs.
4. Collect and check: finished jobs come back with `outcome` and `result`. `success` means the subagent finished. Collecting a result claims it and suppresses later automatic notifications. Explicit `subagent_result` or `agt follow` calls can return it again. Still read the diff, run the tests and confirm the files exist before relying on it.
5. Follow up: `subagent_message` to a finished job continues the same conversation as a new job.

## Writing the task

The subagent can't see this conversation. State the goal, the files or folders involved, what it must not touch, decisions already made, how to verify the result, and what to report back. Point to files instead of pasting them.

Optional arguments: `runtime` (`claude` by default, `codex`, `antigravity`) when a specific agent or a second opinion from another vendor helps; `model`; `effort`; `max_turns` (Claude workers only); `timeout_seconds` (default 1800); `resume_job` to continue an earlier job's conversation; `account` to pin one account; `add_dirs`.

`access: "full"` skips the CLI's own permission checks. Use it only when the user asked for that.

Codex and Antigravity workers have their own web search and page reading, which reach sites such as Reddit that block other tools, so they suit web research. Antigravity workers find YouTube videos and read their title and description, but can't watch them or read transcripts.

## Limits

Limits are enforced by agt. If a start is refused, follow the suggestion in the message; don't count quotas yourself.

Never set override on your own judgment; use it only when the user asked for more jobs in this conversation, or confirmed after you warned them and told them how much of the 5-hour window is left. Use agt's reported usage and reset time; if the window is unknown, say so. Otherwise ask the user or use the home model.

Put the user's own words in `override`, for example the relay line `override=user asked for 6 Codex agents`, the `override` field on `subagent_start`, or `--override "user asked for 6 Codex agents"` on `agt run` or `agt start`. Empty values are rejected. This bypasses only agt's job caps, quota steps, usage ceiling, pool budgets and queueing. Account and pool cooldowns after real usage-limit errors, `models`, `max_effort`, login, depth and access still apply. Override jobs count toward later starts. The text is stored and shown in job lists, follow headers and status/result summaries. A new start needs its own override; a follow-up to a finished job does not inherit it.

## Outcomes

| outcome | What to do |
|---|---|
| `success` | Check the work |
| `usage_limit` | The selected account hit a limit. Unpinned jobs try other accounts when failover is enabled. Check the message for reset times. |
| `no_account` | All accounts are busy or cooling down; wait or tell the user |
| `auth` | For an extra account, ask the user to run `agt accounts login <name>`; add `--runtime codex` or `--runtime antigravity` for non-Claude accounts. For a primary login, use the vendor CLI (`claude auth login`, `codex login` or `agy`). |
| `timeout`, `max_turns` | Narrow the task or raise the limit |
| `error`, `lost` | Read `message` and `warnings`; retry once at most. `model at capacity after N retries` means agt already waited and retried, and the model stayed overloaded. Fall back as your delegation rules say; do not manage another retry timer. |

## Sharing, settings and the log

Use agt's own commands for these, with `--json`. A command that needs a choice it wasn't given changes nothing and exits 3 with `needs_choice` (`ask`, `choices`, `retry`); ask the user with your CLI's own question tool and run `retry` with the answer.

- Sharing agents and skills between the user's CLIs: run `agt share enable --dry-run --json`. Show the user the whole proposal as a table (each of `items` with its `cells` per folder, the `excluded` items with their reasons, the `summary`) and ask once: as suggested, change some picks, or not now. Put each `needs_answer` item (copies that differ) in the same question. With the answers, run the dry run again with them as flags (`--resolve <item>=<label>`, `--resolve <item>=skip`, `--skip <item>@<label>`, `--only <item>=<label>[,<label>]`), then run that proposal's `command` exactly as given: it carries the flags and ends in `--yes --if-plan <id>`. Adding flags to an older `command` changes the plan, so it's refused. [The share guide](../../docs/share.md) has every command.
- Take answers from the user's request instead of asking again: "remove them" is `--remove-copies`, "keep them" is `--keep-copies`, and "use the work one" is `--resolve <item>=claude-work` with `enable`, or `--prefer claude-work` with `resolve`. Ask before `agt share resolve`, `agt share rm` or `--remove-copies` only when the request doesn't already answer it, and before `--yes` only when the dry run does more than the user asked.
- A repo's own agents and skills (`.claude/agents`, `.kiro/agents` and the like) share between that repo's CLI folders once it's listed: `agt share repo add <path>`. Inside a listed repo, item commands act on its items.
- Instruction files (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) share only when the user asks: `agt share kinds add instructions`, then `agt share adopt <label>` adds agt's marked block to a file (`--wrap` puts the file's own text in it). Only the block is shared. Sharing runs in the background once it's on (a watcher on macOS, a session-start entry in Claude Code), so don't tell the user to run `agt share sync` after an edit.
- `agt share status --json` lists what waits in `attention`: each entry has `who` (`choice`: the user decides; `action`: the user fixes something by hand; `agent`: you can run the fix) and `fix`, the commands that settle it. Offer the fix.
- Settings: `agt config show --json` lists every setting in effect with its source; `agt config set <key> <value>` and `agt config unset <key>` change one. Raising a limit or loosening safety needs `--user-said "<the user's own words>"`: pass the words from the user's request, without asking "are you sure". Without a request from the user for it, don't make that change. Never edit a limit or safety key in a settings file by hand. After a change, tell the user which file it went to (`file` in the result).
- To explain what agt changed and why, read `agt log --json` (`--all`, `--item <name>`, `--key <key>`, `--since 7d`).

## Per host

Read the note for the CLI you are running in once: [Claude Code](hosts/claude-code.md), [Codex](hosts/codex.md) or [Antigravity](hosts/antigravity.md). It covers how waiting works there and where to put new skills, MCP servers and instructions so every subagent shares them.

- Claude Code: `subagent_wait` can run for up to an hour; Claude Code moves it to the background and notifies you when it returns.
- Codex: waits end after 240 seconds; call `subagent_wait` again until the jobs finish.
- Antigravity: keep waits to a few minutes and call again.

## What subagents get

Subagents in the same CLI as you load the same setup you have. Subagents in another CLI get a short note listing your instruction files and skill folders to read. Claude and Codex workers get `message_parent` (progress notes), `ask_parent` (a question; it waits for your answer) and `check_parent_messages` (follow-ups you send). Antigravity workers get them only with an effective account cap of exactly 1 and a managed worker folder; the primary file-login fallback has no parent channel. See [Gemini messages](../../docs/gemini.md#messages). Subagents can't start subagents of their own. Tools built into the Claude desktop app itself (its browser or terminal panes, for example) aren't available to them; everything in your config folders and plugins is.

Never put secrets or credentials in a prompt.
