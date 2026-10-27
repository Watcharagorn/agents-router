# Sharing agents and skills between Claude Code, Codex, Kiro and Antigravity

`agt share` keeps your agents and skills the same in every agent CLI you use. Write an agent in Claude Code and it appears in Codex, Kiro and your other Claude folders too, each in that CLI's own format. Edit it in any of them and the edit reaches the others on its own, within a minute (within seconds for a save in Claude Code).

It covers your user folders today:

| CLI | Folder | Agents | Skills |
|---|---|---|---|
| Claude Code | `~/.claude`, and any other Claude folder such as `~/.claude-work` | `agents/<name>.md` | `skills/<name>/` |
| Codex | `~/.codex` | `agents/<name>.toml` | `~/.agents/skills/<name>/` (Codex also reads `~/.codex/skills`) |
| Antigravity | `~/.gemini` | `config/agents/<name>.md` | `antigravity-cli/skills/<name>/` |
| Kiro | `~/.kiro` | `agents/<name>.json` | `skills/<name>/` |

It can share [your instruction files](#sharing-instructions), [your MCP servers](#sharing-mcp-servers), [your hooks](#sharing-hooks) and [your plugins](#sharing-plugins) too, when you turn them on, and share [a repo's own agents and skills](#repos) between that repo's CLI folders. Commands come later. Sharing the extra accounts of one CLI is a separate thing that needs no setup: see [How the shared setup works](../README.md#how-the-shared-setup-works).

## Turning it on

`agt setup` suggests sharing on its one screen, next to the plugin install. Later, in a terminal, `agt share enable` finds your CLI folders and shows what sharing would do. Nothing is written until you press Enter; `p` changes what goes where, and `q` quits. An item whose copies already differ is asked about first.

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
- **Instructions.** A repo's instructions are its `AGENTS.md`, which Codex, Antigravity and Kiro read already. While [instructions](#sharing-instructions) share, a repo with `AGENTS.md` and no `CLAUDE.md` (at its root or in `.claude/`) gets a `CLAUDE.md` holding `@AGENTS.md`, so Claude Code reads it too. Like other copies, it goes in `.git/info/exclude`. agt takes it away when `AGENTS.md` goes, or instructions stop sharing, as long as it still holds only that line; once you add to it, it's yours.
- **The CLIs and kinds a repo shares** are yours: a repo shares between one folder per CLI that your user folders share, and the kinds you share. `cli add` and `kinds add` reach every listed repo at once, and `cli remove` and `kinds remove` take that CLI or kind out of them too, with the same answer about agt's copies (see [Turning things off](#turning-things-off)).

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

`--instructions whole` (or `agt config set share.instructions whole`) shares whole files instead, and makes a missing one. `agt share kinds remove instructions` stops sharing them; every file keeps its text, and the `CLAUDE.md` files agt made in [listed repos](#repos) to point Claude Code at their `AGENTS.md` go to the backups.

## Sharing MCP servers

MCP servers are off unless you turn them on: `agt share kinds add mcp`, or `--mcp on` with `enable` or `agt setup`. Then a server you add or change in one CLI's user settings reaches the others:

| CLI | Where agt reads them | How agt writes them |
|---|---|---|
| Claude Code | `mcpServers` in `~/.claude.json` (for another Claude folder, its own `.claude.json`) | `claude mcp add-json --scope user`, with that folder's `CLAUDE_CONFIG_DIR`; agt never edits `.claude.json` itself |
| Codex | `[mcp_servers.<name>]` in `~/.codex/config.toml` | that table, every other line kept |
| Antigravity | `~/.gemini/config/mcp_config.json` | a JSON merge |
| Kiro | `~/.kiro/settings/mcp.json` | a JSON merge |

Each server is an item named `mcp/<name>`, and it follows the same rules as agents and skills:

- An edit anywhere spreads, and different edits are a conflict. `agt share resolve mcp/<name> --prefer <label>` picks the version that wins, or `--prefer skip` stops sharing that server.
- Deleting a copy agt made stops the server going to that CLI. Deleting the server where it started leaves the other copies until `agt share rm mcp/<name>` takes them out too.
- `agt share pick mcp/<name> --only claude` keeps a server where you say. The copies agt made in the CLIs you leave out are taken out, with a backup, unless you add `--keep-copies`.

Servers are compared on what every CLI shares: the command, its arguments, `env` and `cwd`, or the URL and headers. A CLI's other settings for a server, such as Kiro's `disabled` and `autoApprove` or Codex's timeouts, stay as they are. `${VAR}` references are copied as written and never expanded. In Codex they become `env_vars`, `bearer_token_env_var` and `env_http_headers`.

Some servers don't go everywhere:

- **A server with a secret written into it** (a token in `env`, a header or an argument, or a password in a URL) stays in the CLIs that have it. Use a `${VAR}` reference instead, and it shares.
- **A server that signs in through a `headersHelper` command** stays in the Claude folders that have it. That setting belongs to Claude Code alone, and agt never runs it.
- **A server set up for one CLI** stays where it is. That's one whose `env` sets the CLI's own folder (`CODEX_HOME`, `CLAUDE_CONFIG_DIR` or `KIRO_HOME`), or whose `command` or `cwd` is a relative path. The servers the Codex app adds are like this.
- **A server turned off everywhere it is** (`enabled = false` in Codex, `"disabled": true` in Kiro or Antigravity) isn't copied. One that's on in another CLI still goes from there, and stays off where you turned it off.
- **Codex** passes variables only by name, so a server whose `env` gives one variable another's value (`"API_TOKEN": "${OTHER}"`) doesn't go there. Neither does an SSE server.
- **Antigravity** doesn't expand `${VAR}` in its settings, so a server that needs one doesn't go there. Neither does an SSE server.
- **Kiro** asks you once before it passes a `${VAR}` value to a server.
- A server that logs in with OAuth needs that login once in each CLI.

agt's own server (`agents-router`) is never shared. Every write backs up what was there first (for Claude, the server's own entry), reads the server back, and puts back what was there if it doesn't match. `agt share undo` doesn't cover MCP servers; their backups are in `~/.agents-router/state/share/backups/<run>/mcp/`. The session hook leaves Claude's writes to the background watcher, because two `claude` starts don't fit in a session start. MCP servers share between your user folders only, not repos. `agt share kinds remove mcp` stops sharing them; every CLI keeps the servers it has.

## Sharing hooks

Hooks are off unless you turn them on: `agt share kinds add hooks`, or `--hooks on` with `enable`. Even then agt copies none until you approve it for a CLI, because a hook runs commands on your machine:

```
agt share pick hooks/pretooluse-guard-shell                # what it runs, and whether it can run in each CLI
agt share pick hooks/pretooluse-guard-shell --in codex     # approve it there
agt share pick hooks/pretooluse-guard-shell --not-in codex # take that back
```

agt reads your user hooks from Claude Code's `settings.json`, Codex's `hooks.json` and Antigravity's `config/hooks.json`. A hook is one matcher group: its event, its matcher and the commands it runs. It's named after its event and its script, such as `pretooluse-guard-shell`. The same hook in two Claude folders counts once. Kiro's hooks aren't read or written yet.

A hook can go to another CLI when:

- that CLI has its event. Claude Code and Codex have SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop and SessionEnd; Antigravity has SessionStart, PreToolUse and PostToolUse;
- every tool in its matcher has a counterpart there. The shell does (`Bash` in Claude Code and Codex, `run_command` in Antigravity), and MCP tools do between Claude Code and Codex. Matchers like `Edit`, `*` or a regular expression don't;
- it's a plain command, with no `if` condition;
- its command has no secret written into it, such as a token in an argument, a header or a variable set on the command line.

Another folder of the same CLI can run any hook as it is.

An approval holds the hook as it was: its commands and the content of every script they call. When you change the hook or one of its scripts, its copies do nothing until you approve it again, and `agt share status` says so.

A copy is one entry at the end of the other CLI's hooks file that calls `agt share hook-run` through agt's launcher. That runs your hook's commands as they are in the CLI you wrote them for. The other CLI's input is translated into that CLI's shape (Antigravity's `run_command` becomes `Bash`), and the answer is translated back:

