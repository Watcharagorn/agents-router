"""Checkbox lists for agt share in a terminal: a list of choices, and the items × folders grid.

Keys: ↑/↓ or j/k move, ←/→ or h/l move between folders in the grid, space
ticks or unticks, `a` ticks all (the whole row in the grid), `n` clears,
Enter confirms, and `q` or Esc leaves without changing anything. Rows agt
won't use are greyed out with the reason, never hidden, and the cursor skips
them. Where the terminal can't send single keys (`TERM=dumb`), the same list
comes numbered and you type your answers.

Each frame looks like agt's other output: the plan table's kind groups and
footnotes, the cell under the cursor in reverse video, a count that names each
change, and the keys with what Enter and q do on this screen. It's drawn with
`ui.sgr`, fitted to the terminal so a redraw never scrolls, and cleared when the
picker closes, so what comes next says what happened.

Only people get these: commands check for an agent first (`commands._interactive`)
and give it `needs_choice` instead. The widgets are plain state (`press` a key,
read its `lines`), so they're tested without a terminal; `Terminal` is the only
part that touches one, and it always puts the terminal back as it was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import shutil
import sys
from typing import Callable, Iterator

from agents_router import ui

ON, OFF, FIXED, BLOCKED = "on", "off", "fixed", "blocked"
DONE, QUIT = "done", "quit"
MARKS = {ON: "[x]", OFF: "[ ]", FIXED: " · ", BLOCKED: " - "}
# The legend, in this order, lists only the marks a grid uses.
MEANINGS = [(ON, "there or goes there"), (OFF, "left out"), (FIXED, "stays as it is"), (BLOCKED, "can't be copied there")]
SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
NAME = 16  # an item's name is cut to no less than this before the grid's folders scroll


def _and(words: list[str]) -> str:
    return ", ".join(words[:-1]) + " and " + words[-1] if len(words) > 1 else words[0]


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def _cut(text: str, width: int) -> str:
    """`text` in at most `width` cells, ending in "…" when it was longer."""

    if ui.cell_len(text) <= width:
        return text
    cut = ""
    for char in text:
        if ui.cell_len(cut + char) > width - 1:
            break
        cut += char
    return cut + "…"


def _wrap(text: str, width: int) -> list[str]:
    """`text` in lines of at most `width` cells, broken between words; a `command` in backticks never breaks."""

    lines = [""]
    for word in re.findall(r"`[^`]*`\S*|\S+", text):
        if lines[-1] and ui.cell_len(f"{lines[-1]} {word}") > width:
            lines.append(word)
        else:
            lines[-1] = f"{lines[-1]} {word}" if lines[-1] else word
    return lines


def _title(title: ui.Line, width: int | None = None) -> ui.Line:
    """The screen's title, bold. When it doesn't fit, its muted part goes whole, never cut mid-word."""

    line = [ui.bold(title)] if isinstance(title, str) else list(title)
    if width is not None and ui.cell_len(ui.plain(line)) > width:
        line = [piece for piece in line if not (isinstance(piece, ui.Span) and piece.style == "muted")]
        while line and isinstance(line[-1], str) and not line[-1].strip():
            line.pop()
    return line


def _keys(pairs: list[tuple[str, str]], width: int, one: bool = False) -> list[ui.Line]:
    """The keys and what they do, wrapped at a pair onto more lines: how to finish and how to leave are the keys
    everyone needs, so they're never cut off. `one`: a terminal too short for more gets one line, the keys it can
    spare left out (n, a, the arrows, space)."""

    if one:
        order = [key for key in ("n", "a") if any(pair[0] == key for pair in pairs)] + [pairs[0][0], "space"]
        while len(pairs) > 2 and ui.cell_len("  " + "  ".join(f"{key} {action}" for key, action in pairs)) > width:
            pairs = [pair for pair in pairs if pair[0] != order[0]]
            order = order[1:]
    lines: list[list[ui.Piece]] = [["  "]]
    for key, action in pairs:
        pair = [ui.bold(key), " ", ui.muted(action)]
        if not one and len(lines[-1]) > 1 and ui.cell_len(ui.plain(lines[-1]) + "  " + key + " " + action) > width:
            lines.append(["  "])
        lines[-1] += (["  "] if len(lines[-1]) > 1 else []) + pair
    return lines


def _count(phrases: list[str], changed: int, width: int, joint: str, tail: str = "") -> ui.Line:
    """"1 change: explorer leaves kiro": as many of the changes as fit, then "and 2 more"."""

    if not changed:
        return ["  ", ui.muted("No changes yet")]
    head = f"{changed} change{'s' if changed != 1 else ''}"
    room = width - 2 - ui.cell_len(head) - 2 - ui.cell_len(tail)
    shown: list[str] = []
    for index, phrase in enumerate(phrases):
        rest = len(phrases) - index - 1
        if ui.cell_len(joint.join(shown + [phrase]) + (f"{joint}{rest} more" if rest else "")) > room:
            break
        shown.append(phrase)
    if not shown:  # not even one fits: the count alone
        return ["  ", ui.Span("changed", head)]
    text = joint.join(shown) + (f"{joint}{len(phrases) - len(shown)} more" if len(shown) < len(phrases) else "")
    return ["  ", ui.Span("changed", head), ": " + text, *([ui.muted(tail)] if tail else [])]


def _window(count: int, at: int, room: int) -> tuple[int, int, bool, bool]:
    """Which of `count` lines show in `room` lines around line `at`: (start, end, cut above, cut below). With room
    for it, a cut end gives up its line to "↑ 3 more", and line `at` always shows."""

    if count <= room:
        return 0, count, False, False
    start = max(0, min(at - room // 2, count - room))
    end = start + room
    up, down = start > 0 and room >= 3, end < count and room >= 3
    return start + up, end - down, up, down


def _frame(top: list[tuple[str, ui.Line]], body: list[tuple[ui.Line, bool]], at: int,
           bottom: list[tuple[str, ui.Line]], keys: list[tuple[str, str]], width: int, height: int) -> list[ui.Line]:
    """The whole frame in `height` - 1 lines: a frame as tall as the terminal scrolls it, and the redraw can't reach
    what scrolled away. The body's rows scroll around line `at`; while fewer than 3 rows would show, the lines tagged
    "notes" go first, then "legend", the blank ones and "title": the count and the keys always stay (on one line, with
    Enter and q, only when nothing else is left to give). `body` holds (line, is a row) for the "↑ 3 more" count."""

    budget = max(height - 1, 1)
    want = min(len(body), 5)  # three rows, and a line each for "↑ 3 more" and "↓ 5 more"
    dropped: set[str] = set()
    for tag in ("notes", "legend", "blank", "title", "keys", ""):
        last = _keys(keys, width, one="keys" in dropped)
        kept = [line for name, line in top if name not in dropped], [line for name, line in bottom if name not in dropped] + last
        room = budget - len(kept[0]) - len(kept[1])
        if room >= want or not tag:
            break
        dropped.add(tag)
    start, end, up, down = _window(len(body), at, max(room, 1))
    shown = [line for line, _ in body[start:end]]
    if up:
        shown.insert(0, ["    ", ui.muted(f"↑ {sum(row for _, row in body[:start])} more")])
    if down:
        shown.append(["    ", ui.muted(f"↓ {sum(row for _, row in body[end:])} more")])
    return [*kept[0], *shown, *kept[1]]


def _draw(frame: list[ui.Line], width: int, style: str) -> list[str]:
    """The frame as text for the terminal. `style` is `ui.mode`'s, or "bare" for --color never: plain words, but the
    cell under the cursor keeps reverse video, the one mark of the column you're in."""

    if style == "bare":
        frame = [[piece if isinstance(piece, str) else ui.Span("cursor", piece.text) if piece.style.startswith("cursor")
                  else ui.plain(piece) for piece in ([line] if isinstance(line, (str, ui.Span)) else line)] for line in frame]
        style = "mono"
    return [ui.sgr(line, style, width) for line in frame]


