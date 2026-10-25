"""A small YAML subset for the front matter of agent and skill files.

It covers what Claude Code, Antigravity and skill files use: `key: value`
scalars (plain, single- or double-quoted), flow lists (`[a, b]`), block lists
(`- a`) and `|` / `>` block scalars. Values stay strings: `false` and `200`
are read as text, which is all the comparison needs. `update()` rewrites only
the keys it is given, so every other line, comment and key keeps its bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

FENCE = "---"
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t]+(.*)|[ \t]*)$")
BLOCK_INDICATORS = ("|", "|-", "|+", ">", ">-", ">+")
PLAIN_FIRST = set("-?:,[]{}#&*!|>'\"%@`")
SPECIAL_WORDS = {"", "~", "null", "true", "false", "yes", "no", "on", "off"}
NUMBER_RE = re.compile(r"^[-+]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?$|^0[xob][0-9a-fA-F_]+$")
ESCAPES = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0", " ": " ", "b": "\b", "f": "\f"}


class FrontmatterError(ValueError):
    """Front matter that can't be read. `line` is 1-based, counting the opening fence."""

    def __init__(self, message: str, line: int):
        super().__init__(f"line {line}: {message}")
        self.line = line


@dataclass
class Document:
    fields: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    lines: list[str] = field(default_factory=list)  # front matter lines, fences left out
    spans: dict[str, tuple[int, int]] = field(default_factory=dict)  # key -> [start, end) in lines
    newline: str = "\n"
    has_frontmatter: bool = False


def parse(text: str) -> Document:
    newline = "\r\n" if "\r\n" in text else "\n"
    flat = text.replace("\r\n", "\n")
    if not (flat.startswith(FENCE + "\n") or flat.rstrip(" \t") == FENCE):
        return Document(body=text, newline=newline)
    lines = flat.split("\n")
    close = next((index for index in range(1, len(lines)) if lines[index].rstrip(" \t") == FENCE), None)
    if close is None:
        raise FrontmatterError("the front matter has no closing ---", 1)
    head = lines[1:close]
    rest = lines[close + 1:]
    body = "\n".join(rest)
    if newline != "\n":
        body = body.replace("\n", newline)
    fields, spans = _parse_lines(head)
    return Document(fields=fields, body=body, lines=head, spans=spans, newline=newline, has_frontmatter=True)


def _parse_lines(lines: list[str]) -> tuple[dict[str, Any], dict[str, tuple[int, int]]]:
    fields: dict[str, Any] = {}
    spans: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        number = index + 2  # 1-based, after the opening fence
        if line[0] in " \t":
            raise FrontmatterError("unexpected indentation", number)
        match = KEY_RE.match(line)
        if not match:
            raise FrontmatterError("expected `key: value`", number)
        key, rest = match.group(1), _strip_comment(match.group(2) or "")
        if key in fields:
            raise FrontmatterError(f"`{key}` appears twice", number)
        start = index
        index += 1
        if rest in BLOCK_INDICATORS:
            end = _indented_end(lines, index)
            value: Any = _block_scalar(lines[index:end], rest)
            index = end
        elif rest == "":
            end = index
            while end < len(lines) and (not lines[end].strip() or lines[end].lstrip().startswith("-")
                                        or (lines[end][:1] in " \t" and lines[end].strip())):
                end += 1
            end = _trim_blank(lines, index, end)
            items = [item for item in lines[index:end] if item.strip()]
            if items and all(item.lstrip().startswith("-") for item in items):
                value = [_scalar(item.lstrip()[1:].strip(), number) for item in items]
            elif items:
                value = _fold([rest, *(item.strip() for item in items)])
            else:
                value = ""
            index = end
        else:
            end = _continuation_end(lines, index, rest)
            value = _scalar(_fold([rest, *(item.strip() for item in lines[index:end])]), number)
            index = end
        fields[key] = value
        spans[key] = (start, index)
    return fields, spans


def _indented_end(lines: list[str], index: int) -> int:
    end = index
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in " \t"):
        end += 1
    return _trim_blank(lines, index, end)


def _trim_blank(lines: list[str], start: int, end: int) -> int:
    while end > start and not lines[end - 1].strip():
        end -= 1
    return end


def _continuation_end(lines: list[str], index: int, first: str) -> int:
    """Indented lines that continue a scalar (a long description wrapped by an editor)."""

    if first.startswith("[") and first.rstrip().endswith("]"):
        return index
    if first and first[0] in "\"'" and _quoted_closed(first):
        return index
    end = index
    while end < len(lines) and lines[end][:1] in " \t" and lines[end].strip() and not lines[end].lstrip().startswith("#"):
        end += 1
    return end


def _quoted_closed(text: str) -> bool:
    quote = text[0]
    position = 1
    while position < len(text):
        char = text[position]
        if quote == '"' and char == "\\":
            position += 2
            continue
        if char == quote:
            if quote == "'" and text[position + 1:position + 2] == "'":
                position += 2
                continue
            return True
        position += 1
    return False


def _fold(parts: list[str]) -> str:
    return " ".join(part for part in parts if part)


