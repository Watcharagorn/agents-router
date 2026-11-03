---
name: agents-router
description: How to run subagents through Agents Router (the agt-* agent types in Claude Code; subagent_start, subagent_wait, subagent_result, subagent_message elsewhere). Use it like your built-in subagent tool whenever work can be delegated, run in parallel, reviewed independently, or moved to another of the user's accounts or another agent CLI (Claude Code, Codex, Antigravity, Kiro). Use it for roles too, to call the Reviewer, Builder, Verifier or Decision Maker (agt-reviewer and the other agt-<role> agents) unprompted and to make one change to a role when asked (swap or add a provider, a model or effort, a role on or off, a custom role) with agt roles config. Also use it to share agents and skills between CLIs (agt share), change settings such as max_jobs (agt config), or explain what agt changed (agt log). Not for redesigning the whole role table (orchestrator-setup), "rebalance my roles" (orchestrator-rebalance) or installing agt (agents-router-setup). Skip it for trivial answers and tiny edits.
---

# Agents Router

These tools are your subagent tool. Use them the way you would use a built-in "spawn a subagent" tool: hand over a self-contained task, keep working, collect the result, check it. Behind them, each subagent is a separate agent CLI (Claude Code, Codex, Antigravity or Kiro) running on one of the user's own accounts with the same MCP servers, plugins, skills and instructions as the main setup. Choosing an account, and switching when one runs out of quota, happen on their own; you don't manage them, unless the user names a profile or account. Then pass it as `account=` (`~/.claude-work` is `claude-work`), never the profile this session runs in. To install, update or repair agt itself, use the `agents-router-setup` skill (or run `agt guide setup`).

## In Claude Code: always start a relay agent

In Claude Code, start every Agents Router job with your Agent tool and one of the four relay agent types. This holds at every depth: a subagent that hands work to Codex, Gemini or Kiro starts a relay agent the same way.

| Runs in | `subagent_type` |
|---|---|
| Codex | `agents-router:agt-codex` |
| Antigravity (Gemini) | `agents-router:agt-antigravity` |
| Kiro CLI | `agents-router:agt-kiro` |
| Claude Code on another account | `agents-router:agt-claude` |

Don't run `agt run`, `agt start` or `agt follow` in your own shell, and don't call `subagent_start`. A job started that way never appears in the user's agent list, and your turn is blocked while you wait on it. The same goes for reaching another Claude profile: never `env -u CLAUDE_CONFIG_DIR` or `CLAUDE_CONFIG_DIR=... claude` in your shell; put `account=<profile>` on the relay agent, and agt runs that profile with its own environment. A relay agent appears with its own color, the user can open it and watch every step the worker takes (commands, edits, messages, reasoning), and you get the usual notice when it finishes.

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
- Prompt: option lines first, one per line (`model=...`, `effort=...`, `access=read|edit`, and only when needed `account=NAME`, `cwd=PATH`, `timeout=SECONDS`, `override=USER_WORDS` under the Limits rule below), then a blank line and the complete task written as below. `access` defaults to `edit` in the Claude relay and `read` in the Codex, Antigravity and Kiro relays.
- Start it in the background and keep working. Don't poll or wait in a shell; the notice arrives when it finishes.
- Its final message is the worker's result, unchanged. If the worker asks something, the agent stops and tells you; answer with SendMessage to that agent and it passes the answer on. While the job runs, SendMessage "cancel" stops it and anything else goes to the worker as a follow-up. After the job has finished, SendMessage to the same relay continues the worker's conversation as a new job; the relay follows it and returns its result.

The MCP tools below work everywhere. In Codex, `agt setup --host codex` installs the Claude, Codex and Antigravity relays as native agents (`agt-codex` and so on); start them with Codex's own subagent tool as [the Codex note](hosts/codex.md) shows, and use the MCP tools there only when those agents are missing. In Antigravity, `agt setup --host antigravity` installs them as a companion plugin of subagents; start them as [the Antigravity note](hosts/antigravity.md) shows. Kiro has no native relay in Codex or Antigravity; start Kiro jobs there with the MCP tools (`runtime` `kiro`). In Claude Code, too, use the MCP tools only when the relay agent types are missing.

## The loop with the MCP tools

