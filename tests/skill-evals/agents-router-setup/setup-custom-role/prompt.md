---
# Isolated run: place-files.sh builds .agt-eval/ in the workspace (its own HOME, AGT_HOME, CLAUDE_CONFIG_DIR,
# fake CLIs, agt copied from this clone). Run with tests/skill-evals/bin first on PATH and --scaffold;
# see .claude/skills/agt-maintainer/references/release.md. No real job and no real ~/.agt.
tags: [agents-router-setup, setup, roles, release-gate]
max_turns: 30
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Set up agt for me. The roles it proposes are fine. I also want a role of my own called docs-writer: it updates the README and the docs whenever a command or a flag changes, and it touches nothing but docs. Give it edit access on Codex.
