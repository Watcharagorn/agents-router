import json
import os
from pathlib import Path
import sys
import unittest

from helpers import TempHome

from cli_subagents import jobs
from cli_subagents.config import load_config
from cli_subagents.runtimes.codex import CodexParser, CodexRuntime, strip_own_server

FAKE_CODEX = Path(__file__).with_name("fake_codex.py")

CONFIG = """model = "gpt-test"

[mcp_servers.cli-subagents]
command = "csa"
args = ["mcp"]

[mcp_servers.cli-subagents.env]
X = "1"

[mcp_servers.docs]
command = "docs-server"

[features]
web_search = true
"""


class StripTests(unittest.TestCase):
    def test_own_server_is_removed_and_the_rest_kept(self):
        result = strip_own_server(CONFIG)
        self.assertNotIn("csa", result)
        self.assertNotIn("X = \"1\"", result)
        self.assertIn("[mcp_servers.docs]", result)
        self.assertIn("[features]", result)
        self.assertIn('model = "gpt-test"', result)


class ParserTests(unittest.TestCase):
    def test_success_and_limit(self):
        parser = CodexParser()
        for event in (
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
            {"type": "turn.completed", "usage": {}},
        ):
            parser.feed(event)
        result = parser.finish(0, "")
        self.assertEqual((result.outcome, result.text, result.session_id), ("success", "OK", "t1"))

        parser = CodexParser()
        parser.feed({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
        self.assertEqual(parser.finish(1, "").outcome, "usage_limit")

        parser = CodexParser()
        parser.feed({"type": "error", "message": "Not logged in. Run codex login"})
        self.assertEqual(parser.finish(1, "").outcome, "auth")


class CodexJobTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["CLI_SUBAGENTS_CODEX_COMMAND"] = json.dumps([sys.executable, str(FAKE_CODEX)])
        (self.primary / "config.toml").write_text(CONFIG, encoding="utf-8")
        (self.primary / "skills" / "beta").mkdir(parents=True)
        (self.primary / "AGENTS.md").write_text("agents", encoding="utf-8")
        lines = ["[codex]", f"primary = {json.dumps(str(self.primary))}", 'failover = "on-limit"']
        for name in ("c1", "c2"):
            lines += ["[[codex.accounts]]", f'name = "{name}"', f"home = {json.dumps(str(self.root / 'codex' / name))}"]
        (self.home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def tearDown(self):
        os.environ.pop("CLI_SUBAGENTS_CODEX_COMMAND", None)
        os.environ.pop("FAKE_CODEX_LIMITED", None)
        super().tearDown()

    def test_shared_setup_failover_and_environment(self):
        os.environ["FAKE_CODEX_LIMITED"] = "c1"
        status = jobs.wait_for(jobs.create_job(runtime="codex", prompt="task", cwd=str(self.project), access="read")["job_id"], 60)
        self.assertEqual((status["state"], status["outcome"]), ("done", "success"), status)
        self.assertEqual([attempt["account"] for attempt in status["attempts"]], ["c1", "c2"])
        self.assertEqual(jobs.read_result(status["job_id"])["result"], "codex done on c2")

        home = self.root / "codex" / "c2"
        config = (home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.docs]", config)
        self.assertNotIn("cli-subagents", config)
        self.assertTrue((home / "skills" / "beta").exists())
        self.assertEqual((home / "AGENTS.md").read_text(), "agents")

        record = self.records()[-1]
        self.assertEqual(Path(record["env"]["CODEX_HOME"]), home)
        argv = record["argv"]
        self.assertIn("cli_auth_credentials_store=file", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertEqual(argv[-1], "-")
        self.assertIn("task", record["stdin"])

    def test_resume_uses_config_overrides(self):
        first = jobs.wait_for(jobs.create_job(runtime="codex", prompt="a", cwd=str(self.project), account="c2")["job_id"], 60)
        jobs.wait_for(jobs.create_job(runtime="codex", prompt="b", cwd=str(self.project), resume_job=first["job_id"])["job_id"], 60)
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[:3], ["exec", "resume", "thread-c2"])
        self.assertIn("approvals_reviewer=auto_review", argv)
        self.assertNotIn("-C", argv)


if __name__ == "__main__":
    unittest.main()
