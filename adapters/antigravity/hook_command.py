"""Build Antigravity hook commands without fragile Windows shell quoting."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def python_command_prefix() -> list[str]:
    """Return a Python command prefix safe for Antigravity's Windows hook runner.

    Antigravity CLI 1.1.5 passes quoted hook executables to cmd.exe with the
    quote characters escaped. When the current interpreter lives below
    ``Program Files``, use the standard Windows Python launcher instead. The
    installed bridge venv path has no spaces and therefore uses its own Python.
    """

    executable = str(Path(sys.executable).resolve())
    if os.name == "nt" and any(char.isspace() for char in executable):
        launcher = Path(os.environ.get("WINDIR", r"C:\Windows")) / "py.exe"
        if launcher.is_file():
            return [str(launcher), f"-{sys.version_info.major}.{sys.version_info.minor}"]
    return [executable]


def hook_command(argv: list[str]) -> str:
    """Return a command string, rejecting arguments the hook runner mangles."""

    for argument in argv:
        if any(char.isspace() for char in argument) or '"' in argument:
            raise ValueError(
                "Antigravity hook command arguments must be whitespace-free; "
                "put variable paths in the encoded hook configuration instead"
            )
    return subprocess.list2cmdline(argv)
