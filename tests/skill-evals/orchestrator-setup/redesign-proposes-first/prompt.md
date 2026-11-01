---
# Isolated run: place-files.sh builds .agt-eval/ in the workspace (its own HOME, AGT_HOME, CLAUDE_CONFIG_DIR,
# fake CLIs, agt copied from this clone). Run with tests/skill-evals/bin first on PATH and --scaffold;
# see .claude/skills/agt-maintainer/references/release.md. No real job and no real ~/.agt.
tags: [orchestrator-setup, roles, release-gate]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

I'm logged in to codex and antigravity now too. Redesign my whole agt role table from scratch so every CLI I have gets used.
