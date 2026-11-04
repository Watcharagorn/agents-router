---
name: agt-maintainer
description: How to maintain the Agents Router (agt) repository. Covers changing its code, plugin files (agents, skills, commands, hooks), docs or tests; fixing a bug or adding a feature; running the tests; a live check in the sandbox; reviewing a change; and preparing a release or pull request. Use it whenever you work inside this repository.
---

# Maintaining Agents Router

This is the procedure. The facts it relies on, such as the layout, the rules for tests and the fake CLIs, are in [AGENTS.md](../../../AGENTS.md); read it first.

## 1. Start

1. Run `git status` and `git branch --show-current`. Never commit to `main`; branch first (`git switch -c claude/<topic>`).
2. Read `AGENTS.md`, then the files you'll change and their tests.

## 2. Change

- Python 3.11+ and the standard library only. The one exception is `ui.py`, which loads rich inside functions for a person at a terminal. Agents, pipes, tests and hooks get its plain renderer.
- A new test module imports `tests/helpers.py`. Tests never call the real `claude`, `codex`, `agy` or `kiro-cli`, and never touch the real `~/.claude*`, `~/.codex`, `~/.gemini` or `~/.agents-router`. Teach the fake CLIs (`tests/fake_*.py`) what you need instead.
- Docs keep their style: short sentences, tables for lookups, no hedging, no "simply".

Which docs move with which change:

| Change | Also update |
|---|---|
| A CLI command or flag | README task table, `docs/*.md`, `src/agents_router/helptext.py` (the help page), relay agents in `agents/` if they run it |
| Setup, install, update, profiles | `src/agents_router/guides/setup.md`, then `python3 scripts/sync_guide.py` to copy it into `skills/agents-router-setup/SKILL.md` |
| Profiles or accounts | `docs/claude-code.md`, `docs/configuration.md`, the setup guide |
| Sharing | `docs/share.md` |
| Delegation behavior | `skills/agents-router/SKILL.md`, `commands/delegate.md`, `agents/agt-*.md` |
| Roles: the agents-router Roles section, setup's role step, `skills/orchestrator-*` | The cases in `tests/skill-evals/<skill>/` and its `triggers.json`; the skill gate in [references/release.md](references/release.md) |

## 3. Test

```bash
export PYTHONPATH=src:tests
python3 -m unittest discover -s tests -t tests        # everything, about 3-4 minutes
python3 -m unittest test_setup test_guide             # one or a few modules
```

- **Known failure:** `test_help.test_other_commands_keep_argparse_help` fails on Python 3.15, whose argparse changed. Any other failure blocks.
- **Unsure a failure is yours?** Run the same module on a clean tree with `git stash`, the test, then `git stash pop`.

## 4. A change the plugin ships

Anything the plugin ships counts: `agents/`, `skills/`, `commands/`, `hooks/`, the MCP server, or the `agt` code those call.

Raise `version` in these three files together:
- `.claude-plugin/plugin.json`
- `pyproject.toml`
- `src/agents_router/__init__.py`

Claude Code updates an installed plugin only when that version changes. `tests/test_guide.py` checks that the three match.

## 5. Live check: only in the sandbox, only with an OK

Real jobs use the person's quota. Before running one, ask, and say roughly what it uses: one Haiku job costs about a cent. Everything runs in the sandbox, which keeps the person's real setup untouched:

```bash
scripts/sandbox.sh create      # once; `reinstall` after each code change
claude-agt-a                   # Claude Code on sandbox profile a; claude-agt-b for profile b
agt-sandbox <command>          # one command inside the sandbox; alone, a sandbox shell
```

- The launchers work from any terminal and change nothing in it: the person's own `claude` and `agt` stay their real ones.
- The person logs in to `claude-agt-a` and `claude-agt-b` themselves. Their logins go to the sandbox's own keychain; the sandbox's `claude` runs only on its named profiles.
- Work through the scenarios in [references/live-check.md](references/live-check.md).
- Kiro is the exception: the sandbox logs it out, so it's checked with a scratch `AGT_HOME` on the real Kiro login ([references/live-check.md §7](references/live-check.md#7-kiro-outside-the-sandbox-with-a-scratch-agt-home)).
- Finish with `scripts/sandbox.sh destroy` when the person agrees.

## 6. Review

- **An independent pass.** Start `agents-router:agt-codex` with `access=read`. Give it the diff (`git diff main...HEAD`) and the plan, never your conclusions, and ask for bugs and gaps.
- **Open choices.** Settle them with the decision-maker agents (`system-decision-maker`, `ux-decision-maker`, `general-decision-maker`) instead of asking the person, unless the choice is theirs: scope, money or anything outward-facing.
- **Fold the findings in.** Then tell the person what you left out, and why.

## 7. Release

Steps and exact commands: [references/release.md](references/release.md).

In short:
1. Run the full suite. When the change touches a skill with cases in `tests/skill-evals/`, that skill's graded cases must pass too, and only those (`--tag <skill>`, `-j 4`): real model runs on the person's quota, so only with their OK. The benchmark and trigger tuning only advise.
2. Raise the version.
3. Commit with the subject `Agents Router X.Y.Z: <what changed for the person>`.
4. Push the branch and open a pull request.
5. After the merge, run `agt update` and tell the person to `/reload-plugins`.

Commit or push only when the person asks.

## 8. Never

| Never | Instead |
|---|---|
| Read, copy or print credential files, or values from `.claude.json` | Hashes and `claude auth status` only |
| Touch the real `~/.claude*`, `~/.codex` or `~/.agents-router` from a test | The fakes and `tests/helpers.py` |
| Run `agt doctor --live` or a real job outside the sandbox, or without an OK | The sandbox, after asking (Kiro: a scratch `AGT_HOME`, [live-check §7](references/live-check.md#7-kiro-outside-the-sandbox-with-a-scratch-agt-home)) |
| Edit `~/.agents-router/homes/` | agt rebuilds those folders |
| Reach a Claude profile with `env -u CLAUDE_CONFIG_DIR` or `agt run` in the shell | `agents-router:agt-claude` with `account=<profile>` |
