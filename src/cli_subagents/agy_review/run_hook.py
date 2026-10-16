"""Hook entry point run by agy as a plain script: `<python> -I <path>/run_hook.py`.

It puts the folder above its own package on sys.path (and drops its own folder,
so nothing here can shadow another module), then runs the hook. Works whether the
package lives inside cli_subagents or on its own. Prints a denial if even that fails.
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != here]
    root, parts = here, []
    while (root / "__init__.py").is_file():
        parts.insert(0, root.name)
        root = root.parent
    sys.path.insert(0, str(root))
    try:
        import importlib

        hook = importlib.import_module(".".join(parts) + ".hook")
        code = hook.main()
    except BaseException:
        sys.stdout.write('{"decision": "deny", "reason": "CLI Subagents review: the gate could not start, so it fails closed."}')
        code = 0
    sys.exit(0)
