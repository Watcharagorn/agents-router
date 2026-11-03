---
name: orchestrator-customize
description: Change what an Agents Router role agent (agt-builder, agt-reviewer, agt-verifier, agt-decision-maker or a custom agt-<role>) does on every call, or when the main agent calls it, by editing the two parts of its agt-<role> file the user owns. Use it whenever the user wants a standing rule for a role ("my builder keeps touching migrations, stop that", "make the verifier always run the e2e suite", "remove that rule from the builder") or a different moment to call it ("call the reviewer before every commit"), even without saying "agent file" or "role rules". Not for providers, models or turning a role on or off (agents-router), not for the whole role table or "set up my roles" (orchestrator-setup), not for quota (orchestrator-rebalance), not for running a role on a task, and not for writing a new subagent of the user's own.
---

# Orchestrator customize

Each `agt-<role>` agent file has two parts the user owns. agt keeps both through every rewrite (`agt setup`, `agt update`, `agt roles config`) and owns everything else in the file.

| Part | Where in the file | What it changes |
|---|---|---|
| When to call | The start of the frontmatter `description`, before `Runs the <role> role through Agents Router (` | When the main agent calls the role. The main agent reads it |
| Role rules | The lines between `<!-- agt-role-rules: ... -->` and `<!-- end agt-role-rules -->` in the `## Role rules` section at the end | What the worker does. agt puts them before the task on every call |

Edit only between those boundaries. agt regenerates the rest, so an edit there is lost on the next sync. That includes the relay steps, the model and tools lines, and the description from `Runs the <role> role…` on. Never touch `[roles.*]` settings for this either. Providers and on/off belong to `agt roles config` (agents-router).

## 1. Read what is there

```bash
agt roles show --json
```

For the role the user means, read `when_to_call`, `rules_chars`, `rules_state` and `agent_files`. `rules_state` is about this CLI's own file. `agent_files` lists every copy: each Claude profile, Codex and Antigravity keeps its own file, with `cli`, `where`, `file` and `state`.

- `rules_state` is `broken`: show the user agt's `problem` line (the file and line; it is in that file's `agent_files` entry) and fix that first. Usually a marker line was deleted or doubled. Put back exactly one start and one end marker around the rules.
- `rules_state` is `legacy` (a file from agt 0.11, no Role rules section yet): run `agt update` once. It adds the section and keeps the sentence.
- `rules_state` is `yours`: the user deleted the management line, so agt never rewrites the file, but `agt run` still sends its Role rules. Edit it the same way; `yours` counts as done in step 5.
- `rules_state` is `absent`, or the role isn't listed: there is no agent file for it here. Say so, and say why: a role set only in a project's settings gets no agent, and roles that are off have no file. For an enabled role in the user settings, `agt update` writes it. Don't create the file by hand.
- `rules_state` is `null`: agt doesn't know which CLI it runs in. Use `agent_files` and edit the file of the CLI you run in.

## 2. Pick the part

- **The worker's behaviour**: "always run the tests", "never edit migrations", "check for security issues", "end with a summary". This goes in the **Role rules**.
- **When the role is called**: "before every commit", "only for big changes", "also for docs". This goes in the **when to call** sentence.
- Some requests need both: "call the verifier after every builder run and make it run e2e". Edit both parts and say which change went where.

## 3. Edit this CLI's file

Open `agent_files[...].file` for this CLI (the entry whose `cli` matches the CLI you run in; for Claude Code, the profile you run in). Then:

- **Role rules**: write plain, short imperative lines between the two markers, one rule per line. Keep rules that are already there unless the user asked to change or remove them. They steer a worker that may edit code, so write exactly what the user asked for, no more. In a Codex `.toml` file the rules sit inside `developer_instructions = '''...'''` and can't contain `'''`.
- **When to call**: replace only the text before `Runs the <role> role through Agents Router (`. Keep the frontmatter valid: in a `.md` file the description is one quoted string, so escape any `"` you add. To go back to agt's default, delete the sentence; agt writes the default on the next sync.

## 4. Offer the other copies

When `agent_files` lists other files (another Claude profile, Codex, Antigravity), ask once whether to make the same change there. Each copy is separate on purpose: a rule can apply to one profile only. Apply it to the ones the user picks, in each file's own format.

## 5. Check and report

Run `agt roles show --json` again and confirm `rules_state` is `valid` (or `yours`) and `rules_chars` or `when_to_call` shows the change for every file you edited. If a file reads `broken`, fix the line agt names before you finish. Then tell the user:

- Which file or files changed, and which part.
- When the rules take effect: agt reads them on every new call, so the next `agt-<role>` call uses them. A job already running keeps the rules it started with.
- When the sentence takes effect: Claude Code picks up an edited agent file in `~/.claude/agents/` on the next delegation. For another profile's folder, or in Codex or Antigravity, a new session may be needed before the main agent sees the new sentence.
- That `agt update` and `agt roles config` keep these edits. Turning the role off archives them, and turning it back on restores them.