1. Start: `subagent_start` with a complete task (`prompt`), the project folder (`cwd`, absolute) and `access` (`read` for analysis and review, `edit` for changes). Claude workers on `read` get read-only tools (file reading, search, web) plus shell commands with read-only options, including `git diff`; a worker hook rejects writes, execution helpers and named secret paths. Start several jobs before waiting if the work is independent.
2. Wait: `subagent_wait` with the job IDs. It returns when a job finishes or a subagent sends a message or question. Call it again for the jobs still running.
3. Answer: if a result shows `open_questions`, reply with `subagent_message` and `reply_to` set to the question id. To give a running subagent new instructions, send `subagent_message` without `reply_to`. In Claude Code a hook also wakes an idle session when a job finishes or asks something, so you can end your turn after starting jobs.
4. Collect and check: finished jobs come back with `outcome` and `result`. `success` means the subagent finished. Collecting a result claims it and suppresses later automatic notifications. Explicit `subagent_result` or `agt follow` calls can return it again. Still read the diff, run the tests and confirm the files exist before relying on it.
5. Follow up: `subagent_message` to a finished job continues the same conversation as a new job.

## Writing the task

The subagent can't see this conversation. State the goal, the files or folders involved, what it must not touch, decisions already made, how to verify the result, and what to report back. Point to files instead of pasting them.

Optional arguments: `runtime` (`claude` by default, `codex`, `antigravity`, `kiro`) when a specific agent or a second opinion from another vendor helps; `model`; `effort`; `max_turns` (Claude workers only); `timeout_seconds` (default 1800); `resume_job` to continue an earlier job's conversation; `account` to pin one account; `add_dirs`.

`access: "full"` skips the CLI's own permission checks. Use it only when the user asked for that.

Codex and Antigravity workers have their own web search and page reading, which reach sites such as Reddit that block other tools, so they suit web research. Antigravity workers find YouTube videos and read their title and description, but can't watch them or read transcripts. Kiro workers on `read` can search and fetch from the web too.

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
| refused: "this session runs in" / "No other Claude profile" | The profile this session runs in never takes its own jobs. Name another profile (`account=claude-work`; `subagent_accounts` lists them), use a built-in subagent, or start it with `runtime` codex. |
| `auth` | For an extra account, ask the user to run `agt accounts login <name>`; add `--runtime codex` or `--runtime antigravity` for non-Claude accounts. For a primary login, use the vendor CLI (`claude auth login`, `codex login` or `agy`). |
| `timeout`, `max_turns` | Narrow the task or raise the limit |
| `error`, `lost` | Read `message` and `warnings`; retry once at most. `model at capacity after N retries` means agt already waited and retried, and the model stayed overloaded. Fall back as your delegation rules say; do not manage another retry timer. |

## Sharing, settings and the log

Use agt's own commands for these, with `--json`. A command that needs a choice it wasn't given changes nothing and exits 3 with `needs_choice` (`ask`, `choices`, `retry`); ask the user with your CLI's own question tool and run `retry` with the answer.

