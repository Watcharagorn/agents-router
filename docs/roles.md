# Roles

A role is a named job, such as Reviewer or Builder, with one or more providers. Your main agent calls the role, and agt picks the provider from the role's settings and the quota left on your accounts. Your main session still drives and plans: agt runs no pipeline of its own.

## Base roles

| Role | Access | Your agent uses it |
|---|---|---|
| decision-maker | read | only when reviewers or providers disagree, with just the points in conflict |
| reviewer | read | after every bug fix, feature or new tests, without being asked (not a typo or a one-line edit), and for any code review you ask for, in place of a built-in review skill |
| builder | edit | for edits |
| verifier | read | after the Builder, to check the change works |

You can add custom roles, each with its own access, prompt and providers, and a description in your words of what it does and when your agent should call it. A new custom role needs that description.

| Access | The role's jobs can | Providers run |
|---|---|---|
| read | read files and search; no edits | all at once |
| edit | read and edit files in the workspace; the shell depends on the CLI (Kiro has none) | one, in priority order |
| full | everything edit can, plus every tool and the shell, with no sandbox or permission prompts | one, in priority order |

Only a custom role can have full access. Every provider of a full role gets it: Claude Code runs with `bypassPermissions`, Codex with `--dangerously-bypass-approvals-and-sandbox`, Kiro with `--trust-all-tools`, and Antigravity without its review gate.

## How a role runs

- **Read role:** every usable provider runs in parallel as an ordinary job, all under one group id (`g-…`). The result has one section per provider, plus FAILED and SKIPPED sections, and ends DONE if at least one succeeded. `agt follow`, `agt cancel`, `agt jobs` and `agt message` accept the group id.
- **Edit or full role:** one job with the role's access, on the first provider in the role's order that agt's limits admit, preferring one below the usage ceiling. A job that fails partway is not retried on the next provider.
- A provider whose account is unknown, logged out, cooling down or at its limit is skipped with the reason. If none can run, the call exits 1.
- `agt run --role <name>` can't be combined with `--runtime`, `--account`, `--model` or `--access`: the role's providers set the first three, and the role's settings set its access.

## Settings

Roles live in `[roles.<name>]` in any settings file (`~/.agt/settings.toml`, `<workspace>/.agt/settings.toml`, `settings.local.toml`). A workspace role merges into your user role key by key, and its `providers` list replaces the one below it.

```toml
[roles.reviewer]
providers = ["codex:work/gpt-6.1-sol:high", "antigravity"]

[roles.builder]
providers = ["claude:claude-work/opus", "codex:work"]   # order = priority

[roles.verifier]
enabled = false                                         # turned off: use your own agent

[roles.security-reviewer]
access      = "read"
description = "Checks a change for security problems"
prompt_file = "~/.agt/roles/security-reviewer.md"
providers   = ["codex:work/gpt-6.1-sol:high", "claude:claude-work/opus"]
```

A provider is `<runtime>[:<account>][/<model>][:<effort>]`. `codex` alone uses any Codex account with room. `primary` names a CLI's main account (`antigravity:primary`). The last `:` part is read as effort only when it is a known effort level, because model names can contain `:`.

Base roles have fixed access. The names `claude`, `codex`, `antigravity` and `kiro` belong to the built-in relay agents and can't be roles. A bad shape is refused when settings load, naming the file and line.

## Changing roles

Change roles with `agt roles config`, never by hand-editing, so every change is checked, backed up and logged:

| Command | Does |
|---|---|
| `agt roles` / `agt roles show [--json]` | the table, with roles that are off and providers that can't run |
| `agt roles config` (at a terminal) | the grid: roles as rows, accounts as columns. Space ticks, `a`/`n` whole row, `m`/`e` cell model and effort, `+`/`-` account order, `s` splits a cell, `o` turns the role under the cursor on or off. Enter saves |
| `agt roles config <role> --only/--add/--remove <providers>` | a role's providers (a role keeps at least one) |
| `agt roles config <role> --order <providers>` | an edit or full role's priority |
| `agt roles config --set-model/--set-effort <provider>=<value>` | one provider in every role that uses it |
| `agt roles config --enable/--disable <roles>` | turn roles on or off, base roles included |
| `agt roles config <new-role> --access read --only <providers> --description "<what it does>"` | a custom role (`--access read`, `edit` or `full`; `--prompt`, `--delete`); the description is required |
| `agt roles propose [--rebalance] [--json]` · `agt roles config --from FILE` | a proposed table, then writing it |