# A list of choices -------------------------------------------------------------


@dataclass
class Choice:
    key: str
    text: str
    checked: bool = False
    reason: str | None = None  # why agt won't use it: shown greyed out, and the cursor skips it
    fixed: str | None = None  # ticked, and changed elsewhere (shown greyed with this note; the cursor skips it)
    name: str | None = None  # what the count calls it ("kiro joins"); the text when not given

    @property
    def locked(self) -> bool:
        return self.reason is not None or self.fixed is not None


class ListState:
    """A list of choices. `verbs` name a tick and an untick in the count ("kiro joins", "kiro leaves"); `enter` and
    `leave` say what Enter and q do on this screen."""

    def __init__(self, title: ui.Line, choices: list[Choice], *, verbs: tuple[str, str] = ("on", "off"),
                 enter: str = "done", leave: str = "back"):
        self.title, self.choices, self.verbs, self.enter, self.leave = title, choices, verbs, enter, leave
        self.first = {choice.key: choice.checked for choice in choices}
        usable = self._usable()
        self.cursor = usable[0] if usable else 0

    def _usable(self) -> list[int]:
        return [index for index, choice in enumerate(self.choices) if not choice.locked]

    def press(self, key: str) -> str | None:
        usable = self._usable()
        if key in ("q", "esc"):
            return QUIT
        if key == "enter":
            return DONE
        if not usable:
            return None
        if key in ("up", "k", "down", "j"):
            step = -1 if key in ("up", "k") else 1
            position = usable.index(self.cursor) if self.cursor in usable else 0
            self.cursor = usable[max(0, min(len(usable) - 1, position + step))]
        elif key == "space" and self.cursor in usable:
            self.choices[self.cursor].checked = not self.choices[self.cursor].checked
        elif key in ("a", "n"):
            for index in usable:
                self.choices[index].checked = key == "a"
        return None

    def checked(self) -> set[str]:
        return {choice.key for choice in self.choices if (choice.checked or choice.fixed) and choice.reason is None}

    def changed(self) -> list[Choice]:
        return [choice for choice in self.choices if not choice.locked and choice.checked != self.first[choice.key]]

    def _mark(self, choice: Choice) -> str:
        return "[-]" if choice.reason else "[x]" if choice.checked or choice.fixed else "[ ]"

    def frame(self, width: int = 100, height: int = 40) -> list[ui.Line]:
        changed = self.changed()
        body: list[tuple[ui.Line, bool]] = []
        at = 0
        for index, choice in enumerate(self.choices):
            mark, note = self._mark(choice), choice.reason or choice.fixed
            if choice.locked:
                line: list[ui.Piece] = ["    ", ui.muted(f"{mark} {choice.text}")]
            else:
                style = "changed" if choice in changed else ""
                if index == self.cursor:
                    style = "cursor-changed" if style else "cursor"
                line = ["  ", ui.bold("> ") if index == self.cursor else "  ", ui.Span(style, mark) if style else mark,
                        " " + choice.text]
            if index == self.cursor:
                at = len(body)
            if note and ui.cell_len(ui.plain(line) + "  " + note) > width:  # under its row, never cut mid-command
                body += [(line, True), *((["        ", ui.muted(part)], False) for part in _wrap(note, max(width - 8, 12)))]
            else:
                body.append((line + (["  ", ui.muted(note)] if note else []), True))
        phrases = [f"{choice.name or choice.text} {self.verbs[0] if choice.checked else self.verbs[1]}" for choice in changed]
        keys = [("↑↓", "move"), ("space", "tick"), ("a", "all"), ("n", "none"), ("enter", self.enter), ("q", self.leave)]
        return _frame([("title", _title(self.title, width)), ("blank", "")], body, at,
                      [("blank", ""), ("", _count(phrases, len(changed), width, ", "))], keys, width, height)

    def lines(self, width: int = 100, height: int = 40, *, style: str = "color") -> list[str]:
        return _draw(self.frame(width, height), width, style)

    def fallback(self, read: Callable[[str], str], write: Callable[[str], None]) -> str:
        """The same list numbered, for a terminal without single keys."""

        while True:
            write(ui.plain(_title(self.title)))
            for number, choice in enumerate(self.choices, 1):
                note = choice.reason or choice.fixed
                write(f"  {number:>2}. {self._mark(choice)} {choice.text}" + (f"  {note}" if note else ""))
            try:
                answer = read(f"Numbers to tick or untick (1 3) · a all · n none · Enter {self.enter} · q {self.leave}: ")
            except EOFError:
                return QUIT
            answer = answer.strip().lower()
            if not answer:
                return DONE
            if answer == "q":
                return QUIT
            if answer in ("a", "n"):
                self.press(answer)
                continue
            for word in answer.replace(",", " ").split():
                if word.isdigit() and 1 <= int(word) <= len(self.choices) and not self.choices[int(word) - 1].locked:
                    self.choices[int(word) - 1].checked = not self.choices[int(word) - 1].checked


