"""`python -m agents_router.agy_review hook` runs the PreToolUse hook (stdin JSON in, decision out)."""

import sys

from .hook import main

if __name__ == "__main__":
    if sys.argv[1:2] not in (["hook"], []):
        sys.stderr.write("usage: python -m agents_router.agy_review hook  (reads agy's PreToolUse JSON on stdin)\n")
        sys.exit(2)
    sys.exit(main())
