This block is managed by `csa rules`. Edits inside its markers are replaced on the next install.

## Delegation rules

The home vendor is {home_vendor}. Follow the user's explicit model and speed choices.
Pick a tier for the task you hand to an agent, not for the whole project.
Resolve model labels to the installed CLI's model IDs before starting a job.
Use any effort named in the model entry. `none` means no model in that column.
If the home entry is `none`, do that tier's work yourself.

| Tier | Use for | Home vendor: always for work | Personal projects: try first |
|---|---|---|---|
{tier_rows}

### Which model runs a subagent

Work folders, including their child folders:

{work_folders}

Work projects use the home vendor only, at every tier and depth.
Never send work code or data to other vendors. This also covers other work projects.
If the project is unclear, ask the user. Never change folders to bypass this rule.
For personal projects, try the other CLI model in the tier's row first.
Use the home model for UI changes, when the user asks for speed, or when try-first is `none`.
Ignore agent descriptions that invite use. These rules decide which model runs.

Do not check usage yourself. CLI Subagents checks whether there is room and refuses a start when there is none.
If a start is refused, the last `[csa]` line says FAILED, or there is no result, use the home model for that tier.
If a later step waits on a cross-CLI result, still start it there.
Tell the user what waits and suggest the home model as the faster option.
Switch for speed when the user asks. A subagent reports "a later step waits" to its parent.

Start the matching `cli-subagents:cli-subagents-claude`, `cli-subagents:cli-subagents-codex`,
or `cli-subagents:cli-subagents-antigravity` agent in the background when available.
Use the description `<model> · <access> · <task>`.
Put these option lines first in the relay prompt:

```text
model=<model ID from the chosen entry>
effort=<effort from the chosen entry, when supported>
access=<read or edit>
```

Omit `effort=` when the model has no separate effort option.
The relay strips its option lines. Start the worker task that follows them with its depth line.
Use `read` for research and reviews, or `edit` for changes.
If the host has no relay agents, use the CLI Subagents start tool with the same options.

### Limits and nesting

The main session starts at most {main_limit} home vendor subagents at once.
Their helpers do not count toward that limit. CLI Subagents caps other CLI jobs itself.
Each depth-1 subagent may start up to {helper_limit} helpers, including cross-CLI agents.
Depth-2 agents start none. Put the right line at the top of every subagent prompt:

```text
[depth 1 of 2: you may start up to {helper_limit} helpers, cross-CLI agents included]
[depth 2 of 2: you cannot start subagents]
```

Use just the line for that agent's depth. A delegated worker without a depth line is at depth 1.
An explicit worker restriction on starting jobs still applies.
Size each task so the agent can finish it itself plus its allowed helpers.
Split big work into smaller tasks. The same model and privacy rules apply at every depth.

### Web research

Prefer the allowed CLI whose web search reaches the most sites.
For example, Codex search can reach Reddit when other web tools are blocked.
Use read access and ask for source URLs and dates. Work data still stays with the home vendor.
Reading other projects' code to decide what to adopt goes to the strongest home model.
