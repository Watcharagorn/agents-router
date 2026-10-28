"""agt share's checkbox lists: the widgets driven key by key, the terminal layer, and `pick`'s grid."""

import builtins
import os
from pathlib import Path
import re
import select
import signal
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import helpers  # noqa: F401  (isolates the test process from your real folders)
from test_share_items import ItemBase
from test_share_plan import FLAGS
from test_share_sync import age

from agents_router import ui
from agents_router.share import commands, picker, settings
from agents_router.share.picker import BLOCKED, DONE, FIXED, OFF, ON, QUIT, Cell, Choice, GridState, ListState, Row


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def press(state, *keys):
    outcome = None
    for key in keys:
        outcome = state.press(key)
    return outcome


class ListTests(unittest.TestCase):
    def folders(self):
        return ListState("Folders", [Choice("claude", "claude ~/.claude", True), Choice("homes", "agt homes", reason="agt's own"),
                                     Choice("kiro", "kiro ~/.kiro", name="kiro"), Choice("codex", "codex ~/.codex", True, name="codex")],
                         verbs=("joins", "leaves"))

    def test_moving_skips_greyed_rows_and_ticks_change_only_usable_ones(self):
        state = self.folders()
        self.assertEqual(state.cursor, 0)
        press(state, "down")
        self.assertEqual(state.cursor, 2)  # past the greyed row
        press(state, "space")
        self.assertEqual(state.checked(), {"claude", "kiro", "codex"})
        press(state, "n")
        self.assertEqual(state.checked(), set())
        press(state, "a")
        self.assertEqual(state.checked(), {"claude", "kiro", "codex"})
        press(state, "j", "j", "j", "space")  # stops at the last row
        self.assertEqual(state.cursor, 3)
        self.assertEqual(press(state, "enter"), DONE)
        self.assertEqual(press(self.folders(), "space", "esc"), QUIT)

    def test_lines_show_marks_reasons_and_a_count(self):
        state = self.folders()
        lines = state.lines(80, 20, style="plain")
        self.assertEqual(lines[:4], ["Folders", "", "  > [x] claude ~/.claude", "    [-] agt homes  agt's own"])
        self.assertEqual(lines[-2:], ["  No changes yet", "  ↑↓ move  space tick  a all  n none  enter done  q back"])
        self.assertTrue(all(ui.cell_len(line) <= 30 for line in state.lines(30, 20, style="plain")))

    def test_a_note_that_doesnt_fit_goes_under_its_row(self):
        lines = self.folders().lines(25, 20, style="plain")
        self.assertEqual(lines[3:5], ["    [-] agt homes", "        agt's own"])
        sharing = ListState("Folders", [Choice("claude", "claude  ~/.claude", True, fixed="sharing now; `agt share cli remove` stops it"),
                                        Choice("kiro", "kiro  ~/.kiro")])
        self.assertEqual(sharing.lines(40, 20, style="plain")[2:5], [  # wrapped between words, never inside the command
            "    [x] claude  ~/.claude", "        sharing now;", "        `agt share cli remove` stops it"])

    def test_the_count_names_each_change(self):
        state = self.folders()
        press(state, "down", "space", "down", "space")
        self.assertIn("  2 changes: kiro joins, codex leaves", state.lines(80, 20, style="plain"))
        self.assertIn("\x1b[7;1;33m[ ]\x1b[0m codex", "\n".join(state.lines(80, 20)))  # changed, under the cursor
        press(state, "space")
        self.assertIn("  1 change: kiro joins", state.lines(80, 20, style="plain"))
        self.assertIn("\x1b[1;33m[x]\x1b[0m kiro", "\n".join(state.lines(80, 20)))

    def test_the_numbered_fallback(self):
        state = self.folders()
        answers = iter(["3", "2", "n", "1 4", ""])
        out = []
        self.assertEqual(state.fallback(lambda prompt: next(answers), out.append), DONE)
        self.assertEqual(state.checked(), {"claude", "codex"})  # the greyed row never ticks
        self.assertEqual(self.folders().fallback(lambda prompt: "q", out.append), QUIT)
        self.assertEqual(self.folders().fallback(lambda prompt: (_ for _ in ()).throw(EOFError), out.append), QUIT)


