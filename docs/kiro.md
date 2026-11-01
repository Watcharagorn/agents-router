# Kiro CLI subagents: setup and use

Agents Router can run Kiro CLI workers (`kiro-cli chat --no-interactive`) the same way it runs Claude Code, Codex and Antigravity workers: in the background, from any host. Each worker is the official `kiro-cli` with your own login.

This runtime is new. Its tests use a stand-in, and it has been run live with kiro-cli 2.37.0 on macOS, on the V2 tool engine (see [Checked live](#checked-live)). [What isn't checked yet](#not-checked-yet) lists what a live run still has to confirm.

## Requirements

- Kiro CLI installed and signed in (`kiro-cli whoami`).
- Usually nothing more. Live headless runs worked on a plain `kiro-cli login`, without `KIRO_API_KEY`. Kiro's docs say headless runs need that key, an API key from your Kiro account settings (Pro plans and up), so if a job fails with a login error, set it in the environment agt runs in. agt passes it through to the worker and never reads, stores or prints it.
- The `agt` package installed (see the [Claude Code guide](claude-code.md#1-install)).

Check it with:

```bash
agt doctor --runtime kiro
```

That runs `kiro-cli --version` and `kiro-cli whoami --format json`, which make no model request.

`agt setup` checks the login too, when you have a `~/.kiro` folder and `kiro-cli` is installed: if nobody is logged in, it lists `kiro-cli login` under "Still needed from you". It never logs in for you.

## One account

Kiro keeps its login in its own data store, not in `~/.kiro`, so agt can't keep a second login apart in another folder. Kiro jobs run on your one login, and `agt accounts add --runtime kiro` is refused.

## Running a job

```bash
agt run --runtime kiro --access read "List the modules that import the parser"
```

The task goes to Kiro on stdin. The answer is Kiro's plain-text output.

| Access | What the worker may do without asking |
|---|---|
| `read` | Read and search files, and search and fetch from the web (`fs_read`, `glob`, `grep`, `web_search`, `web_fetch`; `read` in place of `fs_read` on Kiro's V3 engine) |
| `edit` | The same, plus write files (`fs_write`, or `write` on V3). No shell commands |
| `full` | Everything (`--trust-all-tools`) |

A headless Kiro worker can't ask you, so any tool that isn't trusted is refused. A refusal doesn't fail the job: Kiro tells the worker "tool permission approval is not supported in non-interactive mode", the worker carries on without that tool, and the job usually ends `done` with an answer that says what it couldn't do. Read the answer, not only the job state. The engine comes from `chat.agentEngine` in `~/.kiro/settings/cli.json`; without it, agt uses the V2 names.

`read` jobs can search and fetch from the web because those tools change nothing on your machine.

`edit` jobs have no shell. In the other runtimes, agt's review gate decides which commands run. For Kiro that would go through a `PreToolUse` hook, which needs a live check first. Use `access=full` when a Kiro job must run commands.

`model=` and `effort=` become `--model` and `--effort`.

Kiro prints plain text, so while a job runs, `agt status` and `agt follow` show its text so far but no tool steps (`tool_uses` stays 0).

## What a worker sees

The worker uses your real `~/.kiro`: its agents, skills, steering and MCP servers. When [agt share](share.md) is on, that includes the Kiro copies of your other CLIs' agents and skills. agt passes no `--agent`, so Kiro uses your default agent. Kiro's [steering docs](https://kiro.dev/docs/steering/) say Kiro loads the global steering in `~/.kiro/steering/`, where agt share puts the shared instruction block, but a custom agent loads steering only when its `resources` list it. If your default is a custom agent, list the steering files in its `resources` as those docs show.

agt writes nothing into `~/.kiro` for a job, so a Kiro worker doesn't get the `agt-parent` tools (`message_parent`, `ask_parent`) or a Claude parent's MCP servers. It works from the task it was given.

## No follow-ups yet

Kiro prints plain text, so agt gets no session id from it. Without one:
- **A finished Kiro job can't be continued:** `subagent_message`, `agt message` or a SendMessage to the `agt-kiro` relay fails with "job … has no session to continue".
- **A follow-up sent while a Kiro job runs doesn't reach the worker:** Kiro has no tool to read it, and agt can't start a follow-up turn.

Start a new job with the whole task instead. Kiro's `--output-format stream-json` would give agt a session id (see [Not checked yet](#not-checked-yet)).

## How a job ends

| Kiro | agt |
|---|---|
| Exit 0 with an answer | done |
| Usage-limit or throttling wording | `usage_limit` |
| Not logged in, or an auth error | `auth`: run `kiro-cli login`, or set `KIRO_API_KEY` |
| Exit 3 | failed: a required MCP server didn't start |
| Exit 4 | failed: Kiro couldn't find the agent it was asked to use |

## Checked live

Run with kiro-cli 2.37.0 on macOS, V2 engine, signed in with `kiro-cli login` and no `KIRO_API_KEY`:

- headless `kiro-cli chat --no-interactive` jobs end `done` with Kiro's answer, on your login alone;
- `read` jobs: `--trust-tools=fs_read,glob,grep,web_search,web_fetch` is accepted, and `web_search` works;
- a tool left out of `--trust-tools` is refused with "tool permission approval is not supported in non-interactive mode", and the job still exits 0;
- `full` jobs: `--trust-all-tools` gives the worker the web tools (and the shell).

## Not checked yet

These come from Kiro's docs or are guesses, and a live run still has to confirm them:

- whether some plans or setups do need `KIRO_API_KEY` for headless runs;
- the exit code and wording of a usage limit;
- the exact V3 tool ids `--trust-tools` takes (`read`, `glob`, `grep`, `web_search`, `web_fetch`, `write`);
- `--output-format stream-json` events, which would give progress and a session id (agt reads plain text until then);
- resuming a job with `--resume-id` once agt has that session id, which would make [follow-ups](#no-follow-ups-yet) work;
- whether a headless run loads `~/.kiro/steering/AGENTS.md`, as Kiro's docs say;
- whether a `PreToolUse` hook can carry agt's review gate in headless mode, which would give `edit` jobs a shell;
- what Kiro's terms say about running `kiro-cli` from another agent with your own login.