# The items × folders grid --------------------------------------------------------


@dataclass
class Cell:
    state: str  # ON, OFF, FIXED (there, and agt never changes it) or BLOCKED (it can't go there)
    note: str = ""
    there: bool = False  # the item is in that folder now, so unticking it removes agt's copy (with a backup)


@dataclass
class Row:
    key: str  # kind/name: rows of one kind come together, under the kind's name
    text: str
    cells: dict[str, Cell] = field(default_factory=dict)


def _get(row: Row, column: str) -> Cell:
    return row.cells.get(column) or Cell(BLOCKED, "not a folder agt shares")


class GridState:
    """Items × folders. `enter` and `leave` say what Enter and q do on this screen."""

    def __init__(self, title: ui.Line, columns: list[str], rows: list[Row], *, enter: str = "apply", leave: str = "quit"):
        self.title, self.columns, self.rows, self.enter, self.leave = title, columns, rows, enter, leave
        self.first = {(row.key, column): row.cells[column].state for row in rows for column in columns if column in row.cells}
        self.row, self.column = 0, 0
        self._settle()

    def _cell(self, row: int | None = None, column: int | None = None) -> Cell:
        return _get(self.rows[self.row if row is None else row], self.columns[self.column if column is None else column])

    def _toggleable(self, row: int, column: int) -> bool:
        return self._cell(row, column).state in (ON, OFF)

    def _settle(self) -> None:
        """Start on a cell you can change, when there is one."""

        for row in range(len(self.rows)):
            for column in range(len(self.columns)):
                if self._toggleable(row, column):
                    self.row, self.column = row, column
                    return

    def press(self, key: str) -> str | None:
        if key in ("q", "esc"):
            return QUIT
        if key == "enter":
            return DONE
        if not self.rows or not self.columns:
            return None
        if key in ("up", "k"):
            self.row = max(0, self.row - 1)
        elif key in ("down", "j"):
            self.row = min(len(self.rows) - 1, self.row + 1)
        elif key in ("left", "h"):
            self.column = max(0, self.column - 1)
        elif key in ("right", "l"):
            self.column = min(len(self.columns) - 1, self.column + 1)
        elif key == "space" and self._toggleable(self.row, self.column):
            cell = self._cell()
            cell.state = OFF if cell.state == ON else ON
        elif key in ("a", "n"):
            for column in range(len(self.columns)):
                if self._toggleable(self.row, column):
                    self._cell(self.row, column).state = ON if key == "a" else OFF
        return None

    def changes(self) -> dict[tuple[str, str], bool]:
        """(row key, column) -> ticked now, for the cells that differ from how they started."""

        found = {}
        for row in self.rows:
            for column, cell in row.cells.items():
                if cell.state in (ON, OFF) and self.first.get((row.key, column)) != cell.state:
                    found[(row.key, column)] = cell.state == ON
        return found

    def _notes(self) -> list[str]:
        """The "-" cells' notes, numbered as the plan table's footnotes; every other note shows on the cursor line."""

        notes: list[str] = []
        for row in self.rows:
            for column in self.columns:
                cell = _get(row, column)
                if cell.state == BLOCKED and cell.note and cell.note not in notes:
                    notes.append(cell.note)
        return notes

    def _mark(self, cell: Cell, notes: list[str]) -> str:
        if cell.state == BLOCKED and cell.note in notes:
            return " -" + str(notes.index(cell.note) + 1).translate(SUPERSCRIPT)
        return MARKS[cell.state]

    def _groups(self) -> list[tuple[str, list[int]]]:
        groups: list[tuple[str, list[int]]] = []
        for index, row in enumerate(self.rows):
            kind = row.key.partition("/")[0] if "/" in row.key else ""
            if not groups or groups[-1][0] != kind:
                groups.append((kind, []))
            groups[-1][1].append(index)
        return groups

    def _legend(self, notes: list[str]) -> tuple[ui.Line, list[ui.Line]]:
        used = {_get(row, column).state for row in self.rows for column in self.columns}
        legend: list[ui.Piece] = []
        for state, meaning in MEANINGS:
            if state in used:
                mark = MARKS[state].strip()
                legend += ["  ", ui.muted(mark) if state in (FIXED, BLOCKED) else mark, " ", ui.muted(meaning)]
        return legend, [["  ", ui.muted(str(number).translate(SUPERSCRIPT) + " " + _sentence(note))]
                        for number, note in enumerate(notes, 1)]

    def _phrases(self) -> tuple[list[str], bool]:
        """Each item's changes in words: "dana goes to codex and kiro", "explorer leaves kiro", "dana won't go to kiro"."""

        changes = self.changes()
        names = [row.text for row in self.rows if any(key == row.key for key, _ in changes)]
        phrases, leaves = [], False
        for row in self.rows:
            verbs: dict[str, list[str]] = {}
            for column in self.columns:
                if (row.key, column) in changes:
                    verb = "goes to" if changes[(row.key, column)] else "leaves" if row.cells[column].there else "won't go to"
                    verbs.setdefault(verb, []).append(column)
            for verb, where in verbs.items():  # two changed items of one name: "agents/dana", "skills/dana"
                phrases.append(f"{row.key if names.count(row.text) > 1 else row.text} {verb} {_and(where)}")
                leaves = leaves or verb == "leaves"
        return phrases, leaves

    def frame(self, width: int = 100, height: int = 40) -> list[ui.Line]:
        if not self.rows or not self.columns:
            return [_title(self.title, width)]
        notes = self._notes()
        marks = [[self._mark(_get(row, column), notes) for column in self.columns] for row in self.rows]
        widths = [max([ui.cell_len(column)] + [ui.cell_len(row[index]) for row in marks]) + 2
                  for index, column in enumerate(self.columns)]
        names = max([ui.cell_len(row.text) for row in self.rows] + [8])

        def used(first: int, last: int, name: int) -> int:  # the cells a row takes, with "2 more ›" when it's cut
            more = len(self.columns) - last
            return 4 + name + 2 + sum(widths[first:last]) - 2 + (2 + len(f"{more} more ›") if more else 0)

        if used(0, len(self.columns), names) > width:
            names = max(min(names, NAME), names - (used(0, len(self.columns), names) - width))
        first = next((first for first in range(self.column + 1) if used(first, self.column + 1, names) <= width),
                     self.column)  # the folders scroll so the cursor's shows
        last = max((last for last in range(self.column + 1, len(self.columns) + 1) if used(first, last, names) <= width),
                   default=self.column + 1)
        shown = range(first, last)
        header: list[ui.Piece] = ["    ", ui.muted(f"‹ {first} more".ljust(names + 2)) if first else " " * (names + 2)]
        for index in shown:
            header += [ui.bold(self.columns[index]), " " * (widths[index] - ui.cell_len(self.columns[index]))]
        header = header[:-1] + (["  ", ui.muted(f"{len(self.columns) - last} more ›")] if last < len(self.columns) else [])
        changes = self.changes()
        body: list[tuple[ui.Line, bool]] = []
        at = 0
        for kind, indexes in self._groups():
            if kind:
                body.append((["  ", ui.bold(kind.capitalize())], False))
            for index in indexes:
                row = self.rows[index]
                name = _cut(row.text, names)
                line: list[ui.Piece] = ["  ", ui.bold("> ") if index == self.row else "  ", name,
                                        " " * (names - ui.cell_len(name) + 2)]
                for column in shown:
                    cell, mark = _get(row, self.columns[column]), marks[index][column]
                    style = "changed" if (row.key, self.columns[column]) in changes else \
                        "muted" if cell.state in (FIXED, BLOCKED) else ""
                    if index == self.row and column == self.column:
                        style = "cursor-changed" if style == "changed" else "cursor"
                    line += [ui.Span(style, mark) if style else mark, " " * (widths[column] - ui.cell_len(mark))]
                if index == self.row:
                    at = len(body)
                body.append((line[:-1], True))
        cell = self._cell()
        here: ui.Line = ["  ", ui.bold(f"{self.rows[self.row].text} in {self.columns[self.column]}"),
                         *([": " + cell.note] if cell.note else [])]
        phrases, leaves = self._phrases()
        legend, footnotes = self._legend(notes)
        keys = [("↑↓←→", "move"), ("space", "tick"), ("a", "row"), ("n", "clear row"), ("enter", self.enter),
                ("q", self.leave)]
        bottom = [("blank", ""), ("", here), *(("notes", note) for note in footnotes), ("blank", ""),
                  ("", _count(phrases, len(changes), width, "; ", " (agt keeps a backup)" if leaves else "")),
                  ("legend", legend)]
        return _frame([("title", _title(self.title, width)), ("blank", ""), ("", header)], body, at, bottom, keys, width, height)

    def lines(self, width: int = 100, height: int = 40, *, style: str = "color") -> list[str]:
        return _draw(self.frame(width, height), width, style)

    def fallback(self, read: Callable[[str], str], write: Callable[[str], None]) -> str:
        """The grid with numbered rows and lettered folders: type `2b` to tick or untick one cell."""

        letters = "abcdefghijklmnopqrstuvwxyz"
        while True:
            notes = self._notes()
            heads = [f"{letters[index]}:{column}" for index, column in enumerate(self.columns)]
            widths = [max(len(head), 3) + 2 for head in heads]
            names = max([ui.cell_len(row.text) for row in self.rows] + [8])
            write(ui.plain(_title(self.title)))
            write(" " * (6 + names + 2) + "".join(head.ljust(width) for head, width in zip(heads, widths)).rstrip())
            number = 0
            for kind, indexes in self._groups():
                if kind:
                    write(f"  {kind.capitalize()}")
                for index in indexes:
                    number += 1
                    row = self.rows[index]
                    marks = "".join(self._mark(_get(row, column), notes).ljust(width)
                                    for column, width in zip(self.columns, widths))
                    write(f"  {number:>2}. {row.text}{' ' * (names - ui.cell_len(row.text) + 2)}{marks}".rstrip())
            legend, footnotes = self._legend(notes)
            for line in [legend, *footnotes]:
                write(ui.plain(line))
            try:
                answer = read(f"Cells to tick or untick (2b 3a) · a 2 tick row 2 · n 2 clear row 2 · Enter {self.enter} · "
                              f"q {self.leave}: ")
            except EOFError:
                return QUIT
            answer = answer.strip().lower()
            if not answer:
                return DONE
            if answer == "q":
                return QUIT
            words = answer.replace(",", " ").split()
            if len(words) == 2 and words[0] in ("a", "n") and words[1].isdigit() and 1 <= int(words[1]) <= len(self.rows):
                self.row = int(words[1]) - 1
                self.press(words[0])
                continue
            for word in words:
                number_text, letter = word[:-1], word[-1:]
                if number_text.isdigit() and 1 <= int(number_text) <= len(self.rows) and letter in letters[: len(self.columns)]:
                    self.row, self.column = int(number_text) - 1, letters.index(letter)
                    self.press("space")


