# Installing Agents Router (for AI agents)

Agents Router (`agt`) keeps agents, skills, MCP servers and instruction files in sync between Claude Code, Codex CLI, Kiro CLI and Antigravity, and runs subagents in those CLIs on your own accounts. It is an MCP server with a plugin for Claude Code, Codex and Antigravity. It needs Python 3.11 or newer; its one dependency, rich (for color in a terminal), installs with it.

1. Install the `agt` command (it needs git, and uv or pipx):

   ```bash
   uv tool install --managed-python git+https://github.com/Watcharagorn/agents-router
   ```

   Without uv: `pipx install git+https://github.com/Watcharagorn/agents-router`. Agents Router isn't on PyPI; don't `pip install agt`, which is a different project.

2. Run `agt guide setup` and follow it. It prints the steps that match the installed version:
   - one combined question for the person;
   - the plugin in every CLI and Claude Code profile;
   - reaching a profile the person names;
   - what's left for them;
   - how to check, repair or uninstall.

   If `agt` isn't on PATH yet, call it by its full path (`~/.local/bin/agt`).

If you can't run commands, read [skills/agents-router-setup/SKILL.md](skills/agents-router-setup/SKILL.md): it carries the same guide. For an MCP client without a plugin system (Cline, Cursor and others), section 9 of the guide covers the server entry and the skill.
