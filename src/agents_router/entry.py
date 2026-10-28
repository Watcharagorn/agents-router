"""`agt`'s entry point. The commands hosts run all the time start without loading the whole CLI.

Claude Code runs `agt hook …` on its events, an agent CLI starts `agt mcp`, and each job runs
`agt _runner`. Importing every other command first would add tens of milliseconds to each of
them. Any other command, or one of these with flags, goes to `cli.main` as before.
"""

from __future__ import annotations

import os
import sys

HOOK_ACTIONS = ("await", "pending", "inbox")


def quick(argv: list[str]) -> tuple[str | None, str, list[str]] | None:
    """(AGT_HOME, command, arguments) when argv is one of the plain forms hosts run, else None."""

    home = None
    if argv[:1] == ["--home"] and len(argv) > 2:
        home, argv = argv[1], argv[2:]
    if any(arg.startswith("-") for arg in argv[1:]) and argv[:2] != ["mcp", "--worker"]:
        return None
    if argv[:1] == ["hook"] and len(argv) in (2, 3) and argv[1] in HOOK_ACTIONS:
        return home, "hook", argv[1:]
    if argv == ["mcp"]:
        return home, "mcp", []
    if argv[:2] == ["mcp", "--worker"] and len(argv) == 3 and not argv[2].startswith("-"):
        return home, "mcp", [argv[2]]
    if argv[:1] == ["_runner"] and len(argv) == 2:
        return home, "_runner", argv[1:]
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    found = quick(argv)
    if found is None:
        from agents_router.cli import main as full

        return full(argv)
    home, command, rest = found
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if home:
        os.environ["AGT_HOME"] = home
    if command == "hook":
        from agents_router.hooks import main as run_hook

        return run_hook(*rest)
    from agents_router import tools

    try:
        if command == "mcp":
            from agents_router.mcp_server import main as serve

            return serve(rest[0] if rest else None)
        from agents_router.runner import main as run_job

        return run_job(rest[0])
    except (ValueError, RuntimeError, tools.ToolError) as exc:
        print(f"agt: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