class GridTests(unittest.TestCase):
    def grid(self):
        rows = [Row("agents/dana", "dana", {"claude": Cell(FIXED, "your original"), "kiro": Cell(ON, there=True),
                                            "codex": Cell(BLOCKED, "built-in")}),
                Row("skills/ask", "ask", {"claude": Cell(OFF), "kiro": Cell(OFF, "kept as your own file"), "codex": Cell(ON)})]
        return GridState("Pick", ["claude", "kiro", "codex"], rows)

    def test_starts_on_a_cell_you_can_change_and_only_changes_count(self):
        state = self.grid()
        self.assertEqual((state.row, state.column), (0, 1))
        press(state, "space")  # dana leaves kiro
        press(state, "left", "space")  # your original: nothing happens
        press(state, "right", "right", "space")  # can't go there: nothing happens
        press(state, "down", "a")  # ask everywhere it can go
        self.assertEqual(state.changes(), {("agents/dana", "kiro"): False, ("skills/ask", "claude"): True,
                                           ("skills/ask", "kiro"): True})
        press(state, "n")
        self.assertEqual(state.changes(), {("agents/dana", "kiro"): False, ("skills/ask", "codex"): False})
        self.assertEqual(press(state, "q"), QUIT)

    def test_lines_show_the_cell_under_the_cursor_and_fit_the_width(self):
        state = self.grid()
        lines = state.lines(100, 20, style="plain")
        self.assertEqual(lines[:7], ["Pick", "", "              claude  kiro  codex", "  Agents", "  > dana       ·      [x]    -¹",
                                     "  Skills", "    ask       [ ]     [ ]   [x]"])
        self.assertEqual(lines[-6:], ["  dana in kiro", "  ¹ Built-in.", "", "  No changes yet",
                                      "  [x] there or goes there  [ ] left out  · stays as it is  - can't be copied there",
                                      "  ↑↓←→ move  space tick  a row  n clear row  enter apply  q quit"])
        press(state, "left")
        self.assertIn("  dana in claude: your original", state.lines(100, 20, style="plain"))
        self.assertTrue(all(ui.cell_len(line) <= 25 for line in state.lines(25, 20, style="plain")))
        self.assertIn("\x1b[7m · \x1b[0m", "\n".join(state.lines(100, 20)))  # the cursor cell, in reverse

    def test_the_count_names_each_change(self):
        state = self.grid()
        press(state, "space", "down", "a")
        self.assertIn("  3 changes: dana leaves kiro; ask goes to claude and kiro (agt keeps a backup)",
                      state.lines(100, 20, style="plain"))
        self.assertIn("  3 changes: dana leaves kiro; 1 more (agt keeps a backup)", state.lines(70, 20, style="plain"))
        self.assertIn("  3 changes", state.lines(40, 20, style="plain"))  # not even one fits: the count alone
        press(state, "up", "space", "down", "right", "right", "space")
        self.assertIn("  3 changes: ask goes to claude and kiro; ask won't go to codex", state.lines(100, 20, style="plain"))

    def test_a_narrow_frame_wraps_the_keys_and_drops_the_titles_hint(self):
        state = GridState([ui.bold("agt share pick"), "  ", ui.muted("which CLIs get what")], self.grid().columns,
                          self.grid().rows)
        lines = state.lines(30, 30, style="plain")
        self.assertEqual(lines[0], "agt share pick")  # the hint goes whole, never cut mid-word
        self.assertEqual(lines[-3:], ["  ↑↓←→ move  space tick  a row", "  n clear row  enter apply", "  q quit"])
        self.assertEqual(state.lines(100, 30, style="plain")[0], "agt share pick  which CLIs get what")

    def test_a_terminal_too_short_for_more_keeps_enter_and_q_on_one_line(self):
        state = GridState("Pick", self.grid().columns, self.grid().rows)
        self.assertEqual(state.lines(34, 6, style="plain")[-1], "  space tick  enter apply  q quit")  # n, a, arrows gone
        self.assertEqual(state.lines(24, 6, style="plain")[-1], "  enter apply  q quit")
        lists = ListTests().folders()
        self.assertEqual(lists.lines(32, 4, style="plain")[-1], "  space tick  enter done  q back")

    def test_two_changed_items_of_one_name_say_which(self):
        rows = [Row("agents/dana", "dana", {"kiro": Cell(OFF)}), Row("skills/dana", "dana", {"kiro": Cell(ON, there=True)})]
        state = GridState("Pick", ["kiro"], rows)
        press(state, "space")
        self.assertIn("  1 change: dana goes to kiro", state.lines(100, 20, style="plain"))
        press(state, "down", "space")
        self.assertIn("  2 changes: agents/dana goes to kiro; skills/dana leaves kiro (agt keeps a backup)",
                      state.lines(100, 20, style="plain"))

    def test_every_frame_fits_the_terminal(self):
        """A frame as tall as the terminal scrolls it, and a line wider than it wraps: either breaks the redraw."""

        columns = ["claude-lab", "claude", "codex", "kiro", "antigravity", "claude-work"]
        rows = [Row(f"{'agents' if number < 12 else 'skills'}/item-{number}", f"item-{number}" + "-long-name" * (number % 3),
                    {column: Cell([ON, OFF, FIXED, BLOCKED][(number + index) % 4], f"note {index}")
                     for index, column in enumerate(columns)}) for number in range(30)]
        state = GridState([ui.bold("agt share pick"), "  ", ui.muted("which CLIs get what")], columns, rows)
        lists = ListState("Options", [Choice(str(number), f"option {number} " * 6) for number in range(25)])
        for keys in ((), ("down",) * 14 + ("right",) * 4 + ("space",), ("down",) * 29 + ("right",) * 5):
            press(state, *keys)
            press(lists, *[key for key in keys if key != "right"])
            for width in (25, 40, 60, 80, 120):
                for height in (6, 8, 12, 20, 40):
                    for frame in (state.lines(width, height), lists.lines(width, height)):
                        self.assertLessEqual(len(frame), height - 1, (width, height))
                        for line in frame:
                            self.assertLessEqual(ui.cell_len(ANSI.sub("", line)), width, (width, height, line))
                    if height >= 12:  # the cursor's row and column always show
                        text = "\n".join(ANSI.sub("", line) for line in state.lines(width, height))
                        self.assertIn("> ", text)
                        self.assertIn(f"{rows[state.row].text} in {columns[state.column]}"[:width - 3], text)

    def test_without_color_the_cursor_cell_keeps_reverse_video(self):
        state = self.grid()
        press(state, "space")
        bare = state.lines(100, 20, style="bare")
        self.assertIn("\x1b[7m[ ]\x1b[0m", "\n".join(bare))  # --color never: only the cursor cell
        self.assertEqual([ANSI.sub("", line) for line in bare], state.lines(100, 20, style="plain"))
        self.assertEqual(ANSI.findall("\n".join(bare)), ["\x1b[7m", "\x1b[0m"])
        mono = "\n".join(state.lines(100, 20, style="mono"))  # NO_COLOR: bold, dim and reverse, no color
        self.assertIn("\x1b[7;1m[ ]", mono)
        self.assertNotRegex(mono, r"\x1b\[[0-9;]*3[0-7]")

    def test_the_numbered_fallback(self):
        state = self.grid()
        answers = iter(["1b", "a 2", "9z", ""])
        self.assertEqual(state.fallback(lambda prompt: next(answers), lambda line: None), DONE)
        self.assertEqual(state.changes(), {("agents/dana", "kiro"): False, ("skills/ask", "claude"): True,
                                           ("skills/ask", "kiro"): True})


