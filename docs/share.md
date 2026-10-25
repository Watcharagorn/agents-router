# Sharing agents and skills

`agt share` keeps your agents and skills the same in every agent CLI you use. Write an agent in Claude Code and it appears in Codex, Kiro and your other Claude folders too, each in that CLI's own format. Edit it in any of them and the edit reaches the others.

It covers your user folders today:

| CLI | Folder | Agents | Skills |
|---|---|---|---|
| Claude Code | `~/.claude`, and any other Claude folder such as `~/.claude-work` | `agents/<name>.md` | `skills/<name>/` |
| Codex | `~/.codex` | `agents/<name>.toml` | `~/.agents/skills/<name>/` (Codex also reads `~/.codex/skills`) |
| Antigravity | `~/.gemini` | `config/agents/<name>.md` | `antigravity-cli/skills/<name>/` |
| Kiro | `~/.kiro` | `agents/<name>.json` | `skills/<name>/` |

MCP servers, hooks, plugins, commands, instruction files and a repo's own `.claude/agents` come later. Sharing the extra accounts of one CLI is a separate thing that needs no setup: see [How the shared setup works](../README.md#how-the-shared-setup-works).

## Turning it on

In a terminal, `agt share enable` finds your CLI folders and shows what sharing would do. Nothing is written until you press Enter; `p` changes what goes where, and `q` quits. An item whose copies already differ is asked about first.

Without a terminal, or to see the plan as text, add `--dry-run`:

```text
$ agt share enable --claude-dir ~/.claude --claude-dir ~/.claude-work --codex-dir ~/.codex --kiro-dir ~/.kiro --no-agy --dry-run
agt share: dry run, nothing was written.

Folders:
  [x] claude           ~/.claude              1 agents, 1 skills
  [x] claude-work      ~/.claude-work         1 agents, 0 skills
  [x] codex            ~/.codex               0 agents, 0 skills
  [x] kiro             ~/.kiro                0 agents, 0 skills

Agents           claude      claude-work codex       kiro
  planner        +           ·           -1          +
  reviewer       ·           +           +           +

Skills           claude      claude-work codex       kiro
  release-notes  ·           +           +           +

  + copy  ~ update  · has it  x you unticked it  - can't go there  ! conflict  … waiting
  1. planner in codex: Codex can't limit an agent's tools, and this one has no shell (tool_widening = "allow-if-shell")

New folders: ~/.codex/agents, ~/.claude-work/skills, ~/.agents/skills, ~/.kiro/skills

8 changes in 4 folders.
Plan 3eb6ce30adaa. To apply it:
  agt share enable --yes --if-plan 3eb6ce30adaa --claude-dir '~/.claude' --claude-dir '~/.claude-work' --codex-dir '~/.codex' --kiro-dir '~/.kiro' --no-agy
```

The last line applies exactly that plan. `--if-plan` refuses if anything changed since the dry run, and shows the new plan instead. Your answers are saved in `~/.agt/settings.toml`, so later runs don't ask again.

Each folder has a short label, used by every other command: `claude` for `~/.claude`, `claude-work` for `~/.claude-work`, and `codex`, `kiro` and `antigravity` for the others. When two folders would get the same label, they're `claude@~/path` instead.

The options:

| Option | Meaning |
|---|---|
| `--claude-dir PATH` (repeatable), `--codex-dir`, `--kiro-dir`, `--agy-dir` | The folders to share. Without them, agt suggests the folders it finds. |
| `--no-agy` | Leave Antigravity out. |
| `--kinds agents,skills` | What to share. Both by default. |
| `--only ITEM=LABEL[,LABEL]` | Keep one item in these CLIs only. |
| `--skip ITEM@LABEL` | Untick one cell of the table. Repeatable. |
| `--resolve ITEM=LABEL` or `ITEM=skip` | Which copy wins for an item whose copies differ; `skip` doesn't share it, and every copy stays as it is. |
| `--auto-copy on`, `off` or a list of kinds | Whether new items go to every CLI on their own (on by default), or wait for you to pick. |
| `--tool-widening allow-if-shell` or `skip` | Codex agents can't limit their tools. `allow-if-shell` (the default) copies the agents that have a shell anyway, so in Codex they get every tool; `skip` copies none that list tools. |
| `--accept-suggested` | Take the suggested answer for every open choice. |

`agt share scan` lists every choice setup would offer and writes nothing.

## What a copy holds

