# Live check in the sandbox

`scripts/sandbox.sh` (create, reinstall, check, destroy) runs from the repository in a normal terminal; it refuses inside the sandbox shell. Everything else goes through the sandbox's launchers, which work from that same terminal and change nothing in it:

| Launcher | Runs |
|---|---|
| `claude-agt-a`, `claude-agt-b` | Claude Code on the sandbox profile `.claude-a` or `.claude-b` |
| `agt-sandbox <command>` | One command inside the sandbox, such as `agt-sandbox agt accounts` |
| `agt-sandbox` | A shell inside the sandbox; `exit` leaves it |

The launchers start in the sandbox's work folder outside your home (`scripts/sandbox.sh check` prints it), so no real `CLAUDE.md` loads. To test on a repository, clone it there; never start a sandbox session in a real repo or under your home folder. Ask the person before step 3: the jobs use a little quota.

## 1. Build and isolate

```bash
scripts/sandbox.sh create        # from a normal terminal, in the repository
scripts/sandbox.sh check
```

Expected:
- The sandbox `agt` is `~/agt-sandbox/bin/agt` at the repository's version.
- The real `claude` and `agt` on PATH are unchanged, and the launchers are links into `~/agt-sandbox/launchers`.
- `launchctl list | grep agents-router` shows as many jobs as before.

## 2. Logins (the person does these)

```bash
claude-agt-a               # profile a: /login, then /exit
claude-agt-b               # profile b: /login, then /exit
agt-sandbox codex login    # optional
```

The scenarios need two different logins in a and b, for example a personal Max login and a Team one.

## 3. Agent-driven install

1. Run `claude-agt-a`.
2. Paste the README's Install prompt, with the install line changed to `uv tool install --managed-python <repository path>`.

Expected:
- The agent runs `agt guide setup` and asks one combined question.
- `agt setup` installs the plugin from the clone into `claude-a` and `claude-b`, and into Codex when it's logged in.
- The report ends with reloads only.
- Nothing under the real home changed: compare the modification times of `~/.claude/settings.json` and `~/.claude/plugins/installed_plugins.json` with the ones before.

## 4. Delegation

In profile a, after `/reload-plugins`:
- "Run a tiny read-only check on claude-b": `agents-router:agt-claude` with `account=claude-b`, and no `env -u` anywhere.
- `account=claude-a` is refused with "this session runs in".

## 5. Two sessions at once

1. Run `claude-agt-b` in a second terminal tab.
2. Each session delegates a short job to the other profile while the other's job runs.

Expected: each session sees only its own results (`subagent_list`, and the pending-results hook after `/compact`).

## 6. Update and uninstall

1. After a code change, run `scripts/sandbox.sh reinstall`. Then `agt-sandbox agt update --no-package` updates the plugin in both profiles.
2. In `agt-sandbox` (the shell), follow section 10 of `agt guide setup`.
3. `scripts/sandbox.sh check` shows no plugin left; then run `scripts/sandbox.sh destroy`, which also removes the launchers.

## 7. Kiro: outside the sandbox, with a scratch agt home

Kiro is the one runtime checked outside the sandbox:
- `scripts/sandbox.sh` swaps `HOME`, which logs Kiro out.
- A logged-out headless Kiro run once signed in on its own and spent credits.
- Kiro keeps its login in a shared store that a `HOME` override doesn't isolate.

So the check runs on the person's real Kiro login, with everything of agt's kept apart:
- this branch's agt from the checkout, by absolute path, with the scratch folder as the job's folder: `PYTHONPATH=<checkout>/src python3 -m agents_router run --runtime kiro --access read --cwd <scratch> …` (its background runner starts the same way, so the installed `agt` never runs these jobs);
- a scratch `AGT_HOME` and `AGT_CONFIG_DIR` (`mktemp -d`), with a hand-written minimal `settings.toml` holding only the `[kiro]` table each job needs;
- a scratch work folder holding `hello.txt`, whose first line is `alpha-line-one`.

Never run `agt setup`, `agt share`, `kiro-cli login` or `kiro-cli logout` there.

Ask the person first, and say what it costs: about 1 Kiro credit for the 4 tiny jobs below. Then:

1. Run `kiro-cli whoami --format json`. Stop if it shows no account.
2. Note the modification time and file list of `~/.agents-router`.
3. Run the jobs with `--runtime kiro --access read`:

| Job | Settings | Task | Expected |
|---|---|---|---|
| a | `engine = "v3"` | "Reply with the first line of hello.txt" | done with `alpha-line-one`; `tool_uses` ≥ 1; a `sess_` session id; `--v3` in its argv |
| b | `engine = "auto"` | the same | `--v2` on kiro-cli 2.x, `--v3` on 3.x |
| c | — | `agt message <job a> "What was that line?"` | answers from the same session |
| d | `engine = "v3"` | "Create made.txt" | `made.txt` absent: the `read` list refuses the write |

4. Check that `~/.agents-router` has the same modification time and file list as before.