# The terminal ------------------------------------------------------------------------


def parse_posix(read: Callable[[], str], more: Callable[[], bool]) -> str:
    """One key from a POSIX terminal. `more()` says whether another byte follows at once (an arrow, not Esc)."""

    char = read()
    if char == "\x1b":
        if not more():
            return "esc"
        second = read()
        if second in ("[", "O") and more():
            return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(read(), "")
        return "esc"
    if char == "\x03":
        raise KeyboardInterrupt
    return {"\r": "enter", "\n": "enter", " ": "space"}.get(char, char.lower())


def parse_windows(getwch: Callable[[], str]) -> str:
    char = getwch()
    if char in ("\x00", "\xe0"):
        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(getwch(), "")
    if char == "\x03":
        raise KeyboardInterrupt
    return {"\r": "enter", " ": "space", "\x1b": "esc"}.get(char, char.lower())


def _raw_available() -> bool:
    try:
        if os.name == "nt":
            import msvcrt  # noqa: F401
        else:
            import termios  # noqa: F401
            import tty  # noqa: F401
    except ImportError:
        return False
    return True


def _windows_colors() -> bool:
    """An old Windows console prints escape codes as text: turn them on, or say it can't."""

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # the console's output
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(mode.value & 0x0004) or bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, ImportError, OSError):
        return False