An agent's `name`, `description` and instructions are shared, and its tools where the other CLI has a counterpart. Its `model` never is, because model names differ between CLIs. A copy to another folder of the same CLI, such as `~/.claude` to `~/.claude-work`, is the file byte for byte, so it keeps its `model`.

- **Tools.** Claude tool names are mapped to each CLI's own (`Read` is `fs_read` in Kiro, for example). A tool with no counterpart is left out and reported. A Kiro copy leaves out the tools of MCP servers that Kiro doesn't have.
- **Skills** aren't copied: each CLI gets a link to the skill's one real folder, so an edit anywhere is an edit everywhere.
- **Names.** An item is its file or folder name, compared without regard to case. A name only some CLIs accept goes only to those, and two names that differ only in case are refused. An agent named like one of Codex's built-in agents (`default`, `explorer`, `worker`, `awaiter`) isn't copied to Codex.

These are never shared, and `agt share status` counts them as "not shared": hidden files and folders, agt's own relay agents and skill, KiroCrew agents and the agents that rely on its tools, a Kiro agent whose prompt is a `file://` link, a skill folder without `SKILL.md`, a skill turned off in Codex, and `skills/synced`.

## Everyday use

Edit an agent or skill in any CLI, then sync:

```text
$ agt share status
agt share is on for claude (~/.claude), claude-work (~/.claude-work), codex (~/.codex), kiro (~/.kiro).
  2 items in sync, 3 changes for the next sync, 0 conflicts, 0 waiting, 0 waiting to be deleted, 0 not shared.
  `agt share sync` writes the changes.
$ agt share sync
agt share sync: wrote 3 changes in 3 folders.
  ~ agents/reviewer in claude-work: ~/.claude-work/agents/reviewer.md
  ~ agents/reviewer in codex: ~/.codex/agents/reviewer.toml
  ~ agents/reviewer in kiro: ~/.kiro/agents/reviewer.json
```

`status` writes nothing. It exits 1 while there's a conflict, an item waiting to be deleted, or a frozen folder (below). A file saved less than 3 seconds ago waits for the next sync.

- **The same edit in two places** writes nothing more. **Different edits** make a conflict: nothing is written for that item until you pick the winner.
- **A new item** goes to every CLI at the next sync. With `auto_copy` off, it waits until you pick its targets.
- **A copy you delete** that agt made stops that CLI from getting the item. When you delete an original, its other copies wait for `agt share rm`.

### Conflicts

```text
$ agt share status
  Conflict: reviewer differs between claude-work, kiro (the copies were edited differently). Your choice: `agt share resolve reviewer`.
```

`agt share resolve reviewer` in a terminal shows how the copies differ and asks which one wins. `--prefer <label>` picks one directly, `--prefer newest` takes the newest of the copies in conflict, and `--prefer skip` stops sharing the item and leaves every copy as it is. `--all` resolves every conflict, and `--kind agents` limits `--all` to one kind. `agt share status --diff` shows the differences without changing anything.

### Choosing where an item goes

`agt share pick` in a terminal opens a grid of items by CLI: the arrow keys move, Space ticks or unticks a cell, `a` ticks and `n` unticks a whole row, Enter saves, and Esc or `q` quits without saving. By command:

```bash
agt share pick reviewer --not-in kiro        # the copy agt made in Kiro goes to the backups
agt share pick reviewer --in kiro            # back again
agt share pick reviewer --only claude,codex  # these CLIs only
```

`--not-in` and `--only` remove the copies agt made in the CLIs you leave out, with a backup; `--keep-copies` keeps them as your own files instead. Your originals are never removed, and a copy holding an edit the others don't have is always kept. Items are named bare (`reviewer`) when the name is unique, or as `agents/reviewer` and `skills/release-notes`.

### Removing, renaming and undoing

```bash
agt share rm reviewer                   # from every CLI, with backups
agt share mv reviewer code-reviewer     # renamed in every CLI; each copy keeps its own fields
agt share undo reviewer                 # put back what the last change to it replaced
agt share undo reviewer --in kiro       # bring back the copy removed from Kiro
```

A rename you make by hand is spotted too: at the next sync, an item that disappeared and one that appeared with the same contents apart from the name are one item renamed, and the rename goes to every CLI.

## Turning things off

```bash
agt share cli list                      # the folders sharing, and the others agt found
agt share cli add codex                 # share one more; it's filled at once
agt share cli remove kiro --keep-copies
agt share kinds remove skills --remove-copies
agt share disable                       # stop sharing; every copy stays
agt share disable --remove-copies       # also remove the copies agt made
```

