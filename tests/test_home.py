"""`agt` and `agt share` with no command: the home screens."""

import contextlib
import io
import time
import unittest
from unittest.mock import patch

from test_share_plan import MacCopy
from test_share_sync import YES

from agents_router import cli, home
from agents_router.runtimes import get_runtime


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class HomeTests(MacCopy):
    def test_before_setup_it_says_what_agt_does_and_how_to_start(self):
        code, out, _ = run()
        self.assertEqual(code, 0)
        self.assertIn("not set up yet", out.splitlines()[0])
        self.assertIn("hand tasks to the other agent CLIs you have", out)
        self.assertIn("Found here", out)
        self.assertRegex(out, r"Claude Code +~/\.claude, ~/\.claude-lab")
        self.assertRegex(out, r"See what setup would do +agt setup --dry-run")

    def test_after_setup_it_lists_each_thing_to_do_with_its_command(self):
        run("share", *YES)
        code, out, _ = run()
        self.assertEqual(code, 0)
        self.assertIn("2 things to do", out.splitlines()[0])
        self.assertIn("claude: primary needs a login", out)
        self.assertIn("on, 5 agents and skills shared", out)
        self.assertRegex(out, r"Jobs +none yet")
        self.assertRegex(out, r"Log in to codex as primary +agt accounts login primary --runtime codex")
        self.assertIn("`agt --help` lists every command", out)

    def test_all_good_has_no_next_steps(self):
        run("share", *YES)
        with patch.object(get_runtime("claude").__class__, "auth_status", return_value={"loggedIn": True}), \
                patch.object(get_runtime("codex").__class__, "auth_status", return_value={"loggedIn": True}):
            code, out, _ = run()
        self.assertEqual(code, 0)
        self.assertIn("✓ all good, nothing needs you", out)
        self.assertNotIn("Next", out)

    def test_a_login_check_that_runs_over_shows_not_checked(self):
        run("share", *YES)

        def slow(*args, **kwargs):
            time.sleep(2)
            return {"loggedIn": True}

        with patch.object(home, "LOGIN_SECONDS", 0.2), patch.object(get_runtime("codex").__class__, "auth_status", slow):
            started = time.monotonic()
            code, out, _ = run()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(code, 0)
        self.assertIn("codex: primary not checked", out)

    def test_agt_share_alone_shows_the_status_then_the_common_commands(self):
        code, out, _ = run("share")
        self.assertEqual(code, 0)
        self.assertIn("not set up yet", out)
        self.assertNotIn("What goes where", out)
        run("share", *YES)
        code, out, _ = run("share")
        self.assertEqual(code, 0)
        _, status, _ = run("share", "status")
        self.assertTrue(out.startswith(status), out)
        self.assertRegex(out, r"What goes where +agt share pick\n")
        self.assertRegex(out, r"Every command +agt share --help")

    def test_agt_help_is_help(self):
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["help", "share"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Usage  agt share <command> [options]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
