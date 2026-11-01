# Roles

A role is a named job, such as Reviewer or Builder, with one or more providers. Your main agent calls the role, and agt picks the provider from the role's settings and the quota left on your accounts. Your main session still drives and plans: agt runs no pipeline of its own.

## Base roles

| Role | Access | Your agent uses it |
|---|---|---|
| decision-maker | read | only when reviewers or providers disagree, with just the points in conflict |
| reviewer | read | before calling non-trivial work done |
| builder | edit | for edits |
| verifier | read | after the Builder, to check the change works |

You can add custom roles, each with its own access, prompt and providers.

## How a role runs

- **Read role:** every usable provider runs in parallel as an ordinary job, all under one group id (`g-…`). The result has one section per provider, plus FAILED and SKIPPED sections, and ends DONE if at least one succeeded. `agt follow`, `agt cancel`, `agt jobs` and `agt message` accept the group id.
- **Edit role:** one job, on the first provider in the role's order that agt's limits admit, preferring one below the usage ceiling. A job that fails partway is not retried on the next provider.
- A provider whose account is unknown, logged out, cooling down or at its limit is skipped with the reason. If none can run, the call exits 1.
- `agt run --role <name>` can't be combined with `--runtime`, `--account` or `--model`: the role's providers set those.

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
| `agt roles config` (at a terminal) | the grid: providers as rows, roles as columns. Space ticks, `a`/`n` whole column, `m`/`e` model and effort, `+`/`-` order, `d` duplicate row, `o` turns the role under the cursor on or off. Enter saves |
| `agt roles config <role> --only/--add/--remove <providers>` | a role's providers (a role keeps at least one) |
| `agt roles config <role> --order <providers>` | an edit role's priority |
| `agt roles config --set-model/--set-effort <provider>=<value>` | one provider in every role that uses it |
| `agt roles config --enable/--disable <roles>` | turn roles on or off, base roles included |
| `agt roles config <new-role> --access read --only <providers>` | a custom role (`--prompt`, `--description`, `--delete`) |
| `agt roles propose [--rebalance] [--json]` · `agt roles config --from FILE` | a proposed table, then writing it |

Add `--dry-run` to see a change without writing it. Name `--user`, `--workspace` or `--local` to pick the file; with none, a change goes where the role is set now, else your user file, never the committed workspace file. A refused command writes nothing. After a write agt prints the file, the level, any override from another level, the backup and one undo command.

From an agent, a change that widens edit access (a provider added to an edit role, a new edit role) and `--enable`/`--disable` need the user's words: `--user-said "<their words>"`. Without them the command exits 3 and says what to ask.

When agt skips a provider for quota, it suggests a rebalance: ask your agent to "rebalance my roles", and it proposes a new mapping with reasons and changes nothing before you approve.

## Role agents

agt writes one relay agent per enabled role, named `agt-<role>`, only for the CLI you set roles up in: Claude Code (every profile with the plugin), Codex and Antigravity. Kiro has no relay agents. Each file says `Managed by agt roles`; agt never overwrites or deletes a same-named file without that line, so you can turn a role off and write your own `agt-<role>`. `agt share` doesn't copy these files to other CLIs. `agt setup` proposes the role table in its one question, and `agt update` brings the files in line.
