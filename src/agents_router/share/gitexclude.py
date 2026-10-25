"""Keep agt's copies out of `git status` when a CLI folder is a git repo.

`~/.claude` is often one. The copies and links agt makes there are listed in
that repo's own exclude file (`git rev-parse --git-path info/exclude`), which
is never committed. Only paths agt made go in, inside one marked block, and
every other line of the file stays as it is. Paths are anchored at the repo's
top level and have no trailing slash, because git sees a linked skill folder
as a file.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from agents_router import fsutil

BEGIN = "# >>> agents-router share: copies agt made; agt keeps these lines up to date >>>"
END = "# <<< agents-router share <<<"


def repo_of(path: Path) -> tuple[Path, Path] | None:
    """(top level, exclude file) of the git work tree holding `path`, or None outside one."""

    folder = path.parent
    while not folder.is_dir() and folder.parent != folder:
        folder = folder.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--show-toplevel", "--git-path", "info/exclude"],
            capture_output=True, text=True, timeout=10, check=False, **fsutil.no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return None
    top = Path(lines[0])
    exclude = Path(lines[1])
    return top, exclude if exclude.is_absolute() else Path(os.path.normpath(folder / exclude))


def entry(top: Path, path: Path) -> str | None:
    """`/skills/name`: the path from the top level, through the real parent folder."""

    try:
        relative = (Path(os.path.realpath(path.parent)) / path.name).relative_to(Path(os.path.realpath(top)))
    except ValueError:
        return None
    return "/" + relative.as_posix()


def _split(text: str) -> tuple[list[str], list[str], list[str]]:
    lines = text.splitlines()
    if BEGIN in lines and END in lines[lines.index(BEGIN):]:
        start = lines.index(BEGIN)
        end = lines.index(END, start)
        return lines[:start], lines[start + 1:end], lines[end + 1:]
    return lines, [], []


def update(paths: list[Path], *, remove: bool = False) -> list[tuple[Path, list[str]]]:
    """Add (or remove) these paths in their repos' exclude blocks. Returns (exclude file, lines changed)."""

    by_file: dict[Path, tuple[Path, list[str]]] = {}
    for path in paths:
        found = repo_of(path)
        if not found:
            continue
        top, exclude = found
        line = entry(top, path)
        if line:
            by_file.setdefault(exclude, (top, []))[1].append(line)
    changed = []
    for exclude, (_top, lines) in by_file.items():
        try:
            text = exclude.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        before, block, after = _split(text)
        wanted = [line for line in block if line not in lines] if remove else block + [line for line in lines if line not in block]
        if wanted == block:
            continue
        body = before + ([BEGIN, *sorted(dict.fromkeys(wanted)), END] if wanted else []) + after
        exclude.parent.mkdir(parents=True, exist_ok=True)
        fsutil.write_preserving(exclude, ("\n".join(body) + "\n").encode("utf-8"), new=not exclude.exists())
        changed.append((exclude, sorted(set(wanted) ^ set(block))))
    return changed
