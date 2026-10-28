"""`agt --help`, `agt share --help`, and mistakes on the command line."""

import argparse
import contextlib
import io
import unittest

from test_share_plan import MacCopy
from test_share_sync import YES

from agents_router import cli, helptext


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def subcommands(parser):
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices


class HelpTests(unittest.TestCase):
    def test_every_command_people_type_is_on_its_help_page(self):
        root = cli.build_parser()
        for parser, page in ((root, helptext.ROOT), (subcommands(root)["share"], helptext.SHARE)):
            listed = {name for _, rows in page["sections"] for name, _ in rows}
            typed = set(subcommands(parser)) - helptext.INTERNAL - helptext.UNLISTED
            with self.subTest(page=page["name"]):
                self.assertEqual(listed, typed)

    def test_help_pages_are_grouped_and_hide_internal_commands(self):
        code, out, _ = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("Usage  agt <command> [options]", out)
        self.assertIn("Run tasks in another CLI\n  run ", out)
        self.assertIn("Exit codes  0 done", out)
        self.assertNotIn("_runner", out)
        self.assertNotIn("==SUPPRESS==", out)
        self.assertNotIn("\x1b", out)
        code, out, _ = run("share", "--help")
        self.assertEqual(code, 0)
        self.assertIn("Change what goes where\n  pick ", out)
        self.assertNotIn("hook-run", out)
        self.assertNotIn("  scan ", out)
        self.assertNotIn("==SUPPRESS==", out)

    def test_other_commands_keep_argparse_help(self):
        code, out, _ = run("share", "pick", "--help")
        self.assertEqual(code, 0)
        self.assertIn("usage: agt share pick", out)
        self.assertNotIn("\x1b", out)


class MistakeTests(MacCopy):
    def test_a_mistyped_command_names_the_closest_one(self):
        code, out, err = run("shar", "status")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err.splitlines(), ["✗ agt: there's no command shar", "  Did you mean share?",
                                            "    agt share status", "  `agt --help` lists every command."])
        code, _, err = run("share", "stats")
        self.assertEqual(code, 2)
        self.assertIn("✗ agt share: there's no command stats", err)
        self.assertIn("    agt share status", err)

    def test_an_unknown_flag_points_at_that_commands_help(self):
        code, _, err = run("share", "pick", "--bogus")
        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments: --bogus", err)
        self.assertIn("`agt share pick --help` shows how to use it.", err)

    def test_an_unknown_item_names_the_closest_one(self):
        run("share", *YES)
        code, _, err = run("share", "pick", "dna", "--in", "kiro")
        self.assertEqual(code, 2)
        self.assertIn("✗ agt share pick: no agent or skill named dna in the folders sharing", err)
        self.assertIn("  Did you mean dana?\n    agt share pick dana --in kiro\n", err)

    def test_an_unknown_setting_names_the_closest_one(self):
        code, _, err = run("config", "set", "share.auto_cpy", "on")
        self.assertEqual(code, 2)
        self.assertIn("✗ agt config: agt has no setting share.auto_cpy", err)
        self.assertIn("  Did you mean share.auto_copy?\n    agt config set share.auto_copy on\n", err)


if __name__ == "__main__":
    unittest.main()
