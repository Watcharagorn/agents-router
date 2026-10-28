"""How agt prints for people: when output gets color, and how a line is drawn.

A command builds each line from plain strings and spans (`bold`, `muted`, `ok`, `warn`, `error`,
`cmd`, `code`, `path`) and prints it with `out`. Agents, pipes and tests get the plain renderer: the
same words, with backticks around an inline command, never wrapped. A person at a terminal gets rich,
which only this module imports, and only inside functions, so the hooks and the MCP server never load
it. If rich is missing or broken, output is plain.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time
from typing import IO, Callable, Iterable, NamedTuple, Sequence, Union
import unicodedata

from agents_router import hosts


class Span(NamedTuple):
    style: str
    text: str


Piece = Union[str, Span]
Line = Union[Piece, Sequence[Piece]]

# rich's style for each kind of span: the 16 basic colors only, so the terminal's theme picks the shades.
# Muted text uses the dim attribute, never bright black, which some themes draw invisibly.
STYLES = {
    "bold": "bold", "muted": "dim", "path": "dim",
    "ok": "green", "warn": "yellow", "error": "red",
    "ok-mark": "bold green", "warn-mark": "bold yellow", "error-mark": "bold red",
    "cmd": "cyan", "code": "cyan", "cursor": "reverse", "hang": "dim",
    "changed": "bold yellow", "cursor-changed": "reverse bold yellow",  # the picker: a cell or box you changed
}

MODES = ("auto", "always", "never")


def bold(text: str) -> Span:
    return Span("bold", text)


def hang(text: str) -> Span:
    """A muted label that a wrapped line's next rows line up after, as "It said: " before a worker's message."""

    return Span("hang", text)


def muted(text: str) -> Span:
    return Span("muted", text)


def path(text: str | Path) -> Span:
    return Span("path", short_path(text) if isinstance(text, Path) else text)


def ok(text: str) -> Span:
    return Span("ok", text)


def warn(text: str) -> Span:
    return Span("warn", text)


def error(text: str) -> Span:
    return Span("error", text)


def cmd(text: str) -> Span:
    """A command on a line of its own. It's never wrapped, so it can be copied whole."""

    return Span("cmd", text)


def code(text: str) -> Span:
    """A command inside a sentence: backticks around it in plain output, color in a terminal."""

    return Span("code", text)


def inline(text: str) -> list[Piece]:
    """A sentence whose `backticked` parts are commands: those become code spans, so plain output is unchanged."""

    parts = text.split("`")
    if len(parts) % 2 == 0:  # an odd backtick: leave the sentence as it is
        return [text]
    return [code(part) if index % 2 else part for index, part in enumerate(parts) if part or index % 2]


def tilde(text: str) -> str:
    """`text` with the home folder written as ~, for a sentence that names paths."""

    home = str(Path.home())
    return text.replace(home + os.sep, "~" + os.sep).replace(home + "/", "~/")


def short_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


class Mistake(ValueError):
    """A mistake on the command line that agt can name the fix for. It prints as one error with a
    did-you-mean and the corrected command; as a string it's the message and the suggestion."""

    def __init__(self, prog: str, message: str, suggestion: str | None = None, typed: str | None = None,
                 hint: Line | None = None) -> None:
        super().__init__(message + (f"; did you mean {suggestion}?" if suggestion else ""))
        self.prog, self.message, self.suggestion, self.typed, self.hint = prog, message, suggestion, typed, hint


ARGV: list[str] = []  # the command line after `agt`, for a corrected command


# When to use color -----------------------------------------------------------------------------

_flag: str | None = None  # --color for this run


def set_color(mode: str | None) -> None:
    global _flag
    _flag = mode


def take_color_flag(argv: list[str]) -> tuple[list[str], str | None]:
    """argv without its --color option, which may come anywhere before `--`, and the value it gave."""

    kept: list[str] = []
    value = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            kept += argv[index:]
            break
        if arg == "--color":
            if index + 1 == len(argv):
                raise ValueError("--color takes auto, always or never")
            value = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--color="):
            value = arg.split("=", 1)[1]
        else:
            kept.append(arg)
        index += 1
    if value is not None and value not in MODES:
        raise ValueError(f"--color takes auto, always or never, not {value!r}")
    return kept, value