class KeyTests(unittest.TestCase):
    def posix(self, data):
        chars = list(data)
        return picker.parse_posix(lambda: chars.pop(0), lambda: bool(chars))

    def test_posix_keys(self):
        self.assertEqual([self.posix(data) for data in ("\x1b[A", "\x1b[B", "\x1bOC", "\x1b[D", "\x1b", "\r", " ", "K")],
                         ["up", "down", "right", "left", "esc", "enter", "space", "k"])
        with self.assertRaises(KeyboardInterrupt):
            self.posix("\x03")

    def test_windows_keys(self):
        def keys(data):
            chars = list(data)
            return picker.parse_windows(lambda: chars.pop(0))

        self.assertEqual([keys(data) for data in ("\xe0H", "\x00P", "\xe0K", "\xe0M", "\r", " ", "\x1b", "a")],
                         ["up", "down", "left", "right", "enter", "space", "esc", "a"])

    def test_the_terminal_is_put_back_whatever_happens(self):
        termios, tty = MagicMock(), MagicMock()
        termios.tcgetattr.return_value = ["saved"]

        def keys(self):
            yield "space"
            raise RuntimeError("a key handler broke")

        with patch.dict("sys.modules", {"termios": termios, "tty": tty}), patch.object(picker, "can_draw", lambda: True), \
                patch.object(picker.Terminal, "keys", keys), patch("sys.stdin.fileno", lambda: 0), \
                patch("sys.stdout.write"), self.assertRaises(RuntimeError):
            picker.choose(ListTests().folders())
        termios.tcsetattr.assert_called_once_with(0, termios.TCSADRAIN, ["saved"])

        def interrupted(self):
            raise KeyboardInterrupt
            yield  # a generator, like the real one

        termios.reset_mock()
        with patch.dict("sys.modules", {"termios": termios, "tty": tty}), patch.object(picker, "can_draw", lambda: True), \
                patch.object(picker.Terminal, "keys", interrupted), patch("sys.stdin.fileno", lambda: 0), \
                patch("sys.stdout.write"):
            self.assertEqual(picker.choose(ListTests().folders()), QUIT)
        termios.tcsetattr.assert_called_once()

    def test_a_dumb_terminal_gets_the_numbered_list(self):
        with patch.dict(os.environ, {"TERM": "dumb"}), patch("sys.stdin.isatty", lambda: True), \
                patch("sys.stdout.isatty", lambda: True):
            self.assertFalse(picker.can_draw())


