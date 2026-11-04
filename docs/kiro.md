# Kiro CLI subagents: setup and use

Agents Router can run Kiro CLI workers (`kiro-cli chat --no-interactive`) the same way it runs Claude Code, Codex and Antigravity workers: in the background, from any host. Each worker is the official `kiro-cli` with your own login.

Its tests use a stand-in. It has been run live with kiro-cli 2.37.0 and 2.37.1 on macOS, on Kiro's V2 and V3 agent engines (see [Checked live](#checked-live)). [What isn't checked yet](#not-checked-yet) lists what a live run still has to confirm.

## Requirements

- Kiro CLI installed and signed in (`kiro-cli whoami`).
- Usually nothing more. Live headless runs worked without `KIRO_API_KEY`: on a plain `kiro-cli login` (V2), and on an IAM Identity Center login (V2 and V3). Kiro's docs say headless runs need that key, an API key from your Kiro account settings (Pro plans and up), so if a job fails with a login error, set it in the environment agt runs in. agt passes it through to the worker and never reads, stores or prints it.
- The `agt` package installed (see the [Claude Code guide](claude-code.md#1-install)).

Check it with:

```bash
agt doctor --runtime kiro
```

That runs `kiro-cli --version`, `kiro-cli chat --help` and `kiro-cli whoami --format json`, which make no model request. Under the binary line it shows two rows:

| Row | Example | Meaning |
|---|---|---|
| `engine` | `v2 (auto: kiro-cli 2.37.1)` | The engine Kiro jobs will name, and why ([below](#which-engine)) |
| `output` | `stream-json` | Jobs get Kiro's events: progress and follow-ups. `plain text (no --output-format)` on an older kiro-cli; `unknown` when `chat --help` couldn't be read |

When `[kiro] engine = "v3"` would make jobs fail, doctor lists it as a problem, with `agt config set kiro.engine auto` as the fix.

`agt setup` checks the login too, when you have a `~/.kiro` folder and `kiro-cli` is installed: if nobody is logged in, it lists `kiro-cli login` under "Still needed from you". It never logs in for you.

## One account

Kiro keeps its login in its own data store, not in `~/.kiro`, so agt can't keep a second login apart in another folder. Kiro jobs run on your one login, and `agt accounts add --runtime kiro` is refused.

## Running a job

```bash
agt run --runtime kiro --access read "List the modules that import the parser"
```

The task goes to Kiro on stdin. Each launch first runs `kiro-cli --version` and `kiro-cli chat --help` (no model request) to learn which flags the CLI has. When `chat --help` lists `--output-format`, agt adds `--output-format stream-json` and reads Kiro's events. An older kiro-cli prints plain text, and agt reads that.

| Access | What the worker may do without asking |
|---|---|
| `read` | Read and search files, and search and fetch from the web (`--trust-tools=fs_read,glob,grep,web_search,web_fetch`) |
| `edit` | The same, plus write files (`fs_write` added). No shell commands |
| `full` | Everything (`--trust-all-tools`) |

The trust list is the same on both engines; V3 reads the V2 names through its own aliases. agt never passes `--trust-workspace`.

A headless Kiro worker can't ask you, so any tool that isn't trusted is refused. A refusal doesn't fail the job: Kiro tells the worker "tool permission approval is not supported in non-interactive mode", the worker carries on without that tool, and the job usually ends `done` with an answer that says what it couldn't do. Read the answer, not only the job state.

`read` jobs can search and fetch from the web because those tools change nothing on your machine.

`edit` jobs have no shell. In the other runtimes, agt's review gate decides which commands run. For Kiro that would go through a `PreToolUse` hook, which needs a live check first. Use `access=full` when a Kiro job must run commands.

`model=` and `effort=` become `--model` and `--effort`.

With stream-json, `agt status` and `agt follow` show the tool count (`tool_uses`), the last tool and the text so far while a job runs. With plain text they show the text so far, and `tool_uses` stays 0.

## Which engine

Kiro has two agent engines, V2 (the pre-3.0 default) and V3 (the default from kiro-cli 3.0). agt names one on every job with `--v2` or `--v3` when the CLI has those flags, so `read`, `edit` and `full` jobs all run on the same engine. `[kiro] engine` in agt's [settings](configuration.md#the-settings) picks it:

| `[kiro] engine` | `chat.agentEngine` in `~/.kiro/settings/cli.json` | kiro-cli | Flag |
|---|---|---|---|
| `v2` or `v3` | any | has the flag | that one |
| `v3` | any | has no `--v3` | none; the job fails: "kiro-cli X has no V3 engine; update Kiro or set [kiro] engine = "auto"" |
| `v3` | any | `chat --help` failed | none; the job fails: "couldn't read kiro-cli's flags: <reason>" |
| `v2` | any | has no `--v2` | none; such a CLI has no other engine |
| `auto` (default) | `v2` or `v3` | has the flags | as set |
| `auto` | `v1` or another value | has the flags | none, so Kiro's own setting wins |
| `auto` | unset | 3.0 or later | `--v3` |
| `auto` | unset | 2.x, or a version agt can't read | `--v2` |
| `auto` | any | has no `--v2`/`--v3` | none, so Kiro picks as it always did |

```bash
agt config set kiro.engine v3
```

## What a worker sees

The worker uses your real `~/.kiro`: its agents, skills, steering and MCP servers. When [agt share](share.md) is on, that includes the Kiro copies of your other CLIs' agents and skills. agt passes no `--agent`, so Kiro uses your default agent. Kiro's [steering docs](https://kiro.dev/docs/steering/) say Kiro loads the global steering in `~/.kiro/steering/`, where agt share puts the shared instruction block, but a custom agent loads steering only when its `resources` list it. If your default is a custom agent, list the steering files in its `resources` as those docs show.

agt writes nothing into `~/.kiro` for a job, so a Kiro worker doesn't get the `agt-parent` tools (`message_parent`, `ask_parent`, `check_parent_messages`) or a Claude parent's MCP servers. It works from the task it was given.

## Follow-ups

A stream-json job has Kiro's session id: `sess_…` on V3, a plain uuid on V2. With it, a Kiro job takes follow-ups as a Codex job does:

| You send | What happens |
|---|---|
| `agt message`, `subagent_message` or a SendMessage to the `agt-kiro` relay, after the job ended | A new turn on the same session (`--resume-id <id>`) |
| The same while the job runs | "queued for the running subagent". The worker doesn't see it during its turn, since it has no parent-channel tools. When the turn ends, the message becomes a follow-up turn on the same session |

A resumed turn stays on its session's engine, whatever `[kiro] engine` says: `sess_…` gets `--v3` and a plain uuid gets `--v2`, because V2 can't open a V3 session. An id of any other shape gets the engine a new job would, with a warning.

A job from a kiro-cli without stream-json has no session:
- after it ends, `agt message` fails with "job … has no session to continue";
- a message sent while it ran is reported at the end as "1 message wasn't delivered: this job can't take follow-ups".

Start a new job with the whole task instead, or update Kiro.

## How a job ends

| Kiro | agt |
|---|---|
| Exit 0 with an answer (and `runFinished` status `success` with stream-json) | done |
| A `runFinished` status other than `success` | failed, with that status |
| No `runFinished` in stream-json | failed: "the run ended before Kiro reported a result" |
| Usage-limit or throttling wording | `usage_limit` |
| Not logged in, or an auth error | `auth`: run `kiro-cli login`, or set `KIRO_API_KEY` |
| Exit 3 | failed: a required MCP server didn't start |
| Exit 4 | failed: Kiro couldn't find the agent it was asked to use |

The answer is `runFinished`'s `finalText`, or the text after the last tool call when Kiro marks it cut short. V3's `[INFO]`, `[DEBUG]` and `[WARN]` lines on stderr are left out of error messages.

## Checked live

Run with kiro-cli 2.37.0 on macOS, V2 engine, signed in with `kiro-cli login` and no `KIRO_API_KEY`:

- headless `kiro-cli chat --no-interactive` jobs end `done` with Kiro's answer, on your login alone;
- `read` jobs: `--trust-tools=fs_read,glob,grep,web_search,web_fetch` is accepted, and `web_search` works;
- a tool left out of `--trust-tools` is refused with "tool permission approval is not supported in non-interactive mode", and the job still exits 0;
- `full` jobs: `--trust-all-tools` gives the worker the web tools (and the shell).

Run with kiro-cli 2.37.1 on macOS, signed in with IAM Identity Center and no `KIRO_API_KEY`:

- V3 headless runs (`--v3`) work on that login;
- on V3, agt's `read` list reads a file and refuses a write;
- on V3, the `edit` list (`fs_write` added) allows the write and refuses a shell command;
- `--resume-id sess_…` continues a V3 session;
- `--output-format stream-json` works on V2 and V3;
- a never-trusted folder's `.kiro/steering` loads without `--trust-workspace`;
- V2 loads an agent whose `tools` is `["read"]` (Kiro's universal tags, which [share](share.md#what-a-copy-holds) writes);
- `kiro-cli agent upgrade --dry-run` calls an agent with tag `tools` "already universal".

## Not checked yet

These come from Kiro's docs or are guesses, and a live run still has to confirm them:

- whether some plans or setups do need `KIRO_API_KEY` for headless runs;
- the exit code and wording of a usage limit, and the shape of a failed `runFinished`;
- kiro-cli 3.x itself (the checks above ran on 2.37.1 with `--v3`);
- whether a headless run loads `~/.kiro/steering/AGENTS.md`, as Kiro's docs say (a workspace's `.kiro/steering` does);
- whether V3 loads `~/.kiro/settings/mcp.json`, where share writes MCP servers;
- whether a `PreToolUse` hook can carry agt's review gate in headless mode, which would give `edit` jobs a shell;
- what Kiro's terms say about running `kiro-cli` from another agent with your own login.