Both table views keep one column per account, even when its models or efforts differ between roles. Each row shows
the role's access and on/off switch. Cells show `[model/][effort:]✓` for read access or the provider's try-order number
for edit/full access. The footnote gives the shared effort; `def` means the runtime's default, and `·` means unused.
Multiple entries for one account share a cell, separated by commas. `m`/`e` change its last entry; `s` appends a copy
to the role so you can choose another model or effort. `+`/`-` move the account's entries as a block. Ticking an empty
cell uses that account's most common model and effort in the other roles. Arrow keys (or `h`/`j`/`k`/`l`) scroll the
accounts and roles while keeping the selected cell visible. Esc or `q` leaves without saving.

Add `--dry-run` to see a change without writing it. Name `--user`, `--workspace` or `--local` to pick the file; with none, a change goes where the role is set now, else your user file, never the committed workspace file. A refused command writes nothing. After a write agt prints the file, the level, any override from another level, the backup and one undo command.

From an agent, a change that widens access needs the user's words: `--user-said "<their words>"`. That is a provider added to an edit or full role, a new edit or full role, a role moved from read to edit or full, or from edit to full, and every `--enable`/`--disable`. Without them the command exits 3 and says what to ask. Narrowing access (full to edit, edit to read) needs no words.

When agt skips a provider for quota, it suggests a rebalance: ask your agent to "rebalance my roles", and it proposes a new mapping with reasons and changes nothing before you approve.

## Role agents

agt writes one relay agent per enabled role, named `agt-<role>`, only for the CLI you set roles up in: Claude Code (every profile with the plugin), Codex and Antigravity. Kiro has no relay agents. The files come from your user settings alone: a project's settings never add, change or remove one, so `agt roles config --disable builder --local` turns the builder off in that project and leaves `agt-builder` for the others. A role set only in a project's file gets no agent; `agt roles show` says so. Each file says `Managed by agt roles`; agt never overwrites or deletes a same-named file without that line, so you can turn a role off and write your own `agt-<role>`. `agt share` doesn't copy these files to other CLIs. `agt setup` proposes the role table in its one question, with what each role does and when your agent calls it, and asks whether you want a role of your own (added after setup, with its description). `agt update` brings the files in line.

### Roles first in Claude Code

With the plugin enabled, installed agt role agents come first for subagent work. This applies only to Claude Code. Codex, Antigravity and Kiro hosts are unchanged.

| Hook | Runs on | Does |
|---|---|---|
| `agt hook roles-context` | `SessionStart`: startup, resume, clear and compact | Adds a note listing installed role agents and each one's "when to call" sentence, the fallback prefix and the planning rule |
| `agt hook roles-first` | `PreToolUse`: `Agent` or `Task` | Turns back new `Explore`, `Plan`, `general-purpose` and missing-type calls, with a reason listing installed role candidates |

The list includes only enabled, managed role agents in this profile. A user-owned file without the management line is left out. A role shadowed by a workspace agent with the same frontmatter `name:` under `.claude/agents/` is left out too.

To use a built-in on purpose, start the call's `description` with `agt-fallback: <reason>`. Use it when:

| Reason | Example description |
|---|---|
| No installed role fits | `agt-fallback: no read-only role installed` |
| A listed role is unavailable | `agt-fallback: listed role unavailable` |
| The user or a skill asked for that built-in by name | `agt-fallback: user asked for Explore` |

Specialists such as `claude-code-guide`, `web-fetch` and `statusline-setup`, `fork`, provider relays, other plugins' agents and your own agents are never gated. Agent teams, teammate spawns with a non-empty `name` or `team_name`, and messages to running agents are unchanged. Your own agents are called as usual when no role covers their job. When responsibilities overlap, the role comes first. That overlap rule is advisory; the gate never blocks your own agent types.

Planning stays in the main session, which gathers data and drafts plans itself. It never starts a subagent in plan mode (`mode: "plan"`) or a `Plan` agent to plan for it. This rule is advisory: the gate prints nothing in plan mode, so nothing is blocked. The session note still carries the rule.

Both hooks print nothing inside agt workers (`AGT_JOB_ID` or `AGT_DEPTH > 0`), for subagent callers (`agent_id`), outside Claude Code, or when no eligible role agents are installed. They fail open: malformed input, settings errors or unreadable agent files produce no output and let the call through.

`[subagents] prefer_roles` is a boolean, default `true`. Set it to `false` to silence both hooks:

```bash
agt config set subagents.prefer_roles false
```

