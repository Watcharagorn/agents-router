---
# Isolated run: place-files.sh builds .agt-eval/ in the workspace (its own HOME, AGT_HOME, CLAUDE_CONFIG_DIR,
# CODEX_HOME, fake CLIs, agt copied from this clone). Run with tests/skill-evals/bin first on PATH and --scaffold;
# see .claude/skills/agt-maintainer/references/release.md. No real job and no real ~/.agt or ~/.claude.
tags: [orchestrator-customize, near-miss, release-gate]
max_turns: 25
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

put codex first on the builder