def mode(stream: IO[str] | None = None) -> str:
    """"color", "mono" (bold and dim but no color, for NO_COLOR) or "plain", for whoever reads `stream`.

    An agent gets plain text even when it has a terminal; --color or AGT_COLOR overrides that.
    """

    stream = sys.stdout if stream is None else stream
    env = os.environ
    wanted = _flag or env.get("AGT_COLOR", "").strip().lower()
    if wanted == "never":
        return "plain"
    if wanted == "always":
        return "color"
    if hosts.agent_running(env):
        return "plain"
    try:
        terminal = stream.isatty()
    except (AttributeError, ValueError):
        terminal = False
    if env.get("NO_COLOR"):
        return "mono" if terminal and env.get("TERM") != "dumb" else "plain"
    if env.get("FORCE_COLOR"):
        return "color"
    if env.get("TERM") == "dumb" or not terminal:
        return "plain"
    return "color"


# Drawing ---------------------------------------------------------------------------------------


def _pieces(line: Line) -> list[Piece]:
    if isinstance(line, (str, Span)):
        return [line]
    return list(line)


def plain(line: Line) -> str:
    return "".join(piece if isinstance(piece, str) else f"`{piece.text}`" if piece.style == "code" else piece.text
                   for piece in _pieces(line))


def cell_len(text: str) -> int:
    """Terminal cells `text` takes: two for a wide character (CJK), none for a mark drawn on the one before it
    (Thai vowels and tones, accents) or an invisible format character."""

    return sum(0 if unicodedata.category(char) in ("Mn", "Me", "Cf") else 2 if unicodedata.east_asian_width(char) in "WF"
               else 1 for char in text)