The setting applies at user (`~/.agt/settings.toml`), workspace (`.agt/settings.toml`) and local (`.agt/settings.local.toml`) levels; local wins over workspace, which wins over user. The command above writes to the workspace level when run in a workspace. Use `--user`, `--workspace` or `--local` to pick one. See [configuration](configuration.md#subagent-choice).

A hand-written "prefer roles" block in your `CLAUDE.md` is no longer needed for that part; the plugin covers it.

### Your two parts of a role agent

Two parts of each `agt-<role>` file are yours. agt keeps them through every rewrite (`agt setup`, `agt update`, `agt roles config`) and owns the rest.

| Part | Where | What it does |
|---|---|---|
| When to call | The start of the frontmatter `description`, before `Runs the <role> role through Agents Router (` | Your main agent reads it to decide when to call the role |
| Role rules | The `## Role rules` section at the end, between `<!-- agt-role-rules: ... -->` and `<!-- end agt-role-rules -->` | agt puts it before the task on every call, under "Role rules for the <role> (from <file>):" |

```text
---
# Managed by agt roles v2: ...
name: agt-builder
description: "Use for every edit, even tiny. Runs the builder role through Agents Router (`agt run --role builder`): ..."
...
---
<relay steps>

## Role rules

These rules are for the worker, not for you: ...

<!-- agt-role-rules: agt keeps what's between these lines and sends it to the worker before every task -->
Run the tests before you finish.
Never edit migrations.
<!-- end agt-role-rules -->
```

- **Clearing the sentence** (nothing before `Runs the ...`) brings back agt's default. An explicit `agt roles config <custom-role> --description "..."` writes the new sentence into every file, edited or not.
- **A sentence you never changed follows agt's default.** When a release changes a role's default, `agt update` writes the new one. agt keeps a list of its earlier defaults to tell them from yours, so a sentence you edited stays, and `agt roles show` never lists an earlier default as your own.
- **Text after agt's part** of the description, which ends with `then the complete, self-contained task.`, moves to the end of your sentence on the next sync, and the sync says so.
- **Each copy is its own.** Every Claude profile, Codex and Antigravity has its own file, and only calls from that CLI or profile use its rules. Codex keeps the body in `developer_instructions = '''...'''`, so its rules can't contain `'''`.
- **When rules apply.** A role agent passes `--caller claude|codex|antigravity`, and agt reads that CLI's own file: `<profile>/agents/agt-<role>.md`, `<CODEX_HOME>/agents/agt-<role>.toml`, or the relay plugin's `agents/agt-<role>.md`. A run from a terminal, from Kiro or from inside an agt worker gets no rules, and the job's first lines say why. Edits apply to the next new call. A follow-up that has to start a fresh session gets the rules its job started with.
- **Antigravity's limit.** Antigravity takes at most 24,000 characters of prompt, counting agt's own notes. When a prompt with its rules is longer, agt skips Antigravity with the reason: an edit or full role uses the next provider, and a read role runs its other providers. Rules are never cut.
- **Trust.** Role rules steer workers that may edit code. agt reads them only from its own agents folders and refuses a link there. A file without the management line is yours: agt never rewrites it, but `agt run` still sends its Role rules.
- **Reload.** Claude Code's docs say it watches `~/.claude/agents/` and the next delegation uses an edited file with no restart ([sub-agents](https://code.claude.com/docs/en/sub-agents)). They don't say whether another profile's folder (`CLAUDE_CONFIG_DIR`) is watched the same way; if an edit there doesn't show, restart the session. A session also needs a restart to see the first file in an agents folder that didn't exist when it started. Role rules don't depend on this: agt reads them from the file on every call.

How agt reads a file:

| State | Sync | `agt run --role` |
|---|---|---|
| valid | Keeps your two parts and rewrites the rest; no write when nothing changed | Your rules before the task |
| legacy (written by agt 0.11) | Adds an empty Role rules section and keeps the sentence | No rules |
| broken: a marker missing, doubled or out of order, or a description agt can't read | Leaves the file as it is and names the line to fix | Exits 2 before any provider starts when agt can't read the file or its markers; a description problem never blocks a run |
| absent | Writes a new file | No rules, noted in the job's first lines |
| yours: no management line | Never touched | Your rules before the task (a link or broken markers exit 2) |

**Turning a role off keeps your parts.** When agt removes an edited file (the role is off, deleted, or no longer in your user settings), it first moves your sentence and rules to `role-agents/archive/<cli>/<folder>-<hash>/agt-<role>.toml` in your agt settings folder (`~/.agt` by default). That's outside every agents folder, so no CLI loads it. Turning the role back on puts them back. `agt roles show` names the archive while the role is off and lists every file with its state (`agent_files`, `when_to_call`, `rules_chars`, `rules_state` and `archived` in `--json`).
