"""Checkbox lists for agt share in a terminal: a list of choices, and the items × folders grid.

Keys: ↑/↓ or j/k move, ←/→ or h/l move between folders in the grid, space
ticks or unticks, `a` ticks all (the whole row in the grid), `n` clears,
Enter confirms, and `q` or Esc leaves without changing anything. Rows agt
won't use are greyed out with the reason, never hidden, and the cursor skips
them. Where the terminal can't send single keys (`TERM=dumb`), the same list
comes numbered and you type your answers.

Only people get these: commands check for an agent first (`commands._interactive`)
and give it `needs_choice` instead. The widgets are plain state (`press` a key,
read its `lines`), so they're tested without a terminal; `Terminal` is the only
part that touches one, and it always puts the terminal back as it was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import shutil
import sys
from typing import Callable, Iterator

ON, OFF, FIXED, BLOCKED = "on", "off", "fixed", "blocked"
DONE, QUIT = "done", "quit"
MARKS = {ON: "[x]", OFF: "[ ]", FIXED: " · ", BLOCKED: " - "}
DIM, REVERSE, PLAIN = "\x1b[2m", "\x1b[7m", "\x1b[0m"


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


# A list of choices -------------------------------------------------------------


@dataclass
class Choice:
    key: str
    text: str
    checked: bool = False
    reason: str | None = None  # why agt won't use it: shown greyed out, and the cursor skips it
    fixed: str | None = None  # ticked, and changed elsewhere (shown greyed with this note; the cursor skips it)

    @property
    def locked(self) -> bool:
        return self.reason is not None or self.fixed is not None


class ListState:
    def __init__(self, title: str, choices: list[Choice], noun: str):
        self.title, self.choices, self.noun = title, choices, noun
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

    def lines(self, width: int = 100, height: int = 40, *, color: bool = True) -> list[str]:
        body = []
        for index, choice in enumerate(self.choices):
            mark = "[-]" if choice.reason else "[x]" if choice.checked or choice.fixed else "[ ]"
            pointer = ">" if index == self.cursor else " "
            note = choice.reason or choice.fixed
            text = _fit(f"{pointer} {mark} {choice.text}" + (f"  ({note})" if note else ""), width)
            body.append(f"{DIM}{text}{PLAIN}" if color and choice.locked else text)  # cut first, so codes never count
        room = max(height - 4, 3)
        top = max(0, min(self.cursor - room // 2, len(body) - room))
        count = f"{len(self.checked())} of {len(self._usable())} {self.noun} ticked"
        keys = "↑↓ move  space tick  a all  n none  enter confirm  q quit"
        return [_fit(self.title, width), *body[top: top + room], _fit(count, width), _fit(keys, width)]

    def fallback(self, read: Callable[[str], str], write: Callable[[str], None]) -> str:
        """The same list numbered, for a terminal without single keys."""

        while True:
            for number, choice in enumerate(self.choices, 1):
                mark = "[-]" if choice.reason else "[x]" if choice.checked or choice.fixed else "[ ]"
                note = choice.reason or choice.fixed
                write(f"  {number:>2}. {mark} {choice.text}" + (f"  ({note})" if note else ""))
            try:
                answer = read("Type numbers to tick or untick (1 3), a for all, n for none; Enter confirms, q quits: ")
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


@dataclass
class Row:
    key: str
    text: str
    cells: dict[str, Cell] = field(default_factory=dict)


class GridState:
    def __init__(self, title: str, columns: list[str], rows: list[Row]):
        self.title, self.columns, self.rows = title, columns, rows
        self.first = {(row.key, column): row.cells[column].state for row in rows for column in columns if column in row.cells}
        self.row, self.column = 0, 0
        self._settle()

    def _cell(self, row: int | None = None, column: int | None = None) -> Cell:
        return self.rows[self.row if row is None else row].cells.get(self.columns[self.column if column is None else column],
                                                                    Cell(BLOCKED, "not a folder agt shares"))

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

    def lines(self, width: int = 100, height: int = 40, *, color: bool = True) -> list[str]:
        name_width = max([len(row.text) for row in self.rows] + [8]) + 2
        header = " " * (name_width + 2) + "".join(f"{column[:11]:<12}" for column in self.columns)
        body = []
        for index, row in enumerate(self.rows):
            pointer = ">" if index == self.row else " "
            start = f"{pointer} {_fit(row.text, name_width - 1):<{name_width}}"
            marks = [MARKS[row.cells.get(column, Cell(BLOCKED)).state] for column in self.columns]
            plain = start + "".join(f"{mark:<12}" for mark in marks)
            if not color or len(plain) > width:
                body.append(_fit(plain, width))  # too wide for colour codes: the pointer and the note line still show where you are
                continue
            parts = []
            for column_index, (column, mark) in enumerate(zip(self.columns, marks)):
                state = row.cells.get(column, Cell(BLOCKED)).state
                if index == self.row and column_index == self.column:
                    mark = f"{REVERSE}{mark}{PLAIN}"
                elif state in (FIXED, BLOCKED):
                    mark = f"{DIM}{mark}{PLAIN}"
                parts.append(mark + " " * 9)
            body.append(start + "".join(parts))
        room = max(height - 7, 3)
        top = max(0, min(self.row - room // 2, len(body) - room))
        note = self._cell().note if self.rows and self.columns else ""
        here = f"{self.rows[self.row].text} in {self.columns[self.column]}: {note}" if self.rows and self.columns and note else ""
        changed = len(self.changes())
        keys = "↑↓←→ move  space tick  a row  n clear row  enter apply  q quit"
        legend = "[x] there or goes there  [ ] left out  · stays as it is  - can't go there"
        return [_fit(self.title, width), _fit(header, width), *body[top: top + room], _fit(here, width),
                f"{changed} change{'s' if changed != 1 else ''}", _fit(legend, width), _fit(keys, width)]

    def fallback(self, read: Callable[[str], str], write: Callable[[str], None]) -> str:
        """The grid with numbered rows and lettered folders: type `2b` to tick or untick one cell."""

        letters = "abcdefghijklmnopqrstuvwxyz"
        while True:
            write("      " + "".join(f"{letters[i]}:{column[:9]:<10}" for i, column in enumerate(self.columns)))
            for number, row in enumerate(self.rows, 1):
                marks = "".join(f"{MARKS[row.cells.get(column, Cell(BLOCKED)).state]:<12}" for column in self.columns)
                write(f"  {number:>2}. {row.text:<20} {marks}")
            try:
                answer = read("Type cells to tick or untick (2b 3a), or `a 2` / `n 2` for a whole row; "
                              "Enter applies, q quits: ")
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
                number, letter = word[:-1], word[-1:]
                if number.isdigit() and 1 <= int(number) <= len(self.rows) and letter in letters[: len(self.columns)]:
                    self.row, self.column = int(number) - 1, letters.index(letter)
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


def can_draw() -> bool:
    """Whether this terminal can send single keys and redraw in place."""

    try:
        terminal = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
    return terminal and os.environ.get("TERM", "") != "dumb" and _raw_available()


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
        finally:
            sys.stdout.write("\x1b[?25h\n")
            sys.stdout.flush()

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
        with Terminal() as terminal:
            keys = terminal.keys()
            while True:
                size = shutil.get_terminal_size()
                terminal.draw(state.lines(size.columns - 1, size.lines))
                outcome = state.press(next(keys))
                if outcome:
                    return outcome
    except KeyboardInterrupt:
        return QUIT