SGR = {"bold": "1", "dim": "2", "reverse": "7", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
COLORS = ("red", "green", "yellow", "cyan")


def sgr(line: Line, style: str, width: int | None = None) -> str:
    """One line as text with escape codes, for a screen that draws itself (the picker); standard library only.

    `style` is what `mode` gives: "color", "mono" (bold, dim and reverse, no color) or "plain" (no codes, and backticks
    around an inline command, as `plain`). With `width`, the line is cut to that many cells and ends in "…"; the codes
    never count, so the terminal never wraps it.
    """

    pieces = [(piece.style, piece.text) if isinstance(piece, Span) else ("", piece) for piece in _pieces(line)]
    if style == "plain":
        pieces = [(name, f"`{text}`" if name == "code" else text) for name, text in pieces]
    if width is not None and sum(cell_len(text) for _, text in pieces) > width:
        kept, room = [], max(width - 1, 0)
        for name, text in pieces:
            cut = ""
            for char in text:
                if cell_len(cut + char) > room:
                    break
                cut += char
            kept.append((name, cut))
            room -= cell_len(cut)
            if cut != text:
                break
        pieces = kept + ([("", "…")] if width > 0 else [])
    drawn = []
    for name, text in pieces:
        words = [] if style == "plain" else [word for word in STYLES.get(name, "").split()
                                              if style == "color" or word not in COLORS]
        codes = ";".join(SGR[word] for word in words if word in SGR)
        drawn.append(f"\x1b[{codes}m{text}\x1b[0m" if codes and text else text)
    return "".join(drawn)


def columns(rows: Iterable[Sequence[Line]], indent: int = 2, gap: int = 2) -> list[list[Piece]]:
    """Rows of cells lined up in columns, measured on their plain text. The last cell of a row isn't padded."""

    rows = [[cell for cell in row] for row in rows]
    while any(row and plain(row[-1]) == "" for row in rows):
        rows = [row[:-1] if row and plain(row[-1]) == "" else row for row in rows]
    count = max((len(row) for row in rows), default=0)
    widths = [max((cell_len(plain(row[i])) for row in rows if i < len(row) - 1), default=0) for i in range(count)]
    lines = []
    for row in rows:
        pieces: list[Piece] = [" " * indent]
        for i, cell in enumerate(row):
            pieces += _pieces(cell)
            if i < len(row) - 1:
                pieces.append(" " * (widths[i] - cell_len(plain(cell)) + gap))
        lines.append(pieces)
    return lines


_console_cache: tuple[object, str, object] | None = None


def _console(stream: IO[str], style: str):
    global _console_cache
    if _console_cache and _console_cache[0] is stream and _console_cache[1] == style:
        return _console_cache[2]
    try:
        from rich.console import Console
    except Exception:  # rich missing or broken: plain output, never a failed command
        return None
    console = Console(file=stream, markup=False, highlight=False, emoji=False, color_system="standard",
                      force_terminal=True, no_color=style == "mono")
    _console_cache = (stream, style, console)
    return console


def _text(line: Line):
    from rich.text import Text

    text = Text()
    for piece in _pieces(line):
        if isinstance(piece, Span):
            text.append(piece.text, style=STYLES.get(piece.style, ""))
        else:
            text.append(piece)
    return text


def _draw(console, line: Line) -> None:
    from rich.text import Text

    pieces = _pieces(line)
    text = _text(line)
    width = console.width
    if cell_len(text.plain) <= width or any(isinstance(piece, Span) and piece.style == "cmd" for piece in pieces):
        console.print(text, soft_wrap=True)  # a line holding a command is left to the terminal, never broken
        return
    indent = len(text.plain) - len(text.plain.lstrip(" "))
    label = 0
    if any(isinstance(piece, Span) and piece.style == "hang" for piece in pieces):  # rows go on after the label
        for piece in pieces:
            label += cell_len(piece if isinstance(piece, str) else piece.text)
            if isinstance(piece, Span) and piece.style == "hang":
                break
        label -= indent
        if width - indent - label < 20:
            label = 0
    if label:
        rows = text[indent + label:].wrap(console, width - indent - label)
        console.print(text[:indent + label] + rows[0], soft_wrap=True)
        for part in rows[1:]:
            console.print(Text(" " * (indent + label)) + part, soft_wrap=True)
        return
    for part in text[indent:].wrap(console, max(20, width - indent)):
        console.print(Text(" " * indent) + part, soft_wrap=True)


def width(stream: IO[str] | None = None) -> int | None:
    """The terminal's width when `out` draws for a person, else None: plain output is never fitted to a width."""

    stream = sys.stdout if stream is None else stream
    style = mode(stream)
    console = _console(stream, style) if style != "plain" else None
    return console.width if console is not None else None


def help_formatter(prog: str):
    """rich-argparse's formatter for a person at a terminal, set to leave agt's text as it is; else None."""

    style = mode(sys.stdout)
    if style == "plain":
        return None
    console = _console(sys.stdout, style)
    try:
        from rich_argparse import RichHelpFormatter
    except Exception:
        return None
    if console is None:
        return None

    class Formatter(RichHelpFormatter):
        help_markup = False  # agt's help names [antigravity] tables and [x] boxes; they aren't markup
        text_markup = False

    try:
        return Formatter(prog, console=console)
    except TypeError:  # an older rich-argparse without console=
        return Formatter(prog)


def out(*lines: Line, stream: IO[str] | None = None) -> None:
    """Print each line: in color for a person at a terminal, plain for everyone else."""

    stream = sys.stdout if stream is None else stream
    style = mode(stream)
    console = _console(stream, style) if style != "plain" else None
    if console is None:
        for line in lines:
            print(plain(line), file=stream)
        return
    for line in lines:
        _draw(console, line)


# Slow work -------------------------------------------------------------------------------------

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def live_on(stream: IO[str] | None = None) -> bool:
    """Whether a display that redraws in place may draw on `stream` (stderr by default): only for a person at a
    terminal. Agents, pipes and TERM=dumb never get one; --color decides only its colors."""

    stream = sys.stderr if stream is None else stream
    if os.environ.get("TERM") == "dumb" or hosts.agent_running(os.environ):
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def clock(seconds: float) -> str:
    """A running time: 0:12, 1:12, 1:02:03."""

    minutes, secs = divmod(int(max(0, seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def took(seconds: float) -> str:
    """How long something took: 4s, 1m 12s, 1h 02m."""

    seconds = int(round(max(0, seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02}m"


class Live:
    """Lines on stderr that redraw in place while slow work runs, and clear when it ends, so what the command
    prints after is the same for a person and a pipe. `draw(spin, now)` gives the lines; `spin` is the spinner's
    frame. It starts drawing after `delay` seconds, so quick work doesn't flash it. Where `live_on` is false, or
    rich is missing, it draws nothing."""

    delay = 1.0

    def __init__(self, draw: Callable[[str, float], list[Line]]) -> None:
        self.draw = draw
        self._live = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._last: list[Line] = []

    def __enter__(self) -> "Live":
        if not live_on(sys.stderr):
            return self
        style = mode(sys.stderr)
        console = _console(sys.stderr, "mono" if style == "plain" else style)  # --color never: no color, still live
        try:
            from rich.live import Live as RichLive
        except Exception:
            return self
        if console is None:
            return self
        self._live = RichLive(get_renderable=lambda: self._render(console), console=console, transient=True,
                              refresh_per_second=8, redirect_stdout=False, redirect_stderr=False,
                              vertical_overflow="crop")
        self._timer = threading.Timer(self.delay, self._start)
        self._timer.daemon = True
        self._timer.start()
        return self

    def _start(self) -> None:
        with self._lock:
            if self._live is not None:
                self._live.start()

    def _render(self, console):
        from rich.text import Text

        now = time.monotonic()
        try:
            lines = self.draw(SPINNER[int(now * 10) % len(SPINNER)], now)
        except Exception:  # a file read mid-rename, say: the last frame stays, and the work goes on
            lines = self._last
        self._last = lines
        room = max(2, console.height - 1)
        if len(lines) > room:  # the first line (what runs) and the newest ones that fit
            lines = lines[:1] + lines[len(lines) - room + 1:]
        shown = []
        for line in lines:
            text = _text(line)
            text.no_wrap, text.overflow = True, "ellipsis"  # one row each, so the redraw lines up
            shown.append(text)
        return Text("\n").join(shown)

    def __exit__(self, *exc: object) -> None:
        if self._timer is not None:
            self._timer.cancel()
        with self._lock:
            if self._live is not None and self._live.is_started:
                self._live.stop()
            self._live = None

    @property
    def drawing(self) -> bool:
        return self._live is not None and self._live.is_started


def spinner(text: Line) -> Live:
    """One line while one slow step runs: a spinner, what runs and its time."""

    started = time.monotonic()
    return Live(lambda spin, now: [[Span("muted", spin), " ", *_pieces(text), "  ", muted(clock(now - started))]])


class Steps(Live):
    """Slow steps one under another, under a head line: each finished one with its mark and how long it took,
    the running one with a spinner, what it runs now and its time."""

    def __init__(self, head: Line) -> None:
        super().__init__(self._lines)
        self.head = head
        self.done: list[tuple[Span, list[Line], str]] = []
        self.running: tuple[list[Line], str, float] | None = None

    def start(self, *cells: Line) -> None:
        self.running = (list(cells), "", time.monotonic())

    def now(self, what: str) -> None:
        """What the running step runs now (a command), shown after it."""

        if self.running is not None:
            self.running = (self.running[0], what, self.running[2])

    def finish(self, ok: bool, mark: Span = Span("ok-mark", "+")) -> None:
        if self.running is not None:
            cells, _, started = self.running
            self.done.append((mark if ok else Span("error-mark", "✗"), cells, took(time.monotonic() - started)))
            self.running = None

    def _lines(self, spin: str, now: float) -> list[Line]:
        def row(mark: Span, cells: list[Line], last: Line) -> list[Line]:
            return [[mark, " ", *_pieces(cells[0])], *cells[1:], last] if cells else [mark, last]

        rows = [row(mark, cells, muted(spent)) for mark, cells, spent in self.done]
        if self.running is not None:
            cells, what, started = self.running
            doing = [muted("running "), code(what), "  "] if what else []
            rows.append(row(Span("muted", spin), cells, [*doing, muted(clock(now - started))]))
        return [self.head, "", *columns(rows, gap=3)]
