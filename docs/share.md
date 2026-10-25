# Sharing agents and skills

`agt share` keeps your agents and skills the same in every agent CLI you use. Write an agent in Claude Code and it appears in Codex, Kiro and your other Claude folders too, each in that CLI's own format. Edit it in any of them and the edit reaches the others on its own, within a minute (within seconds for a save in Claude Code).

It covers your user folders today:

| CLI | Folder | Agents | Skills |
|---|---|---|---|
| Claude Code | `~/.claude`, and any other Claude folder such as `~/.claude-work` | `agents/<name>.md` | `skills/<name>/` |
| Codex | `~/.codex` | `agents/<name>.toml` | `~/.agents/skills/<name>/` (Codex also reads `~/.codex/skills`) |
| Antigravity | `~/.gemini` | `config/agents/<name>.md` | `antigravity-cli/skills/<name>/` |
| Kiro | `~/.kiro` | `agents/<name>.json` | `skills/<name>/` |

It can share [your instruction files](#sharing-instructions) too, when you turn that on, and [a repo's own agents and skills](#repos) between that repo's CLI folders. MCP servers, hooks, plugins and commands come later. Sharing the extra accounts of one CLI is a separate thing that needs no setup: see [How the shared setup works](../README.md#how-the-shared-setup-works).

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

## Repos

A repo can keep agents and skills of its own: `.claude/agents` and `.claude/skills` for Claude Code, `.codex/agents` for Codex, `.agents/agents` and `.agents/skills` for Codex and Antigravity, `.kiro/agents` and `.kiro/skills` for Kiro. List a repo, and agt shares them between those folders the way it shares your user folders:

```bash
agt share repo add ~/code/app       # or `agt share repo add .` inside it
agt share repo list
agt share repo remove ~/code/app --keep-copies
```

`agt share enable --projects all-with-items` lists every repo your CLIs know (from Claude's and Codex's project lists) that has agents or skills, and `--projects <path>[,<path>]` lists the ones you name. A repo joins only that way; agt never adds one on its own. A session that starts in a repo with its own agents or skills says once that it isn't shared.

- **What changes in a repo.** Skill links are relative, so a worktree's links stay inside it. Copies agt makes go in the repo's `.git/info/exclude`, so they never show in `git status` or get committed by accident. Your originals and their edits are ordinary files: agt never stages or commits anything, and your review is `git diff`.
- **Worktrees** of a listed repo join by themselves, with the repo's picks. `agt config set share.worktrees command` makes them wait for `agt share repo add .` instead.
- **Branches.** A copy agt made that's gone (after `git clean -X`, say) is made again. When a checkout or pull removes an original git has, its copies go to the backups and come back with it; one that holds an edit not yet written back stays. When a branch has no `.claude/agents` folder at all, the copies stay as they are until it's back.
- **During a merge, rebase or cherry-pick,** agt leaves the repo alone and catches up afterwards.
- **Picks** for a repo's items are saved in its `.agt/settings.toml`, under `[share.targets]`. Inside a listed repo, `agt share pick`, `resolve`, `rm`, `mv` and `undo` act on that repo's item when it has one; `--repo <path>` names the repo from anywhere.

`agt share sync` and `status` cover every listed repo, with one line per repo, and so does the background watcher. Each repo keeps its own state and backups under `~/.agents-router/state/share/projects/`.

## Sharing instructions

Instruction files are off unless you turn them on: `agt share kinds add instructions`, or `--instructions block` with `enable`. Each CLI folder has one user-level file:

| CLI | File |
|---|---|
| Claude Code | `CLAUDE.md` in each Claude folder |
| Codex | `~/.codex/AGENTS.md` |
| Antigravity | `~/.gemini/GEMINI.md` |
| Kiro | `~/.kiro/steering/AGENTS.md` (Kiro's user steering; not yet checked with the CLI) |

Only one marked block of each file is shared, so the rest stays that CLI's own:

```markdown
<!-- agents-router:share:start -->
Answer briefly. Use tabs in Go.
<!-- agents-router:share:end -->
```

A file takes part once it has the markers. `agt share adopt codex kiro` adds an empty block at the end of those folders' files (all folders without a label), and the next sync fills it. `agt share adopt claude --wrap` puts that file's own text inside the block instead, to start from what you have. `agt share status` lists the files without a block.

An edit to the block in any file reaches the others; different edits are a conflict, and `agt share resolve instructions --prefer <label>` picks the version that wins. Claude's `@path` imports stay in their Claude file and never go to the other CLIs, and agt's own [delegation rules](delegation.md) block is never copied. Every write backs the file up first and keeps its mode and hard links.

`--instructions whole` (or `agt config set share.instructions whole`) shares whole files instead, and makes a missing one. `agt share kinds remove instructions` stops sharing them; every file keeps its text.

## What a copy holds

An agent's `name`, `description` and instructions are shared, and its tools where the other CLI has a counterpart. Its `model` never is, because model names differ between CLIs. A copy to another folder of the same CLI, such as `~/.claude` to `~/.claude-work`, is the file byte for byte, so it keeps its `model`.

- **Tools.** Claude tool names are mapped to each CLI's own (`Read` is `fs_read` in Kiro, for example). A tool with no counterpart is left out and reported. A Kiro copy leaves out the tools of MCP servers that Kiro doesn't have.
- **Skills** aren't copied: each CLI gets a link to the skill's one real folder, so an edit anywhere is an edit everywhere.
- **Names.** An item is its file or folder name, compared without regard to case. A name only some CLIs accept goes only to those, and two names that differ only in case are refused. An agent named like one of Codex's built-in agents (`default`, `explorer`, `worker`, `awaiter`) isn't copied to Codex.

These are never shared, and `agt share status` counts them as "not shared": hidden files and folders, agt's own relay agents and skill, KiroCrew agents and the agents that rely on its tools, a Kiro agent whose prompt is a `file://` link, a skill folder without `SKILL.md`, a skill turned off in Codex, and `skills/synced`.

## Everyday use

Edit an agent or skill in any CLI. agt spreads the edit on its own ([below](#staying-in-sync-without-you)); `agt share sync` does it at once:

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

### Staying in sync without you

Turning sharing on also sets up what runs it without you:

| What | When it runs | Where |
|---|---|---|
| The background watcher | every minute, and when a shared agents or skills folder changes | macOS: a LaunchAgent, `~/Library/LaunchAgents/io.github.watcharagorn.agents-router.share.plist` |
| A session-start entry | when a Claude Code session starts, resumes or compacts | each Claude folder's `settings.json` |
| A save entry | when Claude Code writes or edits a file inside a shared folder, so the change spreads within seconds | each Claude folder's `settings.json` |
| A catch-up | when agt's MCP server starts in any CLI | |

Each of these syncs only when something moved since the last sync, never waits for another run, and never runs inside agt's workers. A run that changes nothing isn't logged. They all call `~/.agents-router/bin/agt-run`, a small launcher that records which Python and install agt runs from, so they don't depend on `PATH`. `agt setup`, `agt update` and an MCP server start rewrite it when agt moves.

At a session start, Claude Code gets one line when there's something to say: what arrived in that folder since your last session there, and what waits for you (a conflict, say, with the command that fixes it). Nothing at all otherwise. A session that's already open doesn't load agents or skills that arrive later; that's how every CLI works. The watcher also posts a macOS notification for a new conflict, an item waiting to be deleted, a frozen folder or a write that keeps failing, at most once an hour.

agt adds its entries after yours and never changes your own; it backs `settings.json` up to `~/.agents-router/state/share/backups/hooks/` first. A Codex session entry is off by default, because Codex asks you to trust each hook in `/hooks`: `agt config set share.codex_session true` adds one to `~/.codex/hooks.json`. Other switches: `agt config set share.push_on_save false` drops the save entry, `share.watcher false` removes the watcher, and `share.notify false` stops the notifications. Each takes effect at once. `agt share disable` removes all of it.

`agt share status` shows the watcher's last run. When it stopped, the next session start restarts it, and `agt doctor --fix` reinstalls it along with the launcher and the entries. `agt share badge` prints `share:2!` while two things wait for you, and nothing otherwise, for your status line:

```bash
badge=$(agt share badge 2>/dev/null)   # in your statusline-command.sh
```

There's no watcher on Linux or Windows yet. On Linux, session starts, saves and MCP server starts catch up instead; on Windows, MCP server starts do. `agt share sync` works everywhere.

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
agt share disable                       # stop sharing; every copy stays, the watcher and entries go
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
- **Never in the way.** agt's session and save entries always exit 0, print nothing unless there's something to say, and do nothing once agt is gone. When the launcher's Python is gone, the session line says how to reinstall.

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
| An instruction file without agt's block, while instructions share | you choose | `agt share adopt <label>` |
| The background watcher stopped | an agent can do it | `agt doctor --fix` |

The same list is in `agt share status --json` (`attention`, each entry with `kind`, `item`, `who`, `message` and `fix`) and in `~/.agents-router/state/share/attention.json`, which each writing run updates.

## For agents

Every command takes `--json`. A command that needs a choice it wasn't given never prompts an agent: it exits 3 with `{"error": "needs_choice", "ask", "choices", "retry"}`, and changes nothing. Ask the user, then run `retry` with their answer.

To turn sharing on for a user:

1. Run `agt share enable --dry-run --json`. Show the user the proposal as a table: each of `items` with its `cells` per folder, the `excluded` items with their reasons, and the `summary`.
2. Ask once: as suggested, change some picks, or not now. Ask about each item in `needs_answer` (copies that differ) in the same question.
3. If the user changed a pick or answered a question, run the dry run again with their answers as flags: `--resolve <item>=<label>` or `<item>=skip`, `--skip <item>@<label>`, `--only <item>=<label>[,<label>]`.
4. Run the newest proposal's `command` exactly as given. It already carries those flags, and ends in `--yes --if-plan <id>`. Don't add flags to an older `command`: they change the plan, so it's refused ("the plan changed since your dry run"), as it is when a folder changed meanwhile.

Take answers from the user's own words: "remove them" is `--remove-copies`, "keep them" is `--keep-copies`, and "use the work one" is `--resolve <item>=claude-work` with `enable`, or `--prefer claude-work` with `resolve`.

## Settings

Your answers live in the `[share]` tables of `~/.agt/settings.toml`:

```toml
[share]
enabled = true
kinds = ["agents", "skills"]
tool_widening = "allow-if-shell"
auto_copy = true                      # or false, or a list of kinds
codex_session = true                  # the switches are written only when they differ from the default

[[share.folders]]
cli = "claude"
label = "claude-work"
path = "/Users/you/.claude-work"

[share.targets]
"agents/reviewer" = { not_in = ["kiro"] }
"skills/release-notes" = { only = ["claude", "codex"] }
"skills/old-notes" = "off"            # not shared: every copy stays as it is
```

`agt share settings` lists each setting, where it comes from and the command that changes it. Some are plain values: `agt config set share.auto_copy off`, `share.tool_widening skip`, and the switches `share.watcher`, `share.push_on_save`, `share.notify` and `share.codex_session` (true or false). The others change with the commands above, because changing them makes or removes copies. See [Configuration](configuration.md).