- A block (exit 2, or a `deny`) blocks the call.
- `allow`, and a command the hook rewrote (`updatedInput`), go to Claude Code and Codex. Codex has no `ask`, so an `ask` there is no answer.
- Antigravity gets `deny` or `allow`. `allow` hands the call to Antigravity's own permission settings; agt never answers `auto_approve` for you. A rewrite can't be passed to Antigravity, so the call runs unchanged under those settings.
- Context the hook adds goes to Claude Code and Codex.
- In agt's own workers, and whenever something goes wrong, the copy gives no answer, so it never blocks by mistake.

agt keeps its entries' places in each hooks file, since Codex trusts hooks by their position: Codex asks you to trust a new copy once in `/hooks`. agt's own hooks and Antigravity's review gate are never shared, and hooks share between your user folders only, not repos. `agt share kinds remove hooks` (or `agt share disable`) takes every copy out; your approvals stay for next time.

## Sharing plugins

Plugins are off unless you turn them on: `agt share kinds add plugins`, or `--plugins on` with `enable` or `agt setup`. Turning them on first shows what it would install. In a terminal it asks before installing anything; without one it exits 3 until you add `--yes`, and `--dry-run` only shows the plan. Once on, a plugin you install in one of your Claude Code or Codex folders is installed in the others, with that CLI's own commands: `claude plugin marketplace add` and `claude plugin install`, or `codex plugin marketplace add` and `codex plugin add`. agt then checks `plugin list` shows it.