def _strip_comment(text: str) -> str:
    text = text.strip()
    if text and text[0] in "\"'":
        quote = text[0]
        position = 1
        while position < len(text):
            char = text[position]
            if quote == '"' and char == "\\":
                position += 2
                continue
            if char == quote:
                if quote == "'" and text[position + 1:position + 2] == "'":
                    position += 2
                    continue
                tail = text[position + 1:].strip()
                return text[:position + 1] if not tail or tail.startswith("#") else text
            position += 1
        return text
    match = re.search(r"\s#", text)
    return text[:match.start()].rstrip() if match else text


def _scalar(text: str, number: int) -> Any:
    text = text.strip()
    if text.startswith('"'):
        return _double_quoted(text, number)
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            raise FrontmatterError("unclosed quote", number)
        return text[1:-1].replace("''", "'")
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(item, number) for item in _split_flow(text[1:-1], number)]
    return text


def _double_quoted(text: str, number: int) -> str:
    out = []
    position = 1
    while position < len(text):
        char = text[position]
        if char == "\\":
            code = text[position + 1:position + 2]
            if code in ESCAPES:
                out.append(ESCAPES[code])
                position += 2
                continue
            sizes = {"x": 2, "u": 4, "U": 8}
            if code in sizes:
                digits = text[position + 2:position + 2 + sizes[code]]
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    raise FrontmatterError(f"bad escape \\{code}{digits}", number) from None
                position += 2 + sizes[code]
                continue
            raise FrontmatterError(f"bad escape \\{code}", number)
        if char == '"':
            if text[position + 1:].strip():
                raise FrontmatterError("text after the closing quote", number)
            return "".join(out)
        out.append(char)
        position += 1
    raise FrontmatterError("unclosed quote", number)


def _split_flow(text: str, number: int) -> list[str]:
    items, current, quote, position = [], [], None, 0
    while position < len(text):
        char = text[position]
        if quote:
            current.append(char)
            if quote == '"' and char == "\\":
                current.append(text[position + 1:position + 2])
                position += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'" and not "".join(current).strip():
            quote = char  # a quote opens an item only at its start
            current.append(char)
        elif char == ",":
            items.append("".join(current).strip())
            current = []
        elif char in "[]{}":
            raise FrontmatterError("nested lists are not supported", number)
        else:
            current.append(char)
        position += 1
    if quote:
        raise FrontmatterError("unclosed quote", number)
    last = "".join(current).strip()
    if last or items:
        items.append(last)
    return [item for item in items if item]


def _block_scalar(lines: list[str], indicator: str) -> str:
    present = [line for line in lines if line.strip()]
    indent = min((len(line) - len(line.lstrip(" \t")) for line in present), default=0)
    content = [line[indent:] if line.strip() else "" for line in lines]
    if indicator.startswith("|"):
        text = "\n".join(content)
    else:
        paragraphs, current = [], []
        for line in content:
            if line:
                current.append(line)
            else:
                paragraphs.append(" ".join(current))
                current = []
        paragraphs.append(" ".join(current))
        text = "\n".join(paragraphs)
    text = text.rstrip("\n")
    if indicator.endswith("-"):
        return text
    return text + "\n" if text else text


# Writing -----------------------------------------------------------------


def render_scalar(value: str, *, in_flow: bool = False) -> str:
    if _plain_ok(value, in_flow):
        return value
    out = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _plain_ok(value: str, in_flow: bool) -> bool:
    if value != value.strip() or value.lower() in SPECIAL_WORDS or NUMBER_RE.match(value):
        return False
    if value[0] in PLAIN_FIRST or ": " in value or " #" in value or value.endswith(":"):
        return False
    if in_flow and any(char in value for char in ",[]{}"):
        return False
    return not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def render_entry(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, (list, tuple)):
        return f"{key}: [" + ", ".join(render_scalar(str(item), in_flow=True) for item in value) + "]"
    return f"{key}: {render_scalar(str(value))}"


def render(fields: dict[str, Any], body: str) -> str:
    """A new file: front matter with these keys in order, then the body."""

    head = [render_entry(key, value) for key, value in fields.items() if value is not None]
    return "\n".join([FENCE, *head, FENCE]) + "\n" + body


def update(text: str, changes: dict[str, Any], body: str | None = None) -> str:
    """Change only these keys (None removes one) and, if given, the body."""

    document = parse(text)
    if not document.has_frontmatter:
        new_body = document.body if body is None else body
        return render({key: value for key, value in changes.items() if value is not None}, new_body)
    lines = list(document.lines)
    for key, (start, end) in sorted(((k, document.spans[k]) for k in changes if k in document.spans),
                                    key=lambda item: item[1][0], reverse=True):
        value = changes[key]
        lines[start:end] = [] if value is None else [render_entry(key, value)]
    for key, value in changes.items():
        if key not in document.spans and value is not None:
            lines.append(render_entry(key, value))
    newline = document.newline
    head = newline.join([FENCE, *lines, FENCE])
    return head + newline + (document.body if body is None else body)
