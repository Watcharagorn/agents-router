---
name: orchestrator-rebalance
description: Rebalance the user's Agents Router roles by the quota left on each account, with agt roles propose --rebalance, and save only the moves the user approves. Use it whenever the user says "rebalance my roles", "rebalance the roles", "my roles keep hitting limits", "move roles off the account that's running out", "claude-work is almost out, shift the builder", or a role run printed agt's rebalance hint, even if they don't say "orchestrator". Not for one change the user already spelled out, such as "put codex first on the builder" or "swap kiro for antigravity on the verifier" (agents-router), not for designing the whole table from scratch or "set up my roles" (orchestrator-setup), not for raising limits like max_jobs or usage_ceiling (agents-router, agt config), not for a job that failed on quota once (agt already skips exhausted accounts on its own), and not for what a role agent does or when it's called, such as "stop the builder touching migrations" (orchestrator-customize).
---

# Orchestrator rebalance

This proposes a new provider order or provider set for the roles from the quota left on each account, and changes nothing until the user approves. Day to day, agt already skips accounts that are out of quota; a rebalance moves the mapping itself so roles stop landing on them first.

## 1. Propose

```bash
agt roles propose --rebalance --json > "$TMPDIR/agt-roles-rebalance.json"
```

It returns `quota`, one entry per account (`runtime`, `account`, `left_percent`, `window`, `resets_at`, `observed_at`, `cooling_until` and `text`, in the words agt's room line uses), the table (`roles`, every role) and one reason per role (`reasons`). Without `--json` it prints the quota lines first, then each role:

```
claude claude-work 8% left (5h, resets 14:20)
claude personal unknown (no usage data)
codex work unknown (no usage data)

reviewer  codex:work/gpt-6.1-sol:high, antigravity:primary
          no change
builder   codex:work/gpt-6.1-sol, claude:claude-work/opus
          claude:claude-work has 8% left (under 20%) until it resets at 14:20: moved back in the order
verifier  claude:personal/sonnet, kiro:primary
          no change
Nothing written. To save it: agt roles propose --rebalance --json > roles.json, then agt roles config --from roles.json
```

A reading older than 30 minutes adds its age, as in `(5h, resets 14:20, as of 40 min ago)`. agt proposes a move when an account has under 20% left in its current window; that threshold is fixed. Antigravity and Kiro have no usage data, so they show as unknown and are ranked by recent jobs and cooldowns. Roles that are off are skipped.

If `agt roles propose` exits 2 naming a file and line, the settings have a bad hand edit: repeat agt's line to the user as printed and offer the command after its `fix:` exactly (such as `agt roles config builder --unset access --user`), not one of your own. For a TOML syntax error, which has no fix command, show the user the line agt names. Don't patch the file.

## 2. Show the moves and ask once

If nothing changes, say so with the quota lines, and stop.

Otherwise show each changed role: old providers → new providers (for edit and full roles, the order is the priority), and its reason from `reasons`, such as "claude:claude-work has 8% left (under 20%) until it resets at 14:20: moved back in the order". Say which roles stay as they are. Then ask once with your question tool: **Apply** (recommended) · **Keep current mapping**. If the user wants only some moves, drop the others from the JSON file and show the result again.

Change nothing before the answer. **Keep** leaves the mapping as it is.

## 3. Apply

```bash
agt roles config --from "$TMPDIR/agt-roles-rebalance.json" --user-said "<the user's approval, in their words>" --json
```

`--user-said` is needed because a move can add a provider to an edit or full role. If agt exits 3 (`needs_user_words`), ask as its `ask` says and run its `retry` with the answer. If it exits 2, nothing was written: show the reason.

Report from agt's result: the roles that changed, the file and level, any workspace file that overrides a role here (`overridden_by`), and the undo command. Role agents need no reload: they read the mapping each time they run.
