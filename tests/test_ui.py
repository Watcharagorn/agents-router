import ast
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

import helpers  # noqa: F401  (isolates the environment)
from agents_router import ui

SRC = Path(__file__).resolve().parents[1] / "src"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

try:
    import rich  # noqa: F401
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


class Terminal(io.StringIO):
    def isatty(self):
        return True


SAMPLE = [
    [ui.bold("agt share"), "  on, ", ui.warn("3 things wait for you")],
    "",
    *ui.columns([[ui.muted("Folders"), "claude-lab, claude, codex, kiro"], [ui.muted("Items"), [ui.ok("5 in sync"), ", 11 not shared"]]]),
    ["  ", ui.Span("warn-mark", "!"), " ", ui.bold("plugin deck"), ", for codex"],
    ["    ", ui.muted("Install it as it is"), "   ", ui.cmd("agt share pick plugins/deck@tools --in codex")],
    [ui.code("agt share plugins"), " ", ui.muted("says why."), " ", ui.path(Path.home() / ".claude")],
]


def render(lines, stream):
    ui.out(*lines, stream=stream)
    return stream.getvalue()


class PlainTests(unittest.TestCase):
    def setUp(self):
        ui.set_color(None)

    def test_plain_keeps_the_words_and_puts_backticks_around_inline_commands(self):
        text = render(SAMPLE, io.StringIO())
        self.assertEqual(text.splitlines(), [
            "agt share  on, 3 things wait for you",
            "",
            "  Folders  claude-lab, claude, codex, kiro",
            "  Items    5 in sync, 11 not shared",
            "  ! plugin deck, for codex",
            "    Install it as it is   agt share pick plugins/deck@tools --in codex",
            "`agt share plugins` says why. ~/.claude",
        ])

    def test_columns_line_up_by_terminal_cells(self):
        lines = [ui.plain(line) for line in ui.columns([["ภาษา", "a"], ["中文", "b"], ["x", "c", ""]], indent=0)]
        self.assertEqual(lines, ["ภาษา  a", "中文  b", "x     c"])

    def test_cell_len_counts_wide_and_combining_characters(self):
        self.assertEqual(ui.cell_len("中文"), 4)
        self.assertEqual(ui.cell_len("ที่"), 1)
        self.assertEqual(ui.cell_len("✓ · …"), 5)

    def test_sgr_draws_a_line_for_the_picker_without_rich(self):
        line = ["  ", ui.bold("dana"), " in ", ui.Span("cursor-changed", "[ ]"), " ", ui.code("agt x"), " ", ui.ok("中文")]
        self.assertEqual(ui.sgr(line, "color"),
                         "  \x1b[1mdana\x1b[0m in \x1b[7;1;33m[ ]\x1b[0m \x1b[36magt x\x1b[0m \x1b[32m中文\x1b[0m")
        self.assertEqual(ui.sgr(line, "mono"), "  \x1b[1mdana\x1b[0m in \x1b[7;1m[ ]\x1b[0m agt x 中文")  # NO_COLOR: no hue
        self.assertEqual(ui.sgr(line, "plain"), "  dana in [ ] `agt x` 中文")
        self.assertEqual(ui.sgr(line, "plain", 12), "  dana in […")  # cut on cells; the codes never count
        self.assertEqual(ui.sgr(line, "color", 8), "  \x1b[1mdana\x1b[0m …")
        self.assertEqual(ui.sgr(line, "plain", 26), "  dana in [ ] `agt x` 中文")
        self.assertEqual(ui.sgr(line, "plain", 24), "  dana in [ ] `agt x` …")  # a wide character that won't fit goes

    def test_color_flag_comes_out_of_argv_anywhere_before_the_separator(self):
        self.assertEqual(ui.take_color_flag(["share", "status", "--color", "never"]), (["share", "status"], "never"))
        self.assertEqual(ui.take_color_flag(["--color=always", "jobs"]), (["jobs"], "always"))
        self.assertEqual(ui.take_color_flag(["launch", "--", "--color", "x"]), (["launch", "--", "--color", "x"], None))
        with self.assertRaisesRegex(ValueError, "auto, always or never"):
            ui.take_color_flag(["jobs", "--color", "sometimes"])
        with self.assertRaisesRegex(ValueError, "auto, always or never"):
            ui.take_color_flag(["jobs", "--color"])


