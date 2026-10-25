"""Stand-ins for launchctl and osascript, so no test reaches the real launchd or posts a real notification.

`fake_launchd.py launchctl <args>` records its arguments in $FAKE_LAUNCHCTL_RECORD and keeps which labels
are loaded in $FAKE_LAUNCHCTL_STATE: `bootstrap gui/<uid> <plist>` loads the plist's label, `bootout
gui/<uid>/<label>` unloads it, and `print gui/<uid>/<label>` exits 0 only while it's loaded. With
FAKE_LAUNCHCTL_FAIL=bootstrap, bootstrap fails. `fake_launchd.py osascript <args>` records in
$FAKE_OSASCRIPT_RECORD.
"""

import json
import os
import plistlib
import sys


def record(variable: str, argv: list[str]) -> None:
    path = os.environ.get(variable)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")


def main() -> int:
    kind, argv = sys.argv[1], sys.argv[2:]
    if kind == "osascript":
        record("FAKE_OSASCRIPT_RECORD", argv)
        return 0
    record("FAKE_LAUNCHCTL_RECORD", argv)
    state_path = os.environ.get("FAKE_LAUNCHCTL_STATE")
    loaded = []
    if state_path and os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    action = argv[0] if argv else ""
    code = 0
    if action == "bootstrap":
        if os.environ.get("FAKE_LAUNCHCTL_FAIL") == "bootstrap":
            print("Bootstrap failed: 5: Input/output error", file=sys.stderr)
            return 5
        with open(argv[2], "rb") as handle:
            label = plistlib.load(handle)["Label"]
        if label not in loaded:
            loaded.append(label)
    elif action == "bootout":
        label = argv[1].rpartition("/")[2]
        if label in loaded:
            loaded.remove(label)
        else:
            code = 3
    elif action == "print":
        code = 0 if argv[1].rpartition("/")[2] in loaded else 113
    if state_path:
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(loaded, handle)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
