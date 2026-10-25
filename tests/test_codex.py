import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from helpers import TempHome

from agents_router import jobs
from agents_router.config import load_config
from agents_router.fsutil import is_link
from agents_router.runtimes.codex import CodexParser, CodexRuntime, self_server_overrides

FAKE_CODEX = Path(__file__).with_name("fake_codex.py")

CONFIG = """model = "gpt-test"

[mcp_servers.agents-router]
command = "agt"
args = ["mcp"]

[mcp_servers.agents-router.env]
X = "1"

[mcp_servers.docs]
command = "docs-server"

[features]
web_search = true
"""


class SelfServerTests(unittest.TestCase):
    def overrides(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return self_server_overrides([path])

    def test_own_servers_are_disabled_and_the_rest_kept(self):
        text = CONFIG + '\n[mcp_servers.nested]\ncommand = "python"\nargs = ["-m", "agents_router", "mcp"]\n'
        args = self.overrides(text)
        pairs = [args[index + 1] for index in range(0, len(args), 2)]
        self.assertTrue(all(args[index] == "-c" for index in range(0, len(args), 2)))
        self.assertIn("mcp_servers.agents-router.enabled=false", pairs)
        self.assertIn("mcp_servers.nested.enabled=false", pairs)
        self.assertFalse(any("docs" in pair for pair in pairs))
        # Both are defined in config.toml, so neither gets a stub command.
        self.assertFalse(any(pair.endswith(".command='agt'") for pair in pairs))

    def test_plugin_name_gets_a_stub_when_not_configured(self):
        # Codex 0.162.1 refuses `enabled=false` alone for an undefined server ("invalid transport").
        args = self.overrides('[mcp_servers.docs]\ncommand = "docs-server"\n')
        self.assertEqual(args, ["-c", "mcp_servers.agents-router.command='agt'", "-c", "mcp_servers.agents-router.enabled=false"])

    def test_unparseable_config_fails_clearly(self):
        with self.assertRaisesRegex(RuntimeError, "could not read Codex config"):
            self.overrides("[mcp_servers.broken\n")

    def test_dotted_self_server_name_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "rename"):
            self.overrides('[mcp_servers."agt.main"]\ncommand = "agt"\n')


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

    def test_completed_turn_wins_over_recovered_errors(self):
        parser = CodexParser()
        for event in (
            {"type": "error", "message": "Reconnecting... 429 Too Many Requests, retrying"},
            {"type": "item.completed", "item": {"type": "error", "message": "rate limit on a tool call"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "all done"}},
            {"type": "turn.completed", "usage": {}},
        ):
            parser.feed(event)
        result = parser.finish(0, "")
        self.assertEqual((result.outcome, result.text), ("success", "all done"))
        self.assertEqual(len(parser.progress()["warnings"]), 2)

    def test_errors_after_a_completed_turn_are_not_a_limit(self):
        parser = CodexParser()
        parser.feed({"type": "error", "message": "You've hit your usage limit."})
        parser.feed({"type": "turn.completed", "usage": {}})
        self.assertEqual(parser.finish(1, "").outcome, "error")
        # A failed turn still counts.
        parser.feed({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
        self.assertEqual(parser.finish(1, "").outcome, "usage_limit")


class CodexJobTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["AGT_CODEX_COMMAND"] = json.dumps([sys.executable, str(FAKE_CODEX)])
        (self.primary / "config.toml").write_text(CONFIG, encoding="utf-8")
        (self.primary / "skills" / "beta").mkdir(parents=True)
        (self.primary / "AGENTS.md").write_text("agents", encoding="utf-8")
        lines = ["[codex]", f"primary = {json.dumps(str(self.primary))}", 'failover = "on-limit"']
        for name in ("c1", "c2"):
            lines += ["[[codex.accounts]]", f'name = "{name}"', f"home = {json.dumps(str(self.root / 'codex' / name))}"]
        (self.home / "settings.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def tearDown(self):
        os.environ.pop("AGT_CODEX_COMMAND", None)
        os.environ.pop("FAKE_CODEX_LIMITED", None)
        super().tearDown()

    def test_shared_setup_failover_and_environment(self):
        os.environ["FAKE_CODEX_LIMITED"] = "c1"
        status = jobs.wait_for(jobs.create_job(runtime="codex", prompt="task", cwd=str(self.project), access="read")["job_id"], 60)
        self.assertEqual((status["state"], status["outcome"]), ("done", "success"), status)
        self.assertEqual([attempt["account"] for attempt in status["attempts"]], ["c1", "c2"])
        self.assertEqual(jobs.read_result(status["job_id"])["result"], "codex done on c2")

        home = self.root / "codex" / "c2"
        # The config is copied as is; the worker turns the agt server off with -c instead.
        self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), CONFIG)
        self.assertTrue((home / "skills" / "beta").exists())
        self.assertEqual((home / "AGENTS.md").read_text(), "agents")

        record = self.records()[-1]
        self.assertEqual(Path(record["env"]["CODEX_HOME"]), home)
        argv = record["argv"]
        self.assertIn("mcp_servers.agents-router.enabled=false", argv)
        self.assertIn("cli_auth_credentials_store=file", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertEqual(argv[-1], "-")
        self.assertIn("task", record["stdin"])

    def use_primary_only(self):
        (self.home / "settings.toml").write_text(f"[codex]\nprimary = {json.dumps(str(self.primary))}\n", encoding="utf-8")

    def test_primary_account_run_also_disables_the_own_server(self):
        self.use_primary_only()
        status = jobs.wait_for(jobs.create_job(runtime="codex", prompt="t", cwd=str(self.project), effort="max")["job_id"], 60)
        self.assertEqual((status["outcome"], status["account"]), ("success", "primary"), status)
        argv = self.records()[-1]["argv"]
        self.assertIn("mcp_servers.agents-router.enabled=false", argv)
        self.assertIn("model_reasoning_effort=xhigh", argv)

    def test_unparseable_config_fails_the_run(self):
        self.use_primary_only()
        (self.primary / "config.toml").write_text("[mcp_servers.broken\n", encoding="utf-8")
        status = jobs.wait_for(jobs.create_job(runtime="codex", prompt="t", cwd=str(self.project))["job_id"], 60)
        self.assertEqual(status["outcome"], "error", status)
        self.assertIn("could not read Codex config", status["message"])
        self.assertEqual(self.records(), [])

    def test_config_backup_never_writes_through_a_link(self):
        home = self.root / "codex" / "c1"
        home.mkdir(parents=True)
        (home / "config.toml").write_text("own = 1\n", encoding="utf-8")
        victim = self.root / "victim.toml"
        try:
            os.symlink(victim, home / "config.toml.before-agents-router")
        except OSError:
            self.skipTest("symlinks need extra rights on this system")
        config = load_config().runtime("codex")
        CodexRuntime().prepare(config, config.find("c1"), jobs.JobSpec(job_id="t", runtime="codex", prompt="", cwd=str(self.project)))
        self.assertFalse(victim.exists())
        self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), CONFIG)

    def prepare_account(self, name="c1"):
        config = load_config().runtime("codex")
        spec = jobs.JobSpec(job_id="t", runtime="codex", prompt="", cwd=str(self.project))
        return CodexRuntime().prepare(config, config.find(name), spec)

    def test_custom_agents_reach_extra_accounts(self):
        (self.primary / "agents").mkdir()
        (self.primary / "agents" / "reviewer.toml").write_text('name = "reviewer"\n', encoding="utf-8")
        report = self.prepare_account()
        home = self.root / "codex" / "c1"
        self.assertIn("agents", report["linked"], report)
        self.assertTrue(is_link(home / "agents"))
        self.assertEqual((home / "agents" / "reviewer.toml").read_text(encoding="utf-8"), 'name = "reviewer"\n')
        # One link to the primary folder, so an agent added later is there without another sync.
        (self.primary / "agents" / "planner.toml").write_text('name = "planner"\n', encoding="utf-8")
        self.assertTrue((home / "agents" / "planner.toml").is_file())

    def test_account_agents_are_kept_and_shared_ones_added(self):
        (self.primary / "agents").mkdir()
        (self.primary / "agents" / "reviewer.toml").write_text('name = "reviewer"\n', encoding="utf-8")
        own = self.root / "codex" / "c1" / "agents"
        own.mkdir(parents=True)
        (own / "mine.toml").write_text('name = "mine"\n', encoding="utf-8")
        self.prepare_account()
        self.assertFalse(is_link(own))
        self.assertEqual((own / "mine.toml").read_text(encoding="utf-8"), 'name = "mine"\n')
        self.assertEqual((own / "reviewer.toml").read_text(encoding="utf-8"), 'name = "reviewer"\n')

    def test_resume_uses_config_overrides(self):
        first = jobs.wait_for(jobs.create_job(runtime="codex", prompt="a", cwd=str(self.project), account="c2")["job_id"], 60)
        jobs.wait_for(jobs.create_job(runtime="codex", prompt="b", cwd=str(self.project), resume_job=first["job_id"])["job_id"], 60)
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[:3], ["exec", "resume", "thread-c2"])
        self.assertIn("approvals_reviewer=auto_review", argv)
        self.assertNotIn("-C", argv)


if __name__ == "__main__":
    unittest.main()