SRC = str(Path(__file__).resolve().parent.parent / "src")
CHILD = """
import sys, termios
sys.path.insert(0, {src!r})
from agents_router.share import picker
def mode():
    attrs = termios.tcgetattr(0)
    attrs[3] &= ~getattr(termios, "PENDIN", 0)  # macOS's driver sets it on its own when line editing comes back
    return attrs
before = mode()
state = picker.ListState("Folders", [picker.Choice("a", "first"), picker.Choice("b", "second")], verbs=("joins", "leaves"))
outcome = picker.choose(state)
print("RESULT", outcome, ",".join(sorted(state.checked())) or "-", mode() == before, flush=True)
"""


@unittest.skipIf(os.name == "nt", "a pseudo-terminal is POSIX only")
class RealTerminalTests(unittest.TestCase):
    """The picker in a real pseudo-terminal, with real key bytes: no mocks between it and the terminal."""

    def run_keys(self, *writes):
        import pty

        env = {"PATH": os.environ.get("PATH", ""), "TERM": "xterm", "COLUMNS": "60", "LINES": "12", "HOME": "/nonexistent"}
        pid, fd = pty.fork()
        if pid == 0:
            # A shell starts a background job with Ctrl-C ignored, and that survives exec: give the child a terminal's default.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.execve(sys.executable, [sys.executable, "-I", "-c", CHILD.format(src=SRC)], env)
        output = b""
        deadline = time.time() + 15
        try:
            while b"Folders" not in output and time.time() < deadline:  # drawn: it's in single-key mode now
                if select.select([fd], [], [], 0.1)[0]:
                    output += os.read(fd, 4096)
            for data in writes:
                if isinstance(data, float):
                    time.sleep(data)
                elif isinstance(data, tuple):  # ("wait", marker): until the screen shows it, however busy the machine is
                    while data[1] not in output and time.time() < deadline:
                        if select.select([fd], [], [], 0.1)[0]:
                            output += os.read(fd, 4096)
                else:
                    os.write(fd, data)
            while b"RESULT" not in output and time.time() < deadline:
                if select.select([fd], [], [], 0.1)[0]:
                    try:
                        output += os.read(fd, 4096)
                    except OSError:
                        break
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            os.close(fd)
        line = next((line[line.index("RESULT"):] for line in output.decode("utf-8", "replace").splitlines() if "RESULT" in line),
                    None)
        self.assertIsNotNone(line, output[-500:])
        self.assertIn(b"\x1b[J\x1b[?25hRESULT", output)  # the frame is cleared when the picker closes
        return line.split()[1:]

    def test_an_arrow_then_space_then_enter(self):
        self.assertEqual(self.run_keys(b"\x1b[B", b" ", b"\r"), ["done", "b", "True"])

    def test_esc_alone_quits_and_the_terminal_is_put_back(self):
        self.assertEqual(self.run_keys(b" ", b"\x1b", 0.3), ["quit", "a", "True"])

    def test_ctrl_c_quits_and_the_terminal_is_put_back(self):
        # Ctrl-C drops unread keys, so wait until the tick is drawn.
        self.assertEqual(self.run_keys(b" ", ("wait", b"1 change"), b"\x03"), ["quit", "a", "True"])


