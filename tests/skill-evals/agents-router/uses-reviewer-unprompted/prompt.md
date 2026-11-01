---
# Isolated run: place-files.sh builds .agt-eval/ in the workspace (its own HOME, AGT_HOME, CLAUDE_CONFIG_DIR,
# fake CLIs, agt copied from this clone). Run with tests/skill-evals/bin first on PATH and --scaffold;
# see .claude/skills/agt-maintainer/references/release.md. No real job and no real ~/.agt.
tags: [agents-router, roles, release-gate]
max_turns: 30
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash, Agent]
---

Partial refunds are broken in billing.py: returning 1 of 3 items on a 3000-cent order should give back 1000 cents, and the refund should be added to the store balance, not taken off. Fix it and add tests for both.