def can_draw() -> bool:
    """Whether this terminal can send single keys and redraw in place."""

    try:
        terminal = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
    if not terminal or os.environ.get("TERM", "") == "dumb" or not _raw_available():
        return False
    return os.name != "nt" or _windows_colors()


class Terminal:
    """Single keys and in-place redraws, with the terminal put back as it was afterwards, whatever happens."""

    def __enter__(self) -> "Terminal":
        self.drawn = 0
        self.saved = None
        if os.name != "nt":
            import termios
            import tty

            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?25l")  # hide the cursor while the list is up
        sys.stdout.flush()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.saved is not None:
                import termios

                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        finally:  # the frame goes, so the report, the next list or "Nothing was changed." says what happened
            sys.stdout.write((f"\x1b[{self.drawn}F\x1b[J" if self.drawn else "") + "\x1b[?25h")
            sys.stdout.flush()
            self.drawn = 0

    def keys(self) -> Iterator[str]:
        if os.name == "nt":
            import msvcrt

            while True:
                yield parse_windows(msvcrt.getwch)
        import select

        read = lambda: os.read(self.fd, 1).decode("utf-8", "replace")
        more = lambda: bool(select.select([self.fd], [], [], 0.05)[0])  # an arrow's next byte comes at once
        while True:
            yield parse_posix(read, more)

    def draw(self, lines: list[str]) -> None:
        if self.drawn:
            sys.stdout.write(f"\x1b[{self.drawn}F\x1b[J")  # back to the first line of the last drawing, and clear
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        self.drawn = len(lines)


def choose(state: ListState | GridState, *, read: Callable[[str], str] | None = None,
           write: Callable[[str], None] | None = None) -> str:
    """Let a person answer: in place on a terminal that can, numbered otherwise. Returns DONE or QUIT.

    Ctrl-C counts as quit, with nothing changed.
    """

    try:
        if not can_draw():
            return state.fallback(read or input, write or print)
        style = ui.mode(sys.stdout)
        with Terminal() as terminal:
            keys = terminal.keys()
            while True:
                size = shutil.get_terminal_size()
                terminal.draw(state.lines(size.columns - 1, size.lines, style="bare" if style == "plain" else style))
                outcome = state.press(next(keys))
                if outcome:
                    return outcome
    except KeyboardInterrupt:
        return QUIT