Each plugin is an item named `plugins/<name>@<marketplace>`:

- Uninstalling a copy agt installed stops the plugin going to that CLI. Uninstalling the one you installed leaves agt's copies until `agt share rm plugins/<id>` uninstalls them too.
- Turning a plugin on or off stays each CLI's own choice. A plugin that's off in every CLI that has it doesn't go anywhere.
- A plugin installed only for a project (project or local scope) isn't shared, and `agt share status` lists it.
- `agt share pick plugins/<id>` shows what each folder would do with it. `--not-in <label>` keeps it out of that folder, and uninstalls agt's copy there unless you add `--keep-copies`.

Before a plugin goes to a folder, agt checks that the CLI can add the plugin's marketplace and load its parts. Then one of three things happens:

- **It goes.** The next sync installs it. The session hook and a save leave installs to the background watcher, because an install clones a repo. A plugin's server that may log in with OAuth needs that login once in each CLI, and the sync says so.
- **It stays put**, and the sync's report and `agt share status` say why. Either the CLI can't add the marketplace, or the CLI would load nothing of the plugin (a Claude Code mod in Codex, say), or the plugin comes with Codex (`openai-bundled`, for example). Kiro and Antigravity install no plugins.
- **It waits for your answer**, because installing it there would leave parts out or run its hooks without asking, or because it's already installed there for a project. The sync's report and `agt share status` list it with the commands that answer it, and the watcher sends a notification. `agt share pick plugins/<id> --in <label>` installs it as it is now. Your yes holds that list of gaps, so a later version that adds to them asks again. `--not-in <label>` keeps it out.

What each CLI loads from the other's plugins (checked against Claude Code 2.1 and Codex 0.164):