class PickGridTests(ItemBase):
    """`agt share pick` for a person, through the numbered grid (the tests have no real terminal)."""

    def grid(self, *answers, args=("pick",)):
        typed = iter([*answers, ""])
        with patch.object(commands, "_interactive", lambda: True), patch.object(builtins, "input", lambda prompt: next(typed)):
            return self.share(*args)

    def test_unticking_agts_copy_removes_it_and_leaves_the_rest_alone(self):
        self.enable()
        day_plan = (self.user / ".kiro" / "agents" / "day-plan.json").read_bytes()
        out = self.grid("1d")  # row 1 is dana, column d is kiro
        self.assertIn("  - agents/dana   kiro\n", out)
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertEqual(len(self.backups("dana.json")), 1)
        self.assertEqual(settings.load().not_in, {"agents/dana": ["kiro"]})
        self.assertEqual((self.user / ".kiro" / "agents" / "day-plan.json").read_bytes(), day_plan)
        self.assertIn("everything is in sync", self.share("sync"))

    def test_ticking_a_kept_copy_takes_it_back(self):
        self.enable()
        self.share("pick", "dana", "--not-in", "kiro", "--keep-copies")
        self.grid("1d", args=("pick", "dana"))
        self.assertEqual(settings.load().not_in, {})
        self.assertNotIn("released", self.record("agents/dana"))
        self.assertIn("everything is in sync", self.share("sync"))

    def test_your_original_cant_be_unticked_and_quitting_changes_nothing(self):
        self.enable()
        before = self.snapshot()
        self.assertIn("Nothing was changed.", self.grid("1b"))  # dana in claude is your original
        with patch.object(commands, "_interactive", lambda: True), patch.object(builtins, "input", lambda prompt: "q"):
            self.assertIn("Nothing was changed.", self.share("pick"))
        self.assertEqual(self.snapshot(), before)

    def test_an_agent_with_a_terminal_never_gets_the_grid(self):
        self.enable()
        with patch.dict(os.environ, {"CLAUDECODE": "1"}), patch("sys.stdin.isatty", lambda: True), \
                patch("sys.stdout.isatty", lambda: True), patch.object(picker, "choose", side_effect=AssertionError):
            self.share("pick")


