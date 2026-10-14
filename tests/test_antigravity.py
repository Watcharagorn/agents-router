import json
import os
from pathlib import Path
import sys
import unittest

from helpers import TempHome

from cli_subagents import jobs
from cli_subagents.config import load_config
from cli_subagents.runtimes.antigravity import AntigravityParser

FAKE_AGY = Path(__file__).with_name("fake_agy.py")


class ParserTests(unittest.TestCase):
    def test_text_deltas_and_quota(self):
        parser = AntigravityParser()
        parser.feed({"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "text_delta": "O"}})
        parser.feed({"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "text_delta": "K"}})
        self.assertEqual(parser.last_text, "OK")
        parser.feed({"event": "result", "result": {"status": "ERROR", "error": "Resource exhausted"}})
        self.assertEqual(parser.finish(1, "").outcome, "usage_limit")


class AntigravityJobTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["CLI_SUBAGENTS_AGY_COMMAND"] = json.dumps([sys.executable, str(FAKE_AGY)])

    def tearDown(self):
        os.environ.pop("CLI_SUBAGENTS_AGY_COMMAND", None)
        os.environ.pop("FAKE_AGY_SCENARIO", None)
        super().tearDown()

    def test_job_runs_on_the_signed_in_account(self):
        status = jobs.wait_for(jobs.create_job(runtime="antigravity", prompt="task", cwd=str(self.project), model="gemini-test")["job_id"], 60)
        self.assertEqual((status["outcome"], status["account"]), ("success", "primary"), status)
        self.assertEqual(jobs.read_result(status["job_id"])["result"], "gemini done")
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertTrue(argv[-1].startswith("--print=") and argv[-1].endswith("task"))

    def test_read_access_uses_plan_mode_and_resume(self):
        first = jobs.wait_for(jobs.create_job(runtime="antigravity", prompt="a", cwd=str(self.project), access="read")["job_id"], 60)
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        jobs.wait_for(jobs.create_job(runtime="antigravity", prompt="b", cwd=str(self.project), resume_job=first["job_id"])["job_id"], 60)
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-1")

    def test_extra_accounts_are_rejected(self):
        (self.home / "config.toml").write_text('[[antigravity.accounts]]\nname = "x"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config()


if __name__ == "__main__":
    unittest.main()