- Setting agt up: `agt setup --dry-run --json` proposes the plugin install and sharing together; ask once as below, then run its `command`. Sharing agents and skills between the user's CLIs later: run `agt share enable --dry-run --json`. Show the user the whole proposal as a table (each of `items` with its `cells` per folder, the `excluded` items with their reasons, the `summary`) and ask once: as suggested, change some picks, or not now. Put each `needs_answer` item (copies that differ) in the same question. With the answers, run the dry run again with them as flags (`--resolve <item>=<label>`, `--resolve <item>=skip`, `--skip <item>@<label>`, `--only <item>=<label>[,<label>]`), then run that proposal's `command` exactly as given: it carries the flags and ends in `--yes --if-plan <id>`. Adding flags to an older `command` changes the plan, so it's refused. [The share guide](../../docs/share.md) has every command.
- Take answers from the user's request instead of asking again: "remove them" is `--remove-copies`, "keep them" is `--keep-copies`, and "use the work one" is `--resolve <item>=claude-work` with `enable`, or `--prefer claude-work` with `resolve`. Ask before `agt share resolve`, `agt share rm` or `--remove-copies` only when the request doesn't already answer it, and before `--yes` only when the dry run does more than the user asked.
- A repo's own agents and skills (`.claude/agents`, `.kiro/agents` and the like) share between that repo's CLI folders once it's listed: `agt share repo add <path>`. Inside a listed repo, item commands act on its items.
- Instruction files (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) share only when the user asks: `agt share kinds add instructions`, then `agt share adopt <label>` adds agt's marked block to a file (`--wrap` puts the file's own text in it). Only the block is shared. User MCP servers share only when the user asks too: `agt share kinds add mcp` (`--mcp on` with `enable` or `agt setup`); they're items named `mcp/<name>`, and a server with a secret written into it never leaves its CLI. Hooks (`agt share kinds add hooks`) are only listed until the user approves each one for a CLI with `agt share pick hooks/<name> --in <label>`: show them `agt share pick hooks/<name>` (what it runs, and whether it can run there) and never approve one they didn't ask for. Plugins share between Claude Code and Codex folders only when the user asks: `agt share kinds add plugins` shows what it would install and exits 3 until `--yes`, so show the user that list first. A plugin that waits for an answer (the other CLI would leave parts out, would run its hooks without asking, or already has it for a project) installs only after the user says yes: `agt share pick plugins/<id> --in <label>`, or `--not-in <label>` to keep it out. Never answer one for them. With plugins off, `agt share plugins` lists what another CLI could install; install one only when the user asks, with `agt share plugins apply <plugin>:<label> --yes`. Sharing runs in the background once it's on (a watcher on macOS, a session-start entry in Claude Code), so don't tell the user to run `agt share sync` after an edit.
- `agt share status --json` lists what waits in `attention`: each entry has `who` (`choice`: the user decides; `action`: the user fixes something by hand; `agent`: you can run the fix) and `fix`, the commands that settle it. Offer the fix.
- Settings: `agt config show --json` lists every setting in effect with its source; `agt config set <key> <value>` and `agt config unset <key>` change one. Raising a limit or loosening safety needs `--user-said "<the user's own words>"`: pass the words from the user's request, without asking "are you sure". Without a request from the user for it, don't make that change. Never edit a limit or safety key in a settings file by hand. After a change, tell the user which file it went to (`file` in the result).
- To explain what agt changed and why, read `agt log --json` (`--all`, `--item <name>`, `--key <key>`, `--since 7d`).

## Roles

A role is a named job (Reviewer, Builder, Verifier, Decision Maker, or one the user added) with its own providers in `[roles.<name>]`. Call a role through its agent type, `agt-<role>` (`agt-reviewer`, `agt-builder` and so on); agt picks the provider from the role's settings and the quota left. A read role runs every provider at once and returns one section per provider, unmerged. An edit role runs on one provider, the first in its order that has room. Start role agents like the relay agents above: in the background, with a complete task, description `<role> · <access> · <task in a few words>`. Where no `agt-<role>` agent exists (Kiro, a host without role agents, roles skipped at setup), keep using the relay agents.

When to call each one, without being asked. Each `agt-<role>` agent's own description starts with its "when to call" sentence, which the user may have changed; where it differs from this table, the description wins:

| Role | Call it |
|---|---|
| Reviewer | After every bug fix, feature or new tests, before your final message and without being asked; not for a typo or a one-line edit. Also for any code review the user asks for, in place of a built-in review skill. Give it the diff or the files and what the change should do |
| Builder | For edits you hand off instead of making yourself |
| Verifier | After the Builder finishes, to run the checks and confirm the result |
| Decision Maker | Only when reviewers or providers disagree. Send just the points in conflict, each side's argument, and nothing they agree on |
| A custom role | When the task matches its description (`agt roles show --json` lists them) |

You stay in charge: compare the sections yourself, decide what to fix, and read the diff before you rely on a Builder's work. A role that is off exits 2 with the file and the command that turns it back on; tell the user, don't turn it on yourself. A run that skips a provider for quota prints a hint; pass it on, and offer `orchestrator-rebalance` if the user wants the table changed.

