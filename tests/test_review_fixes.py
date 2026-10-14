"""Regression tests for problems found in code review."""

import json
import os
from pathlib import Path
import time
import unittest

from helpers import TempHome

from cli_subagents import jobs, sync, tools
from cli_subagents.config import load_config
from cli_subagents.fsutil import make_dir_link
from cli_subagents.runtimes.claude import SYNC_PLAN, ClaudeParser, ClaudeRuntime, is_auth_env


class SyncSafetyTests(TempHome):
    def setUp(self):
        super().setUp()
        self.target = self.root / "account"
        self.target.mkdir()
        (self.primary / "skills" / "alpha").mkdir(parents=True)
        (self.primary / "commands").mkdir()
        (self.primary / "commands" / "x.md").write_text("cmd", encoding="utf-8")

    def run_sync(self):
        report = sync.new_report()
        sync.sync(self.primary, self.target, SYNC_PLAN, report)
        return report

    def test_manifest_cannot_point_outside_the_account(self):
        victim = self.root / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("keep", encoding="utf-8")
        (self.target / sync.MANIFEST).write_text(
            json.dumps({"items": {"../victim": {"kind": "copydir"}, "C:/victim": {"kind": "copy"}}}), encoding="utf-8"
        )
        self.run_sync()
        self.assertEqual((victim / "keep.txt").read_text(), "keep")

    def test_linked_folder_in_account_is_not_written_through(self):
        outside = self.root / "outside-commands"
        outside.mkdir()
        make_dir_link(outside, self.target / "commands")
        report = self.run_sync()
        self.assertFalse((outside / "x.md").exists())
        self.assertTrue(any(item.startswith("commands") for item in report["conflicts"]))

    def test_stale_entry_behind_linked_folder_is_not_removed(self):
        repo = self.root / "repo-skill"
        repo.mkdir()
        make_dir_link(repo, self.primary / "skills" / "foo")
        # The account's whole skills folder points at the primary, and the manifest
        # remembers an old link entry with the same name.
        make_dir_link(self.primary / "skills", self.target / "skills")
        (self.target / sync.MANIFEST).write_text(
            json.dumps({"items": {"skills/foo": {"kind": "link"}, "skills/gone": {"kind": "link"}}}), encoding="utf-8"
        )
        (self.primary / "skills" / "foo").exists()
        self.run_sync()
        self.assertTrue((self.primary / "skills" / "foo").exists())

    def test_changed_copied_folder_is_kept(self):
        target_dir = self.target / "skills" / "alpha"
        target_dir.mkdir(parents=True)
        (target_dir / "mine.txt").write_text("mine", encoding="utf-8")
        (self.target / sync.MANIFEST).write_text(
            json.dumps({"items": {"skills/alpha": {"kind": "copydir", "signature": "old"}}}), encoding="utf-8"
        )
        (self.primary / "skills" / "alpha").rmdir()
        self.run_sync()
        self.assertEqual((target_dir / "mine.txt").read_text(), "mine")


class ClassificationTests(unittest.TestCase):
    def test_max_turns_wins_over_words_in_the_answer(self):
        parser = ClaudeParser()
        parser.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "when the usage limit is reached"}]}})
        parser.feed({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 999}})
        parser.feed({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})
        result = parser.finish(1, "")
        self.assertEqual(result.outcome, "max_turns")
        self.assertIsNone(result.resets_at)

    def test_model_text_about_limits_is_not_a_limit(self):
        parser = ClaudeParser()
        parser.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "You've hit your session limit"}]}})
        self.assertEqual(parser.finish(1, "crashed").outcome, "error")


class OverlayEnvTests(unittest.TestCase):
    def test_auth_env_rules(self):
        for key in ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_OAUTH_TOKEN"):
            self.assertTrue(is_auth_env(key), key)
        # Tool API keys and everything else are part of the shared setup.
        for key in ("ANTHROPIC_MODEL", "CLAUDE_CODE_PLUGIN_DIRS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "GITHUB_TOKEN", "TAVILY_API_KEY", "MY_FLAG"):
            self.assertFalse(is_auth_env(key), key)


class RunnerRobustnessTests(TempHome):
    def test_malformed_events_do_not_break_the_job(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_SCENARIO"] = "badevent"
        status = jobs.wait_for(jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))["job_id"], 60)
        self.assertEqual((status["state"], status["outcome"]), ("done", "success"))

    def test_cancel_while_queued(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_SCENARIO"] = "slow"
        first = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))
        second = jobs.create_job(runtime="claude", prompt="y", cwd=str(self.project))
        deadline = time.time() + 30
        while jobs.read_status(second["job_id"]).get("message") != "waiting for a free account slot" and time.time() < deadline:
            time.sleep(0.3)
        self.assertEqual(jobs.cancel(second["job_id"])["outcome"], "cancelled")
        self.assertEqual(jobs.cancel(first["job_id"])["outcome"], "cancelled")

    def test_bad_argument_types_become_tool_errors(self):
        self.write_config()
        with self.assertRaises(tools.ToolError):
            tools.call("subagent_start", {"prompt": "x", "cwd": str(self.project), "add_dirs": 5})


if __name__ == "__main__":
    unittest.main()