class ModeTests(unittest.TestCase):
    def setUp(self):
        ui.set_color(None)

    def tearDown(self):
        ui.set_color(None)

    def mode(self, env=None, terminal=True, flag=None):
        ui.set_color(flag)
        with patch.dict(os.environ, env or {}):
            return ui.mode(Terminal() if terminal else io.StringIO())

    def test_who_gets_color(self):
        self.assertEqual(self.mode(), "color")
        self.assertEqual(self.mode(terminal=False), "plain")
        self.assertEqual(self.mode({"TERM": "dumb"}), "plain")
        self.assertEqual(self.mode({"NO_COLOR": "1"}), "mono")
        self.assertEqual(self.mode({"NO_COLOR": "1"}, terminal=False), "plain")
        self.assertEqual(self.mode({"NO_COLOR": ""}), "color")
        self.assertEqual(self.mode({"FORCE_COLOR": "1"}, terminal=False), "color")
        self.assertEqual(self.mode({"FORCE_COLOR": ""}, terminal=False), "plain")

    def test_an_agent_gets_plain_text_even_with_a_terminal(self):
        for env in ({"CLAUDECODE": "1"}, {"CODEX_THREAD_ID": "t"}, {"AGT_JOB_ID": "j"}, {"AGT_DEPTH": "1"}):
            with self.subTest(env=env):
                self.assertEqual(self.mode(env), "plain")
                self.assertEqual(self.mode({**env, "FORCE_COLOR": "1"}), "plain")

    def test_the_flag_and_agt_color_override_everything(self):
        self.assertEqual(self.mode({"CLAUDECODE": "1"}, flag="always"), "color")
        self.assertEqual(self.mode(flag="never"), "plain")
        self.assertEqual(self.mode({"AGT_COLOR": "always", "CLAUDECODE": "1"}, terminal=False), "color")
        self.assertEqual(self.mode({"AGT_COLOR": "never"}), "plain")
        self.assertEqual(self.mode({"AGT_COLOR": "never"}, flag="always"), "color")


@unittest.skipUnless(HAVE_RICH, "rich isn't installed")
class ColorTests(unittest.TestCase):
    def setUp(self):
        ui.set_color(None)

    def test_color_output_without_its_codes_is_the_plain_output_minus_backticks(self):
        colored = render(SAMPLE, Terminal())
        self.assertIn("\x1b[", colored)
        plain = render(SAMPLE, io.StringIO())
        self.assertEqual(ANSI.sub("", colored), plain.replace("`", ""))

    def test_only_the_basic_colors_and_dim_are_used(self):
        colored = render(SAMPLE, Terminal())
        codes = {code for found in re.findall(r"\x1b\[([0-9;]*)m", colored) for code in found.split(";") if code}
        self.assertLessEqual(codes, {"0", "1", "2", "31", "32", "33", "36"})

    def test_no_color_keeps_bold_and_dim_but_no_hue(self):
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            colored = render(SAMPLE, Terminal())
        self.assertIn("\x1b[1m", colored)
        self.assertNotRegex(colored, r"\x1b\[[0-9;]*3[0-7]")

    def test_prose_wraps_under_its_indent_and_a_command_line_never_wraps(self):
        long = "word " * 30
        command = "agt share enable --yes --if-plan a015cac9c69f " + "--claude-dir '~/.claude' " * 4
        with patch.dict(os.environ, {"COLUMNS": "60"}):
            colored = ANSI.sub("", render(["    " + long.strip(), ["  ", ui.cmd(command)]], Terminal()))
        lines = colored.splitlines()
        self.assertTrue(all(len(line) <= 60 for line in lines[:-1]), lines)
        self.assertTrue(all(line.startswith("    word") for line in lines[:-1]), lines)
        self.assertEqual(lines[-1], "  " + command)

    def test_a_hanging_label_keeps_the_rows_after_it(self):
        with patch.dict(os.environ, {"COLUMNS": "60"}):
            colored = ANSI.sub("", render([["  ", ui.hang("It said: "), "word " * 25]], Terminal()))
        lines = colored.splitlines()
        self.assertTrue(lines[0].startswith("  It said: word"), lines)
        self.assertTrue(all(line.startswith(" " * 11 + "word") for line in lines[1:]) and len(lines) > 1, lines)

    def test_text_is_never_read_as_markup_or_emoji(self):
        colored = ANSI.sub("", render(["[x] claude  [antigravity]  :smile:  [bold]no[/bold]"], Terminal()))
        self.assertEqual(colored, "[x] claude  [antigravity]  :smile:  [bold]no[/bold]\n")


