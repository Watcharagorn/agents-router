---
name: agents-router-setup
description: Install, set up, update, repair or uninstall Agents Router (agt) for the user, and reach their other Claude Code profiles the right way. Use it when the user asks to install or set up agt or Agents Router, when `agt` isn't found, when the agents-router plugin, its tools or the agt-* agents are missing in a session or a profile, when the user names another Claude profile or config folder to run work on (~/.claude, ~/.claude-work), when a job is refused with "this session runs in" or "No other Claude profile", or when `agt doctor` reports a problem.
---

# Agents Router setup

When `agt` runs here, run `agt guide setup` and follow what it prints: it's the guide for the installed version. If `agt` is missing, install it first (it needs git, and uv or pipx). If `agt` is there but has no `agt guide` command (older than 0.10), run `agt update` first:

```bash
uv tool install --managed-python git+https://github.com/Watcharagorn/agents-router
agt guide setup
```

To run work on another Claude profile the user names, start `agents-router:agt-claude` with `account=<profile>` (`~/.claude-work` is `claude-work`). Never use `env -u CLAUDE_CONFIG_DIR` or `agt run` in your shell for it.

If you can't run commands, follow the copy below. It matches the version this skill came with.

<!-- The guide below is src/agents_router/guides/setup.md, copied by scripts/sync_guide.py. Edit that file. -->

# Set up Agents Router (steps for an agent)

You are installing, updating, repairing or removing Agents Router (`agt`) for the person you work with. Do the routine steps yourself. Ask them once, in one message, about the choices. At the end, tell them only what's left for them: logins and reloads. This guide is for agt 0.12.0 or newer; `agt guide setup` prints the copy that matches the installed version.

## 0. Before you start

- These steps need the network and write to the person's home folder (`~/.local`, `~/.claude*`, `~/.codex`, `~/.agents-router`). In a sandbox (Codex workspace-write, Claude Code's sandbox), ask once for the permission that covers the whole setup.
- If policy forbids the install, stop and give the person one command to run: `uv tool install --managed-python git+https://github.com/Watcharagorn/agents-router && agt setup`.
- Setup needs `git`, and `uv` or `pipx`. If they're missing, point the person to https://docs.astral.sh/uv/getting-started/installation/ (`brew install uv` on a Mac) and stop; don't install them yourself.

## 1. Install or upgrade

```bash
agt --version   # agents-router 0.12.0 or newer: go to step 2
uv tool install --managed-python git+https://github.com/Watcharagorn/agents-router
# without uv: pipx install git+https://github.com/Watcharagorn/agents-router
```

- **Older than 0.12.0:** run `agt update`, then `agt guide setup` again, because the steps may have changed.
- **`agt` not on PATH after the install:** call it by its full path for the rest of setup (`~/.local/bin/agt`, or `%USERPROFILE%\.local\bin\agt.exe` on Windows). Then run `uv tool update-shell` (or `pipx ensurepath`) so new terminals find it. The plugin's hooks call `agt` by name, so the person's next sessions need it on PATH.
- **Windows:** `agt update` prints the package command instead of running it; run that command yourself.
- **Not on PyPI:** never `pip install agt`; that's a different project.

## 2. Propose, ask once, apply

```bash
agt setup --dry-run --json
```

