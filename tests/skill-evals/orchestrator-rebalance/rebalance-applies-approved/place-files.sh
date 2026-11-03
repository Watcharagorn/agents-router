#!/usr/bin/env bash
set -eu
cp -R "$(dirname "$0")/files/." .
python3 "$(dirname "$0")/../../make_sandbox.py" jobs
# claude-work has 8% of its 5-hour window left, so the rebalance has something to move; written now, so it never goes stale
AGT_HOME="$PWD/.agt-eval/home/.agents-router" PYTHONPATH="$PWD/.agt-eval/src" python3 -c 'import time; from agents_router import accounts; accounts.update_usage("claude", "claude-work", windows={"five_hour": {"utilization": 0.92, "resets_at": time.time() + 4 * 3600}})'
