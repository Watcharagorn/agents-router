#!/usr/bin/env bash
set -eu
cp -R "$(dirname "$0")/files/." .
python3 "$(dirname "$0")/../../make_sandbox.py" jobs
# the stand-in agt-reviewer goes where agt puts it for a real profile: the child's config folder, agents/
bash "$(dirname "$0")/../../install-profile-agents.sh" "$(dirname "$0")/profile-agents"