| Key | Holds |
|---|---|
| `plugin` | The plugin commands per CLI and profile (`planned`, `already`, `failed`) |
| `claude_profiles` | Each Claude Code profile: `name`, `folder`, `this_session`, `gets_plugin` |
| `share`, `repos`, `switches` | Sharing agents and skills between the CLIs, the repos with their own, and the on/off lines |
| `roles` | The proposed role table (`table`: each role's `access`, `providers`, `use_when` and `why`), the `agt-<role>` agents it writes for this CLI (`planned`), `needs_order`, the four `answers`, and on a first setup `custom`: the question about a role of their own and the command that adds it |
| `needs_answer` | Items whose copies differ: the person picks one |
| `still_needed` | What's left for the person after setup |
| `command`, `next` | The command that applies this exact plan, and what to do before running it |

1. **Ask once, in one message.** Show:
   - the Claude profiles that get the plugin, with their folders, marking this session's;
   - the share table (each item with its folders) and the excluded items;
   - the switches, on or off;
   - the role table. Open it with one line on what a role is: a named job your agent hands to its `agt-<role>` agent, and agt picks the provider from the role's settings. Then one bullet per role: **<name>** (<access>; <providers>): <use_when>. Join a read role's providers with " + "; give an edit role's in order, joined with ", then ". Quote `use_when` word for word: it says what the role does and when your agent calls it. Say that the `agt-<role>` agents are for this CLI only, and that they can later change when a role is called or give it standing rules by asking (the orchestrator-customize skill);
   - each `needs_answer` item.

   Offer these answers: **Approve** (as proposed), **Change a role** (or another item), **Skip roles**, or **Cancel**. If `roles.state` is `set`, the roles stay as they are and there is nothing to ask about them. When `roles.custom` is there (a first setup), end the message with its `ask`: "Want a role of your own too? Tell me its name, what it does, read or edit, and its providers."
2. **Put the answers in flags** and run the dry run again.
3. **Run the new proposal's `command` exactly as given.** It ends in `--yes --if-plan <id>`, which refuses when anything changed since the dry run.

| The person wants | Flag |
|---|---|
| The plugin in this profile only, or in named ones | `--claude-profiles this` or `--claude-profiles claude,claude-ntb` |
| One copy of an item whose copies differ | `--resolve <item>=<label>` |
| An item kept out of one CLI | `--skip <item>@<label>` |
| An item only in some CLIs | `--only <item>=<label>[,<label>]` |
| Only some repos, or none | `--projects <paths>` or `--projects none` |
| MCP servers or plugins shared | `--mcp on`, `--plugins on` |
| New items wait for a pick | `--auto-copy off` |
| Worktrees join only by command | `--worktrees command` |
| No Codex agents that get every tool | `--tool-widening skip` |
| Delegation rules, Codex's session line | `--rules on`, `--codex-session-line on` |
| The plugin only | `--share off` |
| A role with other providers, or left out | `--role reviewer=codex,antigravity`, `--role verifier=off` |
| No roles at all (Skip roles) | `--roles off`: no role settings and no role agents. Tell them they can set roles up later by asking you (the orchestrator-setup skill) |
| The order of an edit role's providers | `--role-order builder=<first>,<second>` |

- Profiles already on this machine (`~/.claude`, `~/.claude-*`) need no `--account`. `--account NAME=FOLDER` is only for a logged-in folder somewhere else.
- No question is needed for the install, the PATH fix or the dry run. Everything `command` does needs the person's answer first.
- A command that exits 3 with `needs_choice` changed nothing. Ask its `ask` and run its `retry` with the answer.
- **Builder with several providers.** When the person gives an edit role (the Builder) more than one provider, `roles.needs_order` asks which goes first: it runs on one provider at a time, in that order. Ask it, then add its `--role-order` flag. Read-only roles run all their providers at once, so they have no order.
- **A role of your own.** Setup writes only the base roles. Add each custom role after `command` succeeds, with `roles.custom.command`: `agt roles config <name> --access read|edit --only <providers> --description "<what it does>"`. A role made before that changes the plan: `--if-plan` refuses, and setup stops proposing the base roles.
  - The description is the person's: what the role does and when their agent should call it, in their words. If they didn't say, ask. Never write it yourself; agt refuses a new custom role without one.
  - An edit role also needs `--user-said "<their words>"`.
  - When their request already approved the proposal, run `command` first, then offer a role of your own in your final message.
- Roles are changed later with `agt roles config`, never by editing `[roles.*]` by hand.
- **Role agents go to the CLI you run in.** agt can't tell Antigravity from a plain terminal, so an agent running in Antigravity adds `--host antigravity` to the dry run and to `agt update`. With `--host`, the role agents go to each CLI it names.

## 3. Claude profiles in practice

- **Names.** agt finds every logged-in `~/.claude` and `~/.claude-*` folder and names it after the folder: `~/.claude` is `claude`, and `~/.claude-ntb` is `claude-ntb` (or just `ntb`). `agt accounts` lists them and marks the one this session runs in.
- **The session's own profile never takes its own delegated jobs.** It is the orchestrator; the other profiles are the workers.
- **To run on a profile the person names,** start the relay with `account=<profile>`. In Claude Code that's the `agents-router:agt-claude` agent with the line `account=claude-ntb`; elsewhere, `subagent_start` with `account`.
- **Never reach a profile from your shell.** That means no `env -u CLAUDE_CONFIG_DIR`, no `CLAUDE_CONFIG_DIR=… claude` and no `agt run`. The person can't see such a job, and agt already runs each profile with its own environment.
- **Order and exclusions.** Unpinned jobs go to agt's own accounts first, then to the other profiles. `agt config set claude.order claude-ntb,claude` sets the order; `agt accounts remove <profile>` puts a profile in `claude.ignore`.
- **One login in two folders.** Such folders share their limits, and unpinned jobs skip any folder with this session's login.
- **Two sessions at once.** Two sessions in two profiles can each delegate to the other; each sees only its own results.

## 4. Say what delegation is available now

Read `agt accounts --json` and tell the person in one or two sentences:

- **Only one Claude login** (every other profile has `same_login_as` set, or there are none): Claude-to-Claude delegation is refused here. Point them to the built-in subagents for Claude work, to Codex or Gemini through agt for another vendor, or to `agt accounts add <name>` for a second Claude login.
- **Several logins:** name the profile that takes unpinned jobs first (`takes_unpinned_jobs`), and say how to name another.

## 5. Optional extras: offer them in one line

Example: "Optional: delegation rules (which models your agent uses per task size) and extra Claude logins made by agt. Want either?"

Set one up only when the person says yes; don't interview them.

| Extra | How |
|---|---|
| Rules | Ask for their home and try-first models, run `agt rules --show --target claude --home-high "<model>" ...`, then the same with `--install` |
| Another login | `agt accounts add <name>`; the person completes the browser login |

## 6. Hand-off: what's left for the person

Give them a numbered list built from `still_needed`:

- **Browser logins:**
  - `agt accounts login <name>` for an account agt made;
  - `claude`, then `/login`, inside the profile;
  - `codex login`;
  - the first `agy` sign-in.
- **This session:** type `/reload-plugins` in Claude Code; in Codex or Antigravity, start a new session. Say that the same reload loads the `agt-<role>` agents.
- **Every other Claude profile that got the plugin:** start a new session there.
- **macOS:** if the Claude desktop app reports `agt: command not found` while a terminal finds it, the app doesn't see `~/.local/bin`. Run `uv tool update-shell`, then quit and reopen the app.

## 7. Check

Run `agt doctor`.
- Exit 0 means all good.
- It checks logins without model calls and flags a profile whose plugin is older than `agt` (fix: `agt update`).
- Never run `agt doctor --live` or a real job without the person's OK; both use quota.

## 8. Repair

| Symptom | Check | Fix |
|---|---|---|
| `agt: command not found` | `ls ~/.local/bin/agt` | Full path for now; `uv tool update-shell` (or `pipx ensurepath`), then a new terminal |
| No `agt-*` agents or agents-router tools in a session | `agt doctor`; `agt setup --dry-run --json` lists that profile | `agt setup --claude-profiles <profile>`, then `/reload-plugins` or a new session there |
| `agt doctor`: a profile has an older plugin | Its line names the profile | `agt update`, then reload that profile |
| No `agt-<role>` agents (`agt-reviewer` and so on) | `agt roles` shows the roles; a role that is off has no agent | Run `agt update` from inside that CLI (in Antigravity, `agt update --host antigravity`): it writes them for that CLI, and for a Claude profile added later |
| A role agent isn't agt's | `agt update` says it kept the file (no `Managed by agt roles` line) | It's the person's own; agt never overwrites or deletes it. Rename it if they want agt's back |
| Refused: "this session runs in …" | `agt accounts` | Name another profile, or use a built-in subagent |
| Refused: "No other Claude profile can take this job" | `agt accounts` | Use a built-in subagent, Codex or Gemini; or `agt accounts add <name>` for a second login |
| Outcome `auth`, or "not logged in" | `agt accounts` | The person logs in: `agt accounts login <name>`, or `/login` in that profile |
| Plugin install refused in a work profile | The failed line from `agt setup` | Managed settings may block outside plugins; leave that profile out (`--claude-profiles this`) or ask its admin |
| Plugin declared in `settings.json` but not loaded | `enabledPlugins` in that profile's `settings.json` | Restart Claude Code (a restart, not `/reload-plugins`) |
| "the plan changed since your dry run" | Nothing was written | Run `agt setup --dry-run --json` again; ask again if the change matters |

## 9. Clients without a plugin (Cline, Cursor and others)

1. Find the full path with `command -v agt` (`where agt` on Windows). Add a stdio server with that path:

   ```json
   { "mcpServers": { "agents-router": { "command": "/full/path/to/agt", "args": ["mcp"] } } }
   ```
2. Copy the whole `skills/agents-router/` folder (with its `hosts/` notes) from https://github.com/Watcharagorn/agents-router into the client's skills. If the client takes only rules text, paste `SKILL.md` and the matching host note.
3. Reload the client and check that `subagent_accounts` answers.

## 10. Uninstall

Ask first, then work in this order. Only setting it up again undoes it.

1. `agt share disable` stops sharing and removes the watcher and agt's hook entries; its small launcher script goes with `~/.agents-router` in step 8. Add `--remove-copies` only if the person wants agt's copies gone.
2. `agt rules --remove --target claude` (and `--target codex`), if the rules were installed.
3. **Each Claude profile:** run `claude plugin uninstall agents-router@agents-router`, then `claude plugin marketplace remove agents-router`, once per profile, in that profile's environment.
   - For `~/.claude`, prefix both with `env -u CLAUDE_CONFIG_DIR`, because your session may have the variable set.
   - For any other profile, prefix them with `CLAUDE_CONFIG_DIR=<folder>`.

   Plugin commands like these are the only place you set that variable yourself; for jobs, use `account=`.
4. **Codex:**
   - run `codex plugin remove agents-router@agents-router`, then `codex plugin marketplace remove agents-router`;
   - delete `~/.codex/agents/agt-claude.toml`, `agt-codex.toml` and `agt-antigravity.toml`;
   - keep any `*.agents-router-backup-*` files: they are the person's own earlier versions.
5. **Antigravity:** remove the agents-router plugin with `agy plugin` (`agy plugin list` shows it), and delete `~/.gemini/config/plugins/agents-router-relays` (agt's role agents go with it).
   **Role agents elsewhere:** delete each `agt-<role>.md` in a Claude profile's `agents/` folder and each `agt-<role>.toml` in `~/.codex/agents/` whose first lines say `Managed by agt roles` (`grep -l "Managed by agt roles" ~/.claude*/agents/agt-*.md ~/.codex/agents/agt-*.toml` lists them). Keep any without that line: they are the person's own.
6. **MCP servers added by hand:** `claude mcp remove --scope user agents-router` and `codex mcp remove agents-router`.
7. **The package:** `uv tool uninstall agents-router` (or `pipx uninstall agents-router`).
8. **agt's own folders,** after asking:
   - `~/.agents-router` holds jobs, state and the accounts agt made under `homes/`, whose logins go with it;
   - `~/.agt` holds settings.

The person's own `~/.claude*`, `~/.codex` and `~/.gemini` folders and their logins stay.

## 11. Never

| Never | Instead |
|---|---|
| Read, copy or print credential files, or values from `.claude.json` | agt reads only the fields it needs and keeps hashes |
| Edit files in `~/.agents-router/homes/` | agt rebuilds them |
| Edit a limit or safety key in a settings file | `agt config set`; raising a limit or loosening safety takes `--user-said "<the person's words>"` |
| Run a login for the person | Browser logins are theirs; list them in the hand-off |
| Run `agt doctor --live` or a real job without an OK | Ask first: they use quota |
| Run a job on another profile with `env -u CLAUDE_CONFIG_DIR` or `agt run` in your shell | `account=<profile>` on the relay agent |
