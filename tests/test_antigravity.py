import contextlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from cli_subagents import accounts, cli, jobs, setup, tools
from cli_subagents.config import Account, load_config
from cli_subagents.fsutil import is_link
from cli_subagents.runtimes.antigravity import AUTH_RE, AntigravityParser, AntigravityRuntime
from cli_subagents.runtimes.base import WORKER_TOOLS_NOTE, JobSpec

FAKE_AGY = Path(__file__).with_name("fake_agy.py")
TOKEN = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"
SSH_KEYS = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")


class ParserTests(unittest.TestCase):
    def test_text_deltas_and_quota(self):
        parser = AntigravityParser()
        parser.feed({"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "text_delta": "O"}})
        parser.feed({"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "text_delta": "K"}})
        self.assertEqual(parser.last_text, "OK")
        parser.feed({"event": "result", "result": {"status": "ERROR", "error": "Resource exhausted"}})
        self.assertEqual(parser.finish(1, "").outcome, "usage_limit")

    def test_login_phrases_count_as_auth(self):
        for text in (
            "You are not logged into Antigravity.",
            "Authentication expired. Please log back in.",
            "Authentication required. Please visit the URL to log in:",
        ):
            self.assertTrue(AUTH_RE.search(text), text)
            self.assertEqual(AntigravityParser().finish(1, text).outcome, "auth")

    def test_check_stderr_stops_on_login_prompt(self):
        runtime = AntigravityRuntime()
        extra = Account(name="g2", home=Path("unused"))
        self.assertIsNone(runtime.check_stderr("loading models\n", extra))
        result = runtime.check_stderr("x\nAuthentication required. Please visit the URL to log in:\nhttps://...", extra)
        self.assertEqual(result.outcome, "auth")
        self.assertIn("Authentication required", result.message)
        self.assertIn("csa accounts login g2 --runtime antigravity", result.message)


class AgyHome(TempHome):
    """TempHome plus the fake agy, a primary ~/.gemini and Antigravity accounts."""

    def setUp(self):
        super().setUp()
        os.environ["CLI_SUBAGENTS_AGY_COMMAND"] = json.dumps([sys.executable, str(FAKE_AGY)])
        for key in (*SSH_KEYS, "FAKE_AGY_SCENARIO", "FAKE_AGY_LIMITED"):
            os.environ.pop(key, None)
        self.gemini = self.root / "gemini"
        (self.gemini / "antigravity-cli").mkdir(parents=True)
        self.homes = self.root / "agy-homes"

    def tearDown(self):
        for key in ("CLI_SUBAGENTS_AGY_COMMAND", "FAKE_AGY_SCENARIO", "FAKE_AGY_LIMITED"):
            os.environ.pop(key, None)
        super().tearDown()

    def write_agy_config(self, accounts=("a1",), failover="off", extra=""):
        lines = [
            "[antigravity]",
            f"primary = {json.dumps(str(self.gemini / 'antigravity-cli'))}",
            f'failover = "{failover}"',
            extra,
        ]
        for name in accounts:
            lines += ["[[antigravity.accounts]]", f'name = "{name}"', f"home = {json.dumps(str(self.homes / name))}"]
        (self.home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def log_in(self, name):
        token = self.homes / name / TOKEN
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("fake", encoding="utf-8")

    def runtime_config(self):
        return load_config().runtime("antigravity")

    def run_job(self, **kwargs):
        status = jobs.create_job(runtime="antigravity", prompt=kwargs.pop("prompt", "task"), cwd=str(self.project), **kwargs)
        return jobs.wait_for(status["job_id"], 90)


class ConfigAndEnvTests(AgyHome):
    def test_rotation_is_on_by_default(self):
        lines = ["[antigravity]", f"primary = {json.dumps(str(self.gemini / 'antigravity-cli'))}"]
        for name in ("a1", "a2"):
            lines += ["[[antigravity.accounts]]", f'name = "{name}"', f"home = {json.dumps(str(self.homes / name))}"]
        (self.home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        config = self.runtime_config()
        self.assertEqual((config.failover, config.include_primary), ("on-limit", True))
        self.assertEqual([account.name for account in config.worker_accounts()], ["primary", "a1", "a2"])

    def test_failover_off_keeps_unpinned_jobs_on_the_primary(self):
        self.write_agy_config(accounts=("a1", "a2"))
        config = self.runtime_config()
        self.assertEqual((config.failover, config.include_primary), ("off", True))
        self.assertEqual([account.name for account in config.worker_accounts()], ["primary"])
        self.assertEqual([account.name for account in config.pool_accounts()], ["primary", "a1", "a2"])
        self.write_agy_config(accounts=("a1", "a2"), failover="on-limit")
        self.assertEqual([account.name for account in self.runtime_config().worker_accounts()], ["primary", "a1", "a2"])

    def test_primary_env_is_unchanged_and_extra_env_uses_a_file_login(self):
        self.write_agy_config()
        config = self.runtime_config()
        runtime = AntigravityRuntime()
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k", "GOOGLE_APPLICATION_CREDENTIALS": "c"}):
            primary = runtime.account_env(config, config.primary_account())
            extra = runtime.account_env(config, config.find("a1"))
        self.assertEqual(primary.get("HOME"), os.environ.get("HOME"))
        self.assertEqual(primary.get("USERPROFILE"), os.environ.get("USERPROFILE"))
        self.assertEqual(primary.get("GEMINI_API_KEY"), "k")
        self.assertFalse(any(key in primary for key in SSH_KEYS))
        folder = str(self.homes / "a1")
        self.assertEqual((extra["HOME"], extra["USERPROFILE"]), (folder, folder))
        self.assertTrue(all(extra.get(key) for key in SSH_KEYS))
        self.assertNotIn("GEMINI_API_KEY", extra)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", extra)
        if os.name != "nt":
            self.assertTrue(extra["XDG_CONFIG_HOME"].startswith(folder))

    def test_logged_in_check_by_file_name(self):
        self.write_agy_config()
        runtime = AntigravityRuntime()
        account = self.runtime_config().find("a1")
        self.assertFalse(runtime.logged_in(account))
        self.assertFalse(runtime.auth_status(self.runtime_config(), account)["loggedIn"])
        other = self.homes / "a1" / ".gemini" / "antigravity-cli" / "auth" / "antigravity-oauth-token.json"
        other.parent.mkdir(parents=True)
        other.write_text("x", encoding="utf-8")
        self.assertTrue(runtime.logged_in(account))
        other.unlink()
        self.log_in("a1")
        self.assertTrue(runtime.auth_status(self.runtime_config(), account)["loggedIn"])


class SharedSetupTests(AgyHome):
    def setUp(self):
        super().setUp()
        g = self.gemini
        (g / "GEMINI.md").write_text("rules", encoding="utf-8")
        (g / "config" / "rules").mkdir(parents=True)
        (g / "config" / "rules" / "r.md").write_text("r", encoding="utf-8")
        (g / "config" / "skills" / "s1").mkdir(parents=True)
        (g / "config" / "skills" / "s1" / "SKILL.md").write_text("s", encoding="utf-8")
        (g / "config" / "hooks.json").write_text("{}", encoding="utf-8")
        (g / "config" / "config.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        (g / "antigravity-cli" / "settings.json").write_text(json.dumps({"modelProvider": "vertex"}), encoding="utf-8")
        for plugin in ("p1", "cli-subagents"):
            (g / "antigravity-cli" / "plugins" / plugin).mkdir(parents=True)
        servers = {"tool": {"command": "node", "args": ["tool.js"]}, "cli-subagents": {"command": "csa", "args": ["mcp"]}}
        (g / "config" / "mcp_config.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        self.write_agy_config()

    def prepare(self, job_id="job-1", config=None):
        config = config or self.runtime_config()
        spec = JobSpec(job_id=job_id, runtime="antigravity", prompt="", cwd=str(self.project))
        return AntigravityRuntime().prepare(config, config.find("a1"), spec)

    def test_links_shared_setup_and_skips_identity_settings(self):
        report = self.prepare()
        account = self.homes / "a1" / ".gemini"
        self.assertTrue(os.path.samefile(account / "GEMINI.md", self.gemini / "GEMINI.md"))
        # hooks.json and settings.json are written for the folder: the user's content plus the review gate.
        hooks = json.loads((account / "config" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("cli-subagents-review", hooks)
        self.assertTrue(report["review_gate"])
        self.assertTrue((account / "config" / "config.json").is_file())
        self.assertTrue((account / "config" / "rules" / "r.md").is_file())
        self.assertTrue((account / "config" / "skills" / "s1" / "SKILL.md").is_file())
        self.assertTrue((account / "antigravity-cli" / "plugins" / "p1").is_dir())
        self.assertFalse((account / "antigravity-cli" / "plugins" / "cli-subagents").exists())
        settings = json.loads((account / "antigravity-cli" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings, {"toolPermission": "request-review"})  # modelProvider is not shared
        self.assertTrue(any("modelProvider" in item for item in report["warnings"]), report)
        self.assertIn("config/rules", report["linked"])
        # The primary setup itself is never changed.
        self.assertFalse(is_link(self.gemini / "config" / "rules"))
        # A later sync removes what it no longer shares, and only that.
        (self.gemini / "config" / "skills" / "s1" / "SKILL.md").unlink()
        (self.gemini / "config" / "skills" / "s1").rmdir()
        report = self.prepare()
        self.assertIn("config/skills/s1", report["removed"])
        self.assertTrue((account / "config" / "rules" / "r.md").is_file())

    def test_mcp_config_gets_the_parent_channel_per_job(self):
        report = self.prepare("job-7")
        self.assertTrue(report["parent_channel"])
        path = self.homes / "a1" / ".gemini" / "config" / "mcp_config.json"
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(sorted(servers), ["csa-parent", "tool"])
        self.assertEqual(servers["csa-parent"]["args"][-2:], ["--worker", "job-7"])
        self.assertFalse(self.prepare("probe")["parent_channel"])
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(sorted(servers), ["tool"])
        # The primary file stays as the user wrote it.
        primary = json.loads((self.gemini / "config" / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertNotIn("csa-parent", primary["mcpServers"])

    def test_no_channel_when_an_account_runs_several_jobs(self):
        self.write_agy_config(extra="max_jobs_per_account = 2")
        self.assertFalse(self.prepare("job-8")["parent_channel"])

    def test_existing_mcp_config_is_kept_once(self):
        path = self.homes / "a1" / ".gemini" / "config" / "mcp_config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"mcpServers": {"mine": {"command": "x"}}}), encoding="utf-8")
        self.prepare()
        backup = path.with_name("mcp_config.json.before-cli-subagents")
        self.assertIn("mine", backup.read_text(encoding="utf-8"))

class AntigravityJobTests(AgyHome):
    def test_job_runs_on_the_signed_in_account(self):
        status = self.run_job(model="gemini-test")
        self.assertEqual((status["outcome"], status["account"]), ("success", "primary"), status)
        self.assertEqual(jobs.read_result(status["job_id"])["result"], "gemini done")
        record = self.records()[-1]
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertTrue(argv[-1].startswith("--print=") and argv[-1].endswith("task"))
        # The primary runs in a folder CLI Subagents manages (keyring login), so it gets the parent channel.
        self.assertIn(WORKER_TOOLS_NOTE.strip(), argv[-1])
        self.assertEqual(Path(record["env"]["HOME"]), self.home / "homes" / "antigravity" / "primary")
        self.assertNotIn("SSH_CONNECTION", record["env"])
        self.assertFalse((self.gemini / "config" / "hooks.json").exists())  # the user's own ~/.gemini is left alone

    def test_read_access_uses_plan_mode_and_resume(self):
        first = self.run_job(prompt="a", access="read")
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.run_job(prompt="b", resume_job=first["job_id"])
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-1")

    def test_failover_off_runs_unpinned_jobs_on_the_primary(self):
        self.write_agy_config(accounts=("a1",))
        self.log_in("a1")
        self.assertEqual(self.run_job()["account"], "primary")

    def test_pinned_extra_account_runs_in_its_own_home_with_a_channel(self):
        self.write_agy_config(accounts=("a1",))
        self.log_in("a1")
        status = self.run_job(account="a1")
        self.assertEqual((status["outcome"], status["account"]), ("success", "a1"), status)
        record = self.records()[-1]
        folder = str(self.homes / "a1")
        self.assertEqual((record["env"]["HOME"], record["env"]["USERPROFILE"]), (folder, folder))
        self.assertTrue(all(record["env"].get(key) for key in SSH_KEYS))
        self.assertIn(WORKER_TOOLS_NOTE.strip(), record["argv"][-1])
        config = json.loads((self.homes / "a1" / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["mcpServers"]["csa-parent"]["args"][-1], status["job_id"])

    def test_extra_account_without_login_ends_at_once(self):
        self.write_agy_config(accounts=("a1",))
        started = time.monotonic()
        status = self.run_job(account="a1")
        self.assertEqual(status["outcome"], "auth", status)
        self.assertIn("csa accounts login a1 --runtime antigravity", status["message"])
        self.assertEqual(self.records(), [])  # agy never started
        self.assertLess(time.monotonic() - started, 30)

    def test_login_prompt_on_stderr_stops_the_run_early(self):
        os.environ["FAKE_AGY_SCENARIO"] = "login_prompt"  # the fake then waits 60 s like agy
        started = time.monotonic()
        status = self.run_job()
        self.assertEqual(status["outcome"], "auth", status)
        self.assertIn("Authentication required", status["message"])
        self.assertLess(time.monotonic() - started, 40)

    def test_login_prompt_for_an_extra_account_names_the_login_command(self):
        self.write_agy_config(accounts=("a1",))
        # A tolerant check passes on a fallback name, but agy still wants a login.
        (self.homes / "a1" / ".gemini").mkdir(parents=True)
        (self.homes / "a1" / ".gemini" / "google_accounts.json").write_text("{}", encoding="utf-8")
        status = self.run_job(account="a1")
        self.assertEqual(status["outcome"], "auth", status)
        self.assertIn("csa accounts login a1 --runtime antigravity", status["message"])

    def test_on_limit_fails_over_with_the_partial_output(self):
        self.write_agy_config(accounts=("a1", "a2"), failover="on-limit", extra="include_primary = false")
        self.log_in("a1")
        self.log_in("a2")
        os.environ["FAKE_AGY_LIMITED"] = "a1"
        status = self.run_job()
        self.assertEqual((status["outcome"], status["account"]), ("success", "a2"), status)
        self.assertEqual([attempt["account"] for attempt in status["attempts"]], ["a1", "a2"])
        self.assertIn("half done on a1", self.records()[-1]["argv"][-1])
        self.assertIn("cooldown_until", accounts.load_usage("antigravity")["a1"])
        # The next unpinned job skips the cooling account.
        os.environ.pop("FAKE_AGY_LIMITED")
        self.assertEqual(self.run_job()["account"], "a2")

    def test_on_limit_skips_an_account_without_login(self):
        self.write_agy_config(accounts=("a1", "a2"), failover="on-limit", extra="include_primary = false")
        self.log_in("a2")
        status = self.run_job()
        self.assertEqual((status["outcome"], status["account"]), ("success", "a2"), status)
        self.assertTrue(any("a1" in warning for warning in status["warnings"]), status)

    def test_quota_on_primary_without_failover_sets_no_cooldown(self):
        self.write_agy_config(accounts=(), failover="off")
        os.environ["FAKE_AGY_SCENARIO"] = "quota"
        self.assertEqual(self.run_job()["outcome"], "usage_limit")
        self.assertNotIn("cooldown_until", accounts.load_usage("antigravity").get("primary", {}))


class AccountCommandTests(AgyHome):
    def csa(self, *argv):
        output = io.StringIO()
        with patch("cli_subagents.cli.subprocess.call", return_value=0) as call, contextlib.redirect_stdout(output):
            code = cli.main(list(argv))
        return code, output.getvalue(), call

    def test_add_prints_the_risk_note_and_opens_the_login_in_the_account_env(self):
        self.write_agy_config(accounts=())
        code, out, call = self.csa("accounts", "add", "g2", "--runtime", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertIn("own risk", out)
        self.assertIn("antigravity.google/docs/faq", out)
        folder = Path(self.home / "homes" / "antigravity" / "g2")
        self.assertEqual([account.name for account in self.runtime_config().accounts], ["g2"])
        argv, kwargs = call.call_args[0][0], call.call_args[1]
        self.assertEqual(argv, json.loads(os.environ["CLI_SUBAGENTS_AGY_COMMAND"]))
        self.assertEqual(Path(kwargs["env"]["HOME"]), folder)
        self.assertEqual(Path(kwargs["env"]["USERPROFILE"]), folder)
        self.assertTrue(kwargs["env"].get("SSH_CONNECTION"))
        self.assertEqual(Path(kwargs["cwd"]), folder)
        self.assertIn("paste the code", out)

    def test_login_disable_reset_and_no_login(self):
        self.write_agy_config(accounts=())
        code, out, call = self.csa("accounts", "add", "g3", "--runtime", "antigravity", "--no-login")
        self.assertEqual(code, 0, out)
        call.assert_not_called()
        self.assertIn("csa accounts login g3 --runtime antigravity", out)
        code, out, call = self.csa("accounts", "login", "g3", "--runtime", "antigravity")
        self.assertEqual(code, 0, out)
        self.assertTrue(call.call_args[1]["env"].get("SSH_TTY"))
        self.assertEqual(self.csa("accounts", "disable", "g3", "--runtime", "antigravity")[0], 0)
        self.assertFalse(self.runtime_config().find("g3").enabled)
        accounts.set_cooldown("antigravity", "g3", time.time() + 600, "limit")
        code, out, _call = self.csa("accounts", "reset", "g3", "--runtime", "antigravity")
        self.assertIn("cooldown cleared", out)

    def test_account_report_lists_every_account(self):
        self.write_agy_config(accounts=("a1", "a2"))
        self.log_in("a1")
        (self.home / "config.toml").write_text(
            (self.home / "config.toml").read_text(encoding="utf-8") + "enabled = false\n", encoding="utf-8"
        )
        report = tools.account_report("antigravity")
        entries = {entry["name"]: entry for entry in report["accounts"]}
        self.assertEqual(list(entries), ["primary", "a1", "a2"])
        self.assertTrue(entries["a1"]["auth"]["loggedIn"])
        self.assertFalse(entries["a2"]["auth"]["loggedIn"])
        self.assertFalse(entries["a2"]["enabled"])
        self.assertEqual(entries["a1"]["home"], str(self.homes / "a1"))

    def test_setup_registers_an_antigravity_folder(self):
        self.write_agy_config(accounts=())
        runtime, name, folder = setup.parse_account(f"antigravity:g4={self.root / 'g4'}")
        self.assertEqual((runtime, name), ("antigravity", "g4"))
        config = load_config()
        with self.assertRaises(ValueError):
            setup.register_account(config, "antigravity", "me", Path.home())
        self.assertEqual(setup.register_account(config, "antigravity", "g4", folder)[0], "added")


if __name__ == "__main__":
    unittest.main()