"""Kiro agent files (`~/.kiro/agents/<name>.json`).

`update()` keeps unknown keys, their order and the file's indentation; only the
keys it is given change. A prompt that is a `file://` link points outside the
agent file, so agt never shares such an agent and never writes over it.
"""

from __future__ import annotations

import json
import re
from typing import Any

INDENT_RE = re.compile(r"^([ \t]+)\"", re.MULTILINE)


def read(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("a Kiro agent file must hold a JSON object")
    return data


def indent_of(text: str) -> int | str:
    match = INDENT_RE.search(text)
    if not match:
        return 2
    found = match.group(1)
    return found if "\t" in found else len(found)


def render(fields: dict[str, Any], indent: int | str = 2) -> str:
    return json.dumps({key: value for key, value in fields.items() if value is not None},
                      indent=indent, ensure_ascii=False) + "\n"


def update(text: str, changes: dict[str, Any]) -> str:
    data = read(text)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    result = json.dumps(data, indent=indent_of(text), ensure_ascii=False)
    newline = "\r\n" if "\r\n" in text else "\n"
    if newline != "\n":
        result = result.replace("\n", newline)
    return result + (newline if text.endswith(("\n", "\r")) else "")


def is_file_prompt(data: dict[str, Any]) -> bool:
    return str(data.get("prompt") or "").strip().lower().startswith("file://")
