"""Codex custom agent files (`~/.codex/agents/<name>.toml`).

Reading uses tomllib. `update()` replaces only the top-level keys it is given
and keeps every other line, comment and table; a key that isn't there yet goes
after the last top-level key. Strings are written as basic strings, so
backslashes and quotes are always escaped.
"""

from __future__ import annotations

import re
import tomllib
from typing import Any

KEY_RE = re.compile(r"""^[ \t]*([A-Za-z0-9_-]+|"[^"\n]*"|'[^'\n]*')[ \t]*=[ \t]*(.*)$""")
TABLE_RE = re.compile(r"^[ \t]*\[")


def read(text: str) -> dict[str, Any]:
    return tomllib.loads(text)


def _escape(text: str, *, multiline: bool) -> str:
    out = []
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n" and multiline:
            out.append("\n")
        elif char == "\t":
            out.append("\t")
        elif char == "\n":
            out.append("\\n")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render_value(item) for item in value) + "]"
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in text:
        # The newline right after the opening quotes isn't part of the value.
        return '"""\n' + _escape(text, multiline=True) + '"""'
    return '"' + _escape(text, multiline=False) + '"'


def render(fields: dict[str, Any], header: str | None = None) -> str:
    lines = [f"# {line}" if line else "#" for line in (header.splitlines() if header else [])]
    lines += [f"{key} = {render_value(value)}" for key, value in fields.items() if value is not None]
    return "\n".join(lines) + "\n"


def _closes(segment: str, delimiter: str) -> bool:
    position = segment.find(delimiter)
    while position != -1:
        if delimiter == "'''":
            return True
        backslashes = len(segment[:position]) - len(segment[:position].rstrip("\\"))
        if backslashes % 2 == 0:
            return True
        position = segment.find(delimiter, position + 1)
    return False


def top_level_spans(lines: list[str]) -> tuple[dict[str, tuple[int, int]], int]:
    """Top-level `key = value` spans as [start, end) line ranges, and where the first table starts."""

    spans: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if TABLE_RE.match(line):
            return spans, index
        match = KEY_RE.match(line)
        if not match:
            index += 1
            continue
        key = match.group(1).strip("\"'")
        rest = match.group(2)
        end = index + 1
        for delimiter in ('"""', "'''"):
            if rest.startswith(delimiter):
                if not _closes(rest[3:], delimiter):
                    while end < len(lines) and not _closes(lines[end], delimiter):
                        end += 1
                    if end == len(lines):
                        raise ValueError(f"line {index + 1}: `{key}` has no closing {delimiter}")
                    end += 1
                break
        else:
            if rest.lstrip().startswith("[") and rest.count("[") > rest.count("]"):
                depth = rest.count("[") - rest.count("]")
                while end < len(lines) and depth > 0:
                    depth += lines[end].count("[") - lines[end].count("]")
                    end += 1
        spans[key] = (index, end)
        index = end
    return spans, len(lines)


def update(text: str, changes: dict[str, Any]) -> str:
    """Change only these top-level keys; None removes one. The result is checked with tomllib."""

    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r")) or not text
    lines = text.replace("\r\n", "\n").split("\n")
    if trailing:
        lines.pop()
    spans, first_table = top_level_spans(lines)
    starts = {start: (key, end) for key, (start, end) in spans.items()}
    out: list[str] = []
    after_keys = table_at = None  # positions in `out`
    index = 0
    while index < len(lines):
        if index == first_table:
            table_at = len(out)
        if index in starts:
            key, end = starts[index]
            if key not in changes:
                out += lines[index:end]
            elif changes[key] is not None:
                out += _assignment(key, changes[key])
            after_keys = len(out)
            index = end
            continue
        out.append(lines[index])
        index += 1
    added = [line for key, value in changes.items() if key not in spans and value is not None
             for line in _assignment(key, value)]
    if added:
        if after_keys is not None:
            position = after_keys
        elif table_at is not None:
            position = table_at
            added.append("")  # keep a blank line before the first table
        else:
            position = len(out)
        out[position:position] = added
    result = newline.join(out) + (newline if trailing else "")
    tomllib.loads(result)  # never hand back a file Codex can't read
    return result


def _assignment(key: str, value: Any) -> list[str]:
    return f"{key} = {render_value(value)}".split("\n")
