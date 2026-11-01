---
name: orchestrator-setup
description: Design or redesign the user's whole Agents Router role table (Decision Maker, Reviewer, Builder, Verifier and custom roles, each with its providers) from the CLIs, logins, accounts and quota they have. Use it when the user says "set up my roles", "redo my roles", "design my roles from scratch", "which accounts should do what", wants roles after skipping them at agt setup, or has added or lost a CLI or account and wants the table rethought, even if they don't say "orchestrator". It proposes a table with agt roles propose and writes only after approval. Not for one change to one role, such as swapping a provider, changing a model or turning a role off (agents-router), not for "rebalance my roles" by quota (orchestrator-rebalance), not for installing or repairing agt itself (agents-router-setup), and not for calling a role to do work (agents-router).
---

# Orchestrator setup

This designs the whole role table again. `agt setup` already proposes roles in its one setup question; use this skill when the user skipped that step, or wants the table rethought later. One change to one role is the agents-router skill's Roles rule, not this.

## 1. Look at what the user has

Run these and read the results; don't ask the user what agt can tell you:

```bash
agt roles show --json        # the roles today, which are off, which providers are unusable
agt accounts list --json     # the accounts per CLI and whether each is logged in
```

`agt roles propose` in step 2 reads the quota left per account itself.

If a command fails because `agt` is missing or too old for `agt roles`, stop and use the `agents-router-setup` skill. If `agt roles show` exits 2 naming a file and line, the settings have a bad hand edit: repeat the line to the user and offer the `agt roles config` command that fixes it before going on.

## 2. Propose

```bash
agt roles propose --json > "$TMPDIR/agt-roles-proposal.json"
```

It returns the table (`roles`: each role's access and providers) and one reason per role (`reasons`). agt's choices: read roles mix vendors, the Builder puts the strongest model with the most quota first, and the Decision Maker gets one provider. Antigravity and Kiro have no usage data, so they're ranked by recent jobs and cooldowns.

## 3. Show it and ask once

Show the whole proposal as a table: role, access, providers in order (for edit roles the order is the priority), and the reason. Name what differs from today's table. Then ask once with your question tool: **Save it** (recommended) · **Change a role** · **Keep my current roles**.

- **Change a role:** ask what that role should have. When an edit role gets more than one provider, ask which goes first. Update the JSON file, then show the whole table again and ask again.
- A custom role is one more row: a name (not `claude`, `codex`, `antigravity` or `kiro`), `access`, `description` (its "use me when" line), optional `prompt_file`, and providers.
- **Keep:** change nothing, and say so.

Write nothing before the user approves.

## 4. Write

```bash
agt roles config --from "$TMPDIR/agt-roles-proposal.json" --user-said "<the user's approval, in their words>" --json
```

`--user-said` is needed because a new table can add a provider to an edit role or turn a role on. If agt exits 3 (`needs_user_words`), ask the user as its `ask` says and run its `retry` with the answer. If it exits 2, nothing was written: show the reason and fix the table with the user.

Then report from agt's result: the file and level it wrote, any workspace file that overrides a role here (`overridden_by`), the role agents it created or removed (they load after `/reload-plugins` or a restart), and the undo command.
