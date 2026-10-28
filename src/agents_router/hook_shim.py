"""Entry point for hooks a worker runs: `python <path>/hook_shim.py --home <dir> hook inbox <job_id>`.

Run as a plain script, so it puts its own package root on sys.path and needs
no PYTHONPATH or installed `agt`. Any failure ends with exit code 0: a hook
must never fail the worker's tool call.
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    # Python put this file's own folder first; replace it so modules here can't shadow others.
    here = Path(__file__).resolve().parent
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != here]
    sys.path.insert(0, str(here.parent))
    try:
        from agents_router.entry import main

        code = main(sys.argv[1:])
    except BaseException:
        code = 0
    sys.exit(code if code in (0, 2) else 0)
