"""Item identity: an item is its file or folder name, normalized.

Names are compared in Unicode NFC, case-folded, because APFS ignores case. A
name that only some CLIs accept goes only to those; two names that collide
after case-folding are refused, and both paths are reported.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
import re
import unicodedata

SHARED_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 64
# Each CLI's own rule for agent and skill names. None of them is known to accept
# more than the shared rule, so they all use it until one is verified.
ACCEPTS = {cli: SHARED_NAME_RE for cli in ("claude", "codex", "antigravity", "kiro")}


def identity(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def problem(name: str, cli: str | None = None) -> str | None:
    """Why this name can't be shared (to `cli`, or everywhere), or None."""

    if unicodedata.normalize("NFC", name) != name:
        return "the name isn't in Unicode NFC form"
    if len(name) > MAX_NAME:
        return f"the name is longer than {MAX_NAME} characters"
    pattern = ACCEPTS.get(cli, SHARED_NAME_RE) if cli else SHARED_NAME_RE
    if not pattern.match(name):
        return "names must be lowercase letters, digits and single hyphens"
    return None


def collisions(names: Iterable[str]) -> list[list[str]]:
    """Groups of different names that are the same item after NFC and case-folding."""

    groups: dict[str, set[str]] = defaultdict(set)
    for name in names:
        groups[identity(name)].add(name)
    return [sorted(group) for group in groups.values() if len(group) > 1]
