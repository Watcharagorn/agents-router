"""Kiro CLI as a worker runtime, against tests/fake_kiro.py only (never the real kiro-cli, never `login`)."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import config, jobs
from agents_router.runtimes import base, get_runtime, kiro

FAKE = Path(__file__).with_name("fake_kiro.py")


def spec(**fields):
    values = {"job_id": "j1", "runtime": "kiro", "prompt": "Count the files.", "cwd": str(Path.cwd()), "access": "read"}
    return base.JobSpec(**{**values, **fields})


class KiroUnitTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name)
        self._env = patch.dict(os.environ, {"AGT_KIRO_COMMAND": json.dumps([sys.executable, str(FAKE)])})
        self._env.start()
        self.runtime = get_runtime("kiro")
        self.config = config.load_config().runtime("kiro")
        self.account = self.config.primary_account()

    def tearDown(self):
        self._env.stop()
        self._temp.cleanup()

    def test_tools_follow_access_and_the_engine(self):
        self.assertEqual(kiro.access_args("read", "v2"), ["--trust-tools=fs_read,glob,grep"])
        self.assertEqual(kiro.access_args("edit", "v2"), ["--trust-tools=fs_read,glob,grep,fs_write"])
        self.assertEqual(kiro.access_args("edit", "v3"), ["--trust-tools=read,glob,grep,write"])
        self.assertEqual(kiro.access_args("full", "v3"), ["--trust-all-tools"])
        for access in ("read", "edit"):
            self.assertNotIn("execute_bash", kiro.access_args(access, "v2")[0])  # no shell without a gate
            self.assertNotIn("shell", kiro.access_args(access, "v3")[0])

    def test_engine_is_read_from_kiros_settings(self):
        self.assertEqual(kiro.engine(self.home), "v2")
        (self.home / "settings").mkdir()
        (self.home / "settings" / "cli.json").write_text(json.dumps({"chat.agentEngine": "v3"}), encoding="utf-8")
        self.assertEqual(kiro.engine(self.home), "v3")

    def test_launch(self):
        launch = self.runtime.launch(self.config, self.account, spec(model="claude-sonnet", effort="high"), "Count the files.", {})
        self.assertEqual(launch.argv[2:5], ["chat", "--no-interactive", "--wrap"])
        self.assertIn("--trust-tools=fs_read,glob,grep", launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("--model") + 1], "claude-sonnet")
        self.assertEqual(launch.argv[launch.argv.index("--effort") + 1], "high")
        self.assertNotIn("Count the files.", launch.argv)  # the task goes on stdin, never the command line
        self.assertTrue(launch.stdin.endswith("Count the files."))
        self.assertEqual((launch.env["AGT_JOB_ID"], launch.env["AGT_DEPTH"]), ("j1", "1"))
        edit = self.runtime.launch(self.config, self.account, spec(access="edit"), "x", {})
        self.assertIn("not run shell commands", edit.warnings[0])

    def test_answers_and_failures(self):
        parser = self.runtime.new_parser()
        for line in ("Looked at it.", "Done: 12 files."):
            parser.feed({"type": "text", "text": line})
        result = parser.finish(0, "")
        self.assertEqual((result.outcome, result.text), (base.SUCCESS, "Looked at it.\nDone: 12 files."))
        self.assertEqual(kiro.KiroParser().finish(1, "You have reached the usage limit for this month.").outcome, base.USAGE_LIMIT)
        self.assertEqual(kiro.KiroParser().finish(1, "error: Not logged in. Run kiro-cli login").outcome, base.AUTH)
        failed = kiro.KiroParser().finish(3, "")
        self.assertEqual(failed.outcome, base.ERROR)
        self.assertIn("MCP server didn't start", failed.message)
        self.assertEqual(kiro.KiroParser().finish(0, "").outcome, base.ERROR)  # no answer at all

    def test_login_status_is_read_only(self):
        self.assertTrue(self.runtime.auth_status(self.config, self.account)["loggedIn"])
        with patch.dict(os.environ, {"FAKE_KIRO_LOGGED_OUT": "1"}):
            self.assertFalse(self.runtime.auth_status(self.config, self.account)["loggedIn"])
        self.assertEqual(self.runtime.version(self.config), "kiro-cli 2.30.0")

    def test_one_account_only(self):
        from agents_router import setup

        self.assertEqual(self.config.primary, Path.home() / ".kiro")
        with self.assertRaises(ValueError) as caught:
            setup.register_account(config.load_config(), "kiro", "work", self.home / "work")
        self.assertIn("one login", str(caught.exception))


class KiroJobTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["AGT_KIRO_COMMAND"] = json.dumps([sys.executable, str(FAKE)])

    def tearDown(self):
        for key in ("AGT_KIRO_COMMAND", "FAKE_KIRO_SCENARIO"):
            os.environ.pop(key, None)
        super().tearDown()

    def test_a_job_runs_and_answers(self):
        status = jobs.wait_for(jobs.create_job(runtime="kiro", prompt="count the files", cwd=str(self.project), access="read")["job_id"], 60)
        self.assertEqual((status["state"], status["outcome"]), ("done", "success"), status)
        self.assertEqual(jobs.read_result(status["job_id"])["result"], "Looked at it.\nDone: count the files")
        record = self.records()[-1]
        self.assertIn("--trust-tools=fs_read,glob,grep", record["argv"])
        self.assertIn("count the files", record["stdin"])

    def test_a_usage_limit_is_reported_as_one(self):
        os.environ["FAKE_KIRO_SCENARIO"] = "limit"
        status = jobs.wait_for(jobs.create_job(runtime="kiro", prompt="t", cwd=str(self.project), access="read")["job_id"], 60)
        self.assertEqual(status["outcome"], "usage_limit", status)


if __name__ == "__main__":
    unittest.main()