Turning off a CLI or a kind asks what happens to the copies agt made there: they stay as your own files and agt stops updating them (`--keep-copies`), or they go to the backups (`--remove-copies`). Without either, a terminal asks, and an agent gets the question back (exit 3). Copies that hold an edit not yet shared, and your originals, stay either way. `disable --remove-copies` also puts back any skill folder of yours that `resolve` replaced with a link. `agt share enable` turns sharing on again with your saved answers.

A turn-off that arrives by editing the settings file has nobody to answer, so agt keeps the copies, stops updating them and lists each one in the [log](configuration.md#the-log).

## Safety

- **Backups.** Before agt replaces or removes anything, it copies it to `~/.agents-router/state/share/backups/<run>/`. A removal moves the file or link there, so nothing is ever deleted outright. Each run's `manifest.json` lists every change and its backup; `agt share undo` reads them.
- **No lost edits.** agt reads each copy again just before writing it, and skips the item for this run if it changed meanwhile. It reads each write back and puts the backup back on a mismatch. A run that was cut off is finished from its journal at the start of the next one.
- **One run at a time** per set of folders.
- **A mass delete freezes.** When more than 3 of the copies agt made in one folder disappear at once, and more than a quarter of them, agt deletes nothing there until you say: `agt share sync --allow-deletes <label>` (you meant it) or `agt share sync --refill <label>` (put them back). Either applies to that run only.
- **Git repos.** When a CLI folder is a git repo (`~/.claude` often is), the copies agt makes there go in one marked block of that repo's `.git/info/exclude`, so they don't show in `git status`. Nothing is committed.
- **Not in workers.** agt's own subagent workers never write share copies.

Every run that writes is one line in agt's log, with each write and backup: see [the log](configuration.md#the-log).

## What waits for you

`agt share status` ends with what needs you, one line each, and the command that fixes it:

| Kind | Who acts | Fix |
|---|---|---|
| A conflict | you choose | `agt share resolve <item>` |
| An original you deleted, with copies waiting | you choose | `agt share rm <item>` |
| A new item waiting for its targets (auto copy off) | you choose | `agt share pick <item> --in <label>` |
| A frozen folder | you choose | `agt share sync --allow-deletes <label>` or `--refill <label>` |
| A rename that couldn't finish | you choose | `agt share mv <old> <new>` |
| A file agt can't read | you fix it | |
| A copy agt couldn't write in the last 3 runs | you fix it | |

The same list is in `agt share status --json` (`attention`, each entry with `kind`, `item`, `who`, `message` and `fix`) and in `~/.agents-router/state/share/attention.json`, which each writing run updates.

## For agents

Every command takes `--json`. A command that needs a choice it wasn't given never prompts an agent: it exits 3 with `{"error": "needs_choice", "ask", "choices", "retry"}`, and changes nothing. Ask the user, then run `retry` with their answer.

To turn sharing on for a user:

1. Run `agt share enable --dry-run --json`. Show the user the proposal as a table: each of `items` with its `cells` per folder, the `excluded` items with their reasons, and the `summary`.
2. Ask once: as suggested, change some picks, or not now. Ask about each item in `needs_answer` (copies that differ) in the same question.
3. Run the proposal's `command` (`agt share enable --yes --if-plan <id>`), plus `--resolve`, `--skip` or `--only` for the user's answers. If the plan changed meanwhile, it refuses and prints the new one.

Take answers from the user's own words: "remove them" is `--remove-copies`, "keep them" is `--keep-copies`, "use the work one" is `--prefer claude-work`.

## Settings

Your answers live in the `[share]` tables of `~/.agt/settings.toml`:

```toml
[share]
enabled = true
kinds = ["agents", "skills"]
tool_widening = "allow-if-shell"
auto_copy = true                      # or false, or a list of kinds

[[share.folders]]
cli = "claude"
label = "claude-work"
path = "/Users/you/.claude-work"

[share.targets]
"agents/reviewer" = { not_in = ["kiro"] }
"skills/release-notes" = { only = ["claude", "codex"] }
"skills/old-notes" = "off"            # not shared: every copy stays as it is
```

`agt share settings` lists each setting, where it comes from and the command that changes it. Two are plain values: `agt config set share.auto_copy off` and `agt config set share.tool_widening skip`. The others change with the commands above, because changing them makes or removes copies. See [Configuration](configuration.md).
