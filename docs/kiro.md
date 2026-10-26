# Kiro: setup and use

Agents Router can run Kiro CLI workers (`kiro-cli chat --no-interactive`) the same way it runs Claude Code, Codex and Antigravity workers: in the background, from any host. Each worker is the official `kiro-cli` with your own login.

This runtime is new. It's built from Kiro's documentation and tested against a stand-in, not yet against the real CLI. [What isn't checked yet](#not-checked-yet) lists what a live run still has to confirm.

## Requirements

- Kiro CLI installed and signed in (`kiro-cli whoami`).
- Kiro's docs say headless runs need `KIRO_API_KEY`, an API key from your Kiro account settings (Pro plans and up). If a job fails with a login error, set it in the environment agt runs in. agt passes it through to the worker and never reads, stores or prints it.
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
| `read` | Read files and search them (`fs_read`, `glob`, `grep`; `read`, `glob`, `grep` on Kiro's V3 engine) |
| `edit` | The same, plus write files (`fs_write`, or `write` on V3). No shell commands |
| `full` | Everything (`--trust-all-tools`) |

A headless Kiro worker can't ask you, so any tool that isn't trusted is refused. The engine comes from `chat.agentEngine` in `~/.kiro/settings/cli.json`; without it, agt uses the V2 names.

`edit` jobs have no shell. In the other runtimes, agt's review gate decides which commands run. For Kiro that would go through a `PreToolUse` hook, which needs a live check first. Use `access=full` when a Kiro job must run commands.

`model=` and `effort=` become `--model` and `--effort`.

## What a worker sees

The worker uses your real `~/.kiro`: its agents, skills, steering and MCP servers. When [agt share](share.md) is on, that includes the Kiro copies of your other CLIs' agents and skills.

agt writes nothing into `~/.kiro` for a job, so a Kiro worker doesn't get the `agt-parent` tools (`message_parent`, `ask_parent`) or a Claude parent's MCP servers. It works from the task it was given.

## How a job ends

| Kiro | agt |
|---|---|
| Exit 0 with an answer | done |
| Usage-limit or throttling wording | `usage_limit` |
| Not logged in, or an auth error | `auth`: run `kiro-cli login`, or set `KIRO_API_KEY` |
| Exit 3 | failed: a required MCP server didn't start |
| Exit 4 | failed: Kiro couldn't find the agent it was asked to use |

## Not checked yet

These come from Kiro's docs or are guesses, and a live run still has to confirm them:

- whether headless runs work on your login alone, or always need `KIRO_API_KEY`;
- the exit code and wording of a usage limit;
- the exact V3 tool ids `--trust-tools` takes (`read`, `glob`, `grep`, `write`);
- `--output-format stream-json` events, which would give progress and a session id (agt reads plain text until then);
- resuming a job with `--resume-id`, which needs the session id;
- whether a `PreToolUse` hook can carry agt's review gate in headless mode, which would give `edit` jobs a shell;
- what Kiro's terms say about running `kiro-cli` from another agent with your own login.