| Part | Claude Code | Codex |
|---|---|---|
| Skills | yes | yes |
| Commands | yes | yes, as skills |
| Agents, interface mods (`modules` in `hooks/hooks.json`), LSP servers, output styles, monitors, themes, channels, install settings (`userConfig`) | yes | no: waits for your answer |
| Hooks | yes, without asking: waits for your answer when the plugin comes from Codex | yes, and Codex asks you before running them |
| MCP servers | expands `${CLAUDE_PLUGIN_ROOT}`, and starts them in your session's folder: a server with a relative `cwd`, or one that needs a `CODEX_*` variable, waits for your answer | doesn't expand `${CLAUDE_PLUGIN_ROOT}`, so a server that uses it waits for your answer, as does one that signs in through `headersHelper` or that [MCP sharing](#sharing-mcp-servers) wouldn't send to Codex (an SSE server, say); starts them in the plugin's folder |
| Apps (`.app.json`) | no: waits for your answer | yes |

Marketplaces: Claude Code adds one from a folder, a Git URL or a GitHub repo that has `.claude-plugin/marketplace.json`. Codex adds one from a folder or a Git repo that has that file or its own `.agents/plugins/marketplace.json`. Codex can't add a Claude marketplace that's only a URL to a `marketplace.json` file. A marketplace that has only Codex's file can't go to Claude Code.

agt reads Claude Code's `plugins/installed_plugins.json`, `known_marketplaces.json` and `enabledPlugins`, and Codex's `[plugins."<id>"]` and `[marketplaces]` tables in `config.toml` with the files in `plugins/cache`. agt's own plugin is left to `agt setup`. When an install fails, `agt share status` says so, and agt doesn't try it again until the plugin's version changes. `agt share pick plugins/<id> --in <label>` or `agt share plugins apply <id>:<label>` tries again now. A Claude Code plugin file agt can't read (one Claude Code is in the middle of writing, say) leaves that folder out of the run, so it never reads as plugins you uninstalled. `agt share plugins apply` takes the same lock as a sync, and what it installs counts as agt's copy. `agt share undo` doesn't cover plugins. Plugins share between your user folders only, not repos. `agt share kinds remove plugins` stops sharing them; every CLI keeps what it has, unless you add `--remove-copies` to uninstall the ones agt installed. Restart a CLI that got a plugin (or run `/reload-plugins` in Claude Code) to load it.

With plugins off, `agt share plugins` lists what another CLI could install, and `agt share plugins apply` installs the ones you say yes to:

```
agt share plugins
agt share plugins apply                              # asks about each one, in a terminal
agt share plugins apply formatter@tools:codex --yes  # installs the ones you name
```

In a terminal, `apply` asks about each plugin, with the parts it would leave out (`x` means "never offer it again", saved as `"plugins/<id>" = "off"` under `[share.targets]`). Without a terminal, it lists them and exits 3 until you name them with `--yes`.

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

Each of these syncs only when something moved since the last sync, never waits for another run, and never runs inside agt's workers. Changes inside `skills/synced` don't count, since agt never shares it and Claude Code rewrites it often. A run that changes nothing isn't logged. They all call `~/.agents-router/bin/agt-run`, a small launcher that records which Python and install agt runs from, so they don't depend on `PATH`. `agt setup`, `agt update` and an MCP server start rewrite it when agt moves.

At a session start, Claude Code gets one line when there's something to say: what arrived in that folder since your last session there, and what waits for you (a conflict, say, with the command that fixes it). Nothing at all otherwise. A session that's already open doesn't load agents or skills that arrive later; that's how every CLI works. The watcher also posts a macOS notification for a new conflict, an item waiting to be deleted, a frozen folder, a write that keeps failing or a plugin waiting for your answer, at most once an hour.

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

Turning off a CLI or a kind asks what happens to the copies agt made there: they stay as your own files and agt stops updating them (`--keep-copies`), or they go to the backups (`--remove-copies`). Without either, a terminal asks, and an agent gets the question back (exit 3). The answer covers your [listed repos](#repos) too: a kind you stop sharing leaves them, and so does a CLI whose last folder stops sharing (with two Claude folders, removing one leaves the repos as they are). While git is in the middle of a merge or rebase in one of them, nothing changes until it's finished. Copies that hold an edit not yet shared, and your originals, stay either way. `disable --remove-copies` also puts back any skill folder of yours that `resolve` replaced with a link. `agt share enable` turns sharing on again with your saved answers.

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
| A plugin waiting for your answer before it goes to a folder | you choose | `agt share pick plugins/<id> --in <label>` or `--not-in <label>` |
| A plugin install that failed | you fix it, or keep it out | `agt share pick plugins/<id> --in <label>` (tries again) or `--not-in <label>` |
| The background watcher stopped | an agent can do it | `agt doctor --fix` |

The same list is in `agt share status --json` (`attention`, each entry with `kind`, `item`, `who`, `message` and `fix`) and in `~/.agents-router/state/share/attention.json`, which each writing run updates.

## For agents

Every command takes `--json`. A command that needs a choice it wasn't given never prompts an agent: it exits 3 with `{"error": "needs_choice", "ask", "choices", "retry"}`, and changes nothing. Ask the user, then run `retry` with their answer.

Setting agt up for a user covers sharing too: `agt setup --dry-run --json` returns the plugin install and the share proposal together, and its `command` (`agt setup --yes --if-plan <id> ...`) does all of it. The steps are the same as below. To turn sharing on later, or again:

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
"mcp/notes" = { only = ["claude"] }   # an MCP server stays in that Claude folder
"plugins/deck@tools" = { not_in = ["codex"] }

[share.plugin_approvals]               # your yes to a plugin that waited for it, per folder
"deck@tools" = { codex = "3f2a9c1e7b04" }
```

`agt share settings` lists each setting, where it comes from and the command that changes it. Some are plain values: `agt config set share.auto_copy off`, `share.tool_widening skip`, and the switches `share.watcher`, `share.push_on_save`, `share.notify` and `share.codex_session` (true or false). The others change with the commands above, because changing them makes or removes copies. See [Configuration](configuration.md).
