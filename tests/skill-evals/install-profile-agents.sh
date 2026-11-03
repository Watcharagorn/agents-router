#!/usr/bin/env bash
# install-profile-agents.sh DIR: copy DIR/*.md into the eval child's own Claude Code config folder, `agents/`.
#
# In production agt writes `<profile>/agents/agt-<role>.md`, a user-scope agent the session calls by its bare
# name (`agt-reviewer`). An eval run loads no `.claude/` folder from the workspace, and a test plugin's agent
# would be `<plugin>:agt-reviewer`, so a case that needs the bare name puts its stand-in in the child's config
# folder, the same place a real profile keeps it.
#
# Not a documented `claude plugin eval` mechanism: it relies on the run layout of Claude Code 2.1.316, where the
# scaffold gets HOME=<run>/home, TMPDIR=<run>/tmp and the workspace <run>/home/cwd, and the child's config folder
# <run>/config already exists (with its .claude.json) when the scaffold runs. If that layout changes, this exits 1
# ("scaffold failed", the run scores 0) instead of writing anywhere else.
set -eu
src=$1
fail() { echo "install-profile-agents: $* (the eval's run layout changed; see release.md, skill gate)" >&2; exit 1; }
home=$(cd "$HOME" && pwd -P)
run=$(dirname "$home")
config="$run/config"
[ "$(basename "$home")" = home ] || fail "HOME ($home) is not <run>/home"
[ -n "${TMPDIR:-}" ] && [ "$(cd "$TMPDIR" && pwd -P)" = "$run/tmp" ] || fail "TMPDIR (${TMPDIR:-unset}) is not $run/tmp"
case "$(pwd -P)/" in "$home"/*) ;; *) fail "workspace $(pwd -P) is not inside $home" ;; esac
[ -d "$config" ] && [ -f "$config/.claude.json" ] || fail "no child config folder at $config"
[ -d "$config/agents" ] || mkdir "$config/agents"
for file in "$src"/*.md; do
  [ -f "$file" ] || fail "no agent files in $src"
  cp "$file" "$config/agents/"
done
echo "install-profile-agents: $config/agents: $(cd "$config/agents" && ls | tr '\n' ' ')"