class EnableScreenTests(ItemBase):
    """`agt share enable` for a person at a terminal, answered through the numbered lists."""

    def screen(self, *answers, code=0):
        queue = list(answers)

        def typed(prompt):
            answer = queue.pop(0)
            return answer(prompt) if callable(answer) else answer

        with patch.object(commands, "_interactive", lambda: True), patch.object(builtins, "input", typed):
            out = self.share("enable", *FLAGS, code=code)
        self.assertEqual(queue, [], "some answers weren't asked for")
        return out

    def test_enter_applies_the_plan_on_screen_like_yes(self):
        out = self.screen("s", "")  # gamma-apikey: don't share it; then Enter
        self.assertIn("agt share enable  what sharing would do; nothing is written until you press Enter\n", out)
        self.assertIn("agt share enable  ✓ wrote 9 changes in 4 folders\n", out)
        saved = settings.load()
        self.assertEqual(saved.off, {"skills/gamma-apikey"})
        self.assertEqual([folder.label for folder in saved.folders], ["claude-lab", "claude", "codex", "kiro"])

    def test_p_changes_the_folders_and_q_in_a_list_drops_the_changes(self):
        self.screen("s", "p", "", "4", "", "", "", "")  # untick kiro in the folder list, confirm the rest, then Enter
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertEqual([folder.label for folder in settings.load().folders], ["claude-lab", "claude", "codex"])

    def test_q_in_a_list_drops_its_changes(self):
        self.screen("s", "p", "", "4", "q", "")
        self.assertTrue((self.user / ".kiro" / "agents" / "dana.json").exists())

    def test_the_grid_on_the_screen_leaves_a_cell_out(self):
        self.screen("s", "p", "", "", "", "1d", "", "")  # dana in kiro: unticked
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertTrue((self.user / ".codex" / "agents" / "dana.toml").exists())
        self.assertEqual(settings.load().not_in, {"agents/dana": ["kiro"]})

    def test_a_plan_that_changed_meanwhile_is_shown_again(self):
        def edit_then_enter(prompt):
            self.agent(self.claude, "dana", body="Dana, edited meanwhile.", tools="Bash, Read")
            age(self.claude / "agents" / "dana.md")
            return ""

        out = self.screen("s", edit_then_enter, "")
        self.assertIn("The plan changed while you looked at it; here's the new one.", out)
        self.assertIn("Dana, edited meanwhile.", (self.lab / "agents" / "dana.md").read_text(encoding="utf-8"))

    def test_q_writes_nothing(self):
        before = self.snapshot()
        self.assertIn("Nothing was written.", self.screen("s", "q"))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(settings.path().exists())

    def test_an_agent_with_a_terminal_gets_the_plan_and_no_screen(self):
        with patch.dict(os.environ, {"CLAUDECODE": "1"}), patch("sys.stdin.isatty", lambda: True), \
                patch("sys.stdout.isatty", lambda: True), patch.object(builtins, "input", side_effect=AssertionError):
            out = self.share("enable", *FLAGS, code=2)
        self.assertIn("Add --yes to save these answers", out)

    def test_on_a_rerun_the_folder_list_offers_a_folder_found_later(self):
        self.share("enable", *FLAGS, "--resolve", "gamma-apikey=skip", "--yes")
        personal = self.folder(".claude-other")
        (personal / "agents").mkdir()
        queue = ["p", "", "5", "", "", "", ""]  # tick the new folder (row 5), confirm the rest, then Enter

        with patch.object(commands, "_interactive", lambda: True), patch.object(builtins, "input", lambda prompt: queue.pop(0)):
            out = self.share("enable")
        self.assertEqual(queue, [])
        self.assertIn("claude-other", [folder.label for folder in settings.load().folders])
        self.assertTrue((personal / "agents" / "dana.md").is_file(), out)