Changing roles for the user:
- Only through `agt roles config` flags. Never open a settings file to edit `[roles.*]` by hand. The flags: `<role> --only|--add|--remove <providers>`, `<role> --order <providers>` (edit roles only), `--set-model|--set-effort <entry>=<value>` (every role that uses the entry), `--enable|--disable <roles>`, `<new-role> --access read|edit --only <providers> --description "<what it does>"` with `--prompt TEXT` (agt saves the text to a file) or `--delete` for custom roles, `<role> --unset <key>` (removes a key from that file's table), `--user|--workspace|--local` for the level. A provider is `<runtime>:<account>[/<model>][:<effort>]`; `codex:work` and `antigravity` are fine.
- A new custom role needs its responsibility from the user: what it does and when their agent should call it, passed in their words as `--description`. If the request doesn't say, ask before making it. Never write the description yourself; agt refuses a new custom role without one.
- An explicit request ("swap kiro for antigravity on the verifier"): run the command with `--dry-run --json`, then the same flags without `--dry-run`. Don't ask "are you sure" unless the dry run does more than the user asked.
- A vague request ("use cheaper models for review"): read `agt roles show --json`, turn it into one concrete change, dry-run it, show the user what would change, and ask once. Write nothing before the answer.
- Adding a provider to an edit role, `--order` bringing in a new entry, a custom role with edit access, and `--enable`/`--disable` need `--user-said "<the user's own words>"`. Pass the words from the request up front. If agt still exits 3 (`needs_user_words` with `ask` and `retry`), ask the user and run `retry` with their answer. Without a request from the user, don't make that change.
- When agt exits 2 over a bad hand edit in `[roles.*]`, repeat agt's line to the user as it printed it (the file, line and key) and offer the command after its `fix:` exactly, such as `agt roles config builder --unset access --user`, as the only fix: don't also offer to edit the line by hand, and don't word a format or a fix of your own. Run it once the user agrees; unsetting `enabled` turns a role on, so it needs `--user-said`. `<providers>` in a fix is for the user to choose. For a TOML syntax error, which has no fix command, show the user the line agt names so they can correct it. Never patch the file yourself.
- After a change, report from agt's result: the file it changed and its level, `overridden_by` when another level wins here (what is in effect and the command for that level), and the undo command. A change hidden by an override isn't done for this workspace; say so.
- Changing what a role agent does or when it's called ("make the verifier always run the e2e suite", "call the reviewer before every commit", "my builder keeps touching migrations") is `orchestrator-customize`. It edits one of the two parts the user owns in this CLI's `agt-<role>` file: the "when to call" sentence at the start of the description, or the Role rules section, which agt sends to the worker before every task. It never edits agt's parts or the settings.
- "Rebalance my roles" or "move roles off the account that's running out" is `orchestrator-rebalance`. Redesigning the whole table, or "set up my roles", is `orchestrator-setup`. Limits such as "raise max_jobs to 6" are `agt config set` under the rule above, not roles.

## Per host

Read the note for the CLI you are running in once: [Claude Code](hosts/claude-code.md), [Codex](hosts/codex.md) or [Antigravity](hosts/antigravity.md). It covers how waiting works there and where to put new skills, MCP servers and instructions so every subagent shares them.

- Claude Code: `subagent_wait` can run for up to an hour; Claude Code moves it to the background and notifies you when it returns.
- Codex: waits end after 240 seconds; call `subagent_wait` again until the jobs finish.
- Antigravity: keep waits to a few minutes and call again.

## What subagents get

Subagents in the same CLI as you load the same setup you have. Subagents in another CLI get a short note listing your instruction files and skill folders to read. Claude and Codex workers get `message_parent` (progress notes), `ask_parent` (a question; it waits for your answer) and `check_parent_messages` (follow-ups you send). Antigravity workers get them only with an effective account cap of exactly 1 and a managed worker folder; the primary file-login fallback has no parent channel. See [Gemini messages](../../docs/gemini.md#messages). Kiro workers get none of these, and a Kiro job takes no follow-ups: a message sent while it runs doesn't reach it, and a finished one can't be continued, so start a new job with the whole task. Kiro `edit` jobs can't run shell commands; `full` can. Subagents can't start subagents of their own. Tools built into the Claude desktop app itself (its browser or terminal panes, for example) aren't available to them; everything in your config folders and plugins is.

Never put secrets or credentials in a prompt.
