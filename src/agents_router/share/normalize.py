"""How two copies of an item are compared.

Different files never look like edits only because of line endings, trailing
spaces or blank lines at the ends, and a description that a format wraps
differently still matches. The state keeps `NORM_VERSION` with each copy's
last-synced text: when these rules change, agt hashes again instead of writing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

NORM_VERSION = 1


def text(value: str | None) -> str:
    lines = [line.rstrip() for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def description(value: str | None) -> str:
    return " ".join(str(value or "").split())


def agent_core(name: str, description_text: str | None, body: str | None) -> dict[str, str]:
    return {"name": name, "description": description(description_text), "body": text(body)}


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
