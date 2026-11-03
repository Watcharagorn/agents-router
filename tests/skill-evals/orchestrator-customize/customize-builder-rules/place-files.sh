#!/usr/bin/env bash
set -eu
cp -R "$(dirname "$0")/files/." .
python3 "$(dirname "$0")/../../make_sandbox.py" jobs
# Claude Code refuses edits inside a folder named .claude in an eval's don't-ask mode, so this case's
# Claude profile lives in claude-profile and the sandbox's CLAUDE_CONFIG_DIR points there.
python3 - <<'PY'
import json
from pathlib import Path
root = Path(".agt-eval")
home = root / "home"
(home / ".claude").rename(home / "claude-profile")
spec = json.loads((root / "env.json").read_text(encoding="utf-8"))
spec["env"]["CLAUDE_CONFIG_DIR"] = str((home / "claude-profile").resolve())
(root / "env.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
PY
