"""agt share's checkbox lists: the widgets driven key by key, the terminal layer, and `pick`'s grid."""

import builtins
import os
import unittest
from unittest.mock import MagicMock, patch

import helpers  # noqa: F401  (isolates the test process from your real folders)
from test_share_items import ItemBase
from test_share_plan import FLAGS
from test_share_sync import age

from agents_router.share import commands, picker, settings
from agents_router.share.picker import BLOCKED, DONE, FIXED, OFF, ON, QUIT, Cell, Choice, GridState, ListState, Row


def press(state, *keys):
    outcome = None
    for key in keys:
        outcome = state.press(key)
    return outcome


class ListTests(unittest.TestCase):
    def folders(self):
        return ListState("Folders", [Choice("claude", "claude ~/.claude", True), Choice("homes", "agt homes", reason="agt's own"),
                                     Choice("kiro", "kiro ~/.kiro"), Choice("codex", "codex ~/.codex", True)], "folders")

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
        lines = self.folders().lines(80, 20, color=False)
        self.assertEqual(lines[1], "> [x] claude ~/.claude")
        self.assertEqual(lines[2], "  [-] agt homes  (agt's own)")
        self.assertIn("2 of 3 folders ticked", lines)
        self.assertTrue(all(len(line) <= 30 for line in self.folders().lines(30, 20, color=False)))

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
        rows = [Row("agents/dana", "dana", {"claude": Cell(FIXED, "your original"), "kiro": Cell(ON), "codex": Cell(BLOCKED, "built-in")}),
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
        lines = state.lines(100, 20, color=False)
        self.assertRegex(lines[2], r"^> dana\s+ ·  \s+\[x\]\s+ - ")
        self.assertIn("0 changes", lines)
        press(state, "left")
        self.assertIn("dana in claude: your original", state.lines(100, 20, color=False))
        self.assertTrue(all(len(line) <= 25 for line in state.lines(25, 20, color=False)[:-3]))
        self.assertIn("\x1b[7m", "\n".join(state.lines(100, 20)))  # the cursor cell, in reverse

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
        self.assertIn("- agents/dana in kiro", out)
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
            self.share("pick", code=3)


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
        self.assertIn("Nothing is written until you press Enter.", out)
        self.assertIn("agt share: wrote 9 changes in 4 folders.", out)
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