class FallbackTests(unittest.TestCase):
    def tearDown(self):
        ui.set_color(None)
        ui._console_cache = None

    def test_without_rich_a_terminal_gets_plain_output(self):
        ui._console_cache = None
        ui.set_color("always")
        with patch.dict(sys.modules, {"rich": None, "rich.console": None, "rich.text": None}):
            text = render(SAMPLE[:1], Terminal())
        self.assertEqual(text, "agt share  on, 3 things wait for you\n")


class CommandTests(unittest.TestCase):
    """agt jobs and agt log, the first commands drawn through ui."""

    def setUp(self):
        ui.set_color(None)

    def tearDown(self):
        ui.set_color(None)

    def run_agt(self, *argv):
        from agents_router import cli

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_color_flag_works_anywhere_and_rejects_other_values(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_agt("jobs", "--color", "loud")
        self.assertEqual(code, 2)
        self.assertIn("--color takes auto, always or never", err.getvalue())
        code, out = self.run_agt("jobs", "--all", "--color", "never")
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)


class HotPathTests(unittest.TestCase):
    """Commands agents and hosts run often never load rich and never color, whatever the terminal says."""

    def run_module(self, *argv, stdin="{}"):
        env = {**os.environ, "PYTHONPATH": str(SRC), "FORCE_COLOR": "1", "AGT_COLOR": ""}
        probe = ("import runpy, sys; sys.argv = ['agt', *sys.argv[1:]]\n"
                 "from agents_router.entry import main\n"
                 "try:\n    code = main()\nexcept SystemExit as exc:\n    code = exc.code\n"
                 "print('RICH-LOADED' if 'rich' in sys.modules else 'RICH-ABSENT', file=sys.stderr)\n")
        return subprocess.run([sys.executable, "-c", probe, *argv], input=stdin, capture_output=True, text=True,
                              env=env, timeout=60)

    def test_hooks_and_share_session_stay_plain_and_rich_free(self):
        for argv in (("hook", "pending"), ("share", "session", "--label", "claude"), ("share", "badge")):
            with self.subTest(argv=argv):
                done = self.run_module(*argv)
                self.assertIn("RICH-ABSENT", done.stderr, done.stderr)
                self.assertNotIn("\x1b", done.stdout + done.stderr)

    def test_hook_entry_skips_the_rest_of_the_cli(self):
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        probe = ("import sys; sys.argv = ['agt', 'hook', 'pending']\n"
                 "from agents_router.entry import main\nmain()\n"
                 "print(json.dumps(sorted(m for m in sys.modules if m.startswith('agents_router'))), file=sys.stderr)\n")
        done = subprocess.run([sys.executable, "-c", "import json\n" + probe], input="{}", capture_output=True,
                              text=True, env=env, timeout=60)
        loaded = json.loads(done.stderr.strip().splitlines()[-1])
        self.assertNotIn("agents_router.cli", loaded)
        self.assertNotIn("agents_router.share.commands", loaded)


class ImportTests(unittest.TestCase):
    def imports(self, path):
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_only_ui_imports_rich(self):
        for path in (SRC / "agents_router").rglob("*.py"):
            if path.name == "ui.py":
                continue
            with self.subTest(path=path.relative_to(SRC)):
                self.assertFalse(self.imports(path) & {"rich", "rich_argparse"})

    def test_the_hook_runners_stay_on_the_standard_library(self):
        # Both run under `python -I`; the read hook may reach its own validator package, nothing else.
        for name, allowed in (("share/hook_run.py", set()), ("claude_read_hook.py", {"agents_router"})):
            path = SRC / "agents_router" / name
            with self.subTest(name=name):
                outside = {module for module in self.imports(path) if module not in sys.stdlib_module_names}
                self.assertLessEqual(outside, allowed)
        self.assertNotIn("agents_router.ui", (SRC / "agents_router" / "claude_read_hook.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
