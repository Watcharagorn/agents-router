"""Regression tests for the second audit (orphaned workers, classification, permissions, ...)."""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import accounts, cli, jobs, runner, sync, tools
from agents_router.config import Account, jobs_dir, load_config
from agents_router.fsutil import make_private_dir, open_private, pid_alive, process_start_time, write_json
from agents_router.runtimes.antigravity import AntigravityParser
from agents_router.runtimes.base import clean_environment
from agents_router.runtimes.claude import SYNC_PLAN, ClaudeParser, ClaudeRuntime, _project_dir_name

FAKE_AGY = Path(__file__).with_name("fake_agy.py")


def dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def wait_until(check, seconds=30.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    return check()


class OrphanedWorkerTests(TempHome):
    """Item 1: a killed runner must not free its slot while its worker still runs."""

    def slot(self):
        config = load_config().runtime("claude")
        return config, config.find("a1")

    def owner_file(self):
        return accounts._owner_path(accounts._slot_path(*self.slot(), 0))

    def test_start_time_identifies_this_process(self):
        stamp = process_start_time(os.getpid())
        if os.name == "nt" or sys.platform.startswith("linux"):
            self.assertIsNotNone(stamp)
        self.assertEqual(stamp, process_start_time(os.getpid()))
        self.assertIsNone(process_start_time(dead_pid()))

    def test_slot_with_live_owner_is_busy(self):
        self.write_config(accounts=("a1",))
        config, account = self.slot()
        write_json(self.owner_file(), {"pid": os.getpid(), "started": process_start_time(os.getpid())})
        self.assertIsNone(accounts.try_lease(config, account))
        self.assertEqual(accounts.busy_slots(config, account), 1)

    def test_stale_owner_record_is_cleared(self):
        self.write_config(accounts=("a1",))
        config, account = self.slot()
        write_json(self.owner_file(), {"pid": dead_pid(), "started": 1})
        lease = accounts.try_lease(config, account)
        self.assertIsNotNone(lease)
        self.assertFalse(self.owner_file().exists())
        lease.release()

    def test_reused_pid_does_not_hold_the_slot(self):
        stamp = process_start_time(os.getpid())
        if stamp is None:
            self.skipTest("no process start times on this OS")
        self.write_config(accounts=("a1",))
        config, account = self.slot()
        write_json(self.owner_file(), {"pid": os.getpid(), "started": stamp + 1})
        lease = accounts.try_lease(config, account)
        self.assertIsNotNone(lease)
        lease.release()

    def test_killed_runner_keeps_slot_until_lost_job_stops_the_worker(self):
        self.write_config(accounts=("a1",), extra="max_jobs_per_account = 1")
        os.environ["FAKE_CLAUDE_SCENARIO"] = "slow"
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))["job_id"]
        status = wait_until(lambda: (lambda s: s if s.get("child_pid") else None)(jobs.read_status(job_id)))
        self.assertTrue(status and status.get("child_pid"), status)
        child = int(status["child_pid"])
        self.assertTrue(self.owner_file().exists())

        os.kill(int(status["runner_pid"]), getattr(signal, "SIGKILL", signal.SIGTERM))  # the runner only
        self.assertTrue(wait_until(lambda: not pid_alive(int(status["runner_pid"])), 10))
        config, account = self.slot()
        if os.name == "nt":
            self.assertTrue(wait_until(lambda: not pid_alive(child), 10), "runner death must close its worker Job Object")
            lease = accounts.try_lease(config, account)
            self.assertIsNotNone(lease)
            lease.release()
        else:
            self.assertIsNone(accounts.try_lease(config, account), "the orphaned worker still runs on a1")

        # Once the status is stale, reading it marks the job lost and stops the worker.
        raw = json.loads((jobs.job_dir(job_id) / "status.json").read_text(encoding="utf-8"))
        raw["updated_at"] = time.time() - 60
        (jobs.job_dir(job_id) / "status.json").write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(jobs.read_status(job_id)["outcome"], "lost")
        self.assertTrue(wait_until(lambda: not pid_alive(child), 15), "the orphaned worker was not stopped")
        lease = accounts.try_lease(config, account)
        self.assertIsNotNone(lease)
        lease.release()
        # cancel() acts on lost jobs too.
        self.assertEqual(jobs.cancel(job_id)["outcome"], "cancelled")
        self.assertEqual(jobs.read_status(job_id)["outcome"], "cancelled")

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_SCENARIO", None)
        super().tearDown()


class ClaudeClassificationTests(unittest.TestCase):
    """Items 4 and 8."""

    def parse(self, *events, stderr=""):
        parser = ClaudeParser()
        for event in events:
            parser.feed(event)
        return parser.finish(1, stderr)

    def test_429_alone_is_not_a_usage_limit(self):
        result = self.parse({"type": "result", "is_error": True, "api_error_status": 429, "result": "API Error: overloaded"})
        self.assertEqual(result.outcome, "error")

    def test_rejected_event_does_not_turn_an_unrelated_failure_into_a_limit(self):
        result = self.parse(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 123}},
            {"type": "result", "is_error": True, "api_error_status": 500, "result": "API Error: 500 Internal server error"},
        )
        self.assertEqual(result.outcome, "error")

    def test_rejected_event_with_a_limit_error_is_a_limit(self):
        result = self.parse(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 123}},
            {"type": "result", "is_error": True, "result": "Request rejected: rate limit"},
        )
        self.assertEqual((result.outcome, result.resets_at), ("usage_limit", 123.0))

    def test_failed_attempt_keeps_the_last_real_message(self):
        result = self.parse(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial work"}]}},
            {"type": "result", "is_error": True, "result": "You've hit your session limit · resets 3pm"},
        )
        self.assertEqual((result.outcome, result.text), ("usage_limit", "partial work"))

    def test_rate_limit_event_never_sets_a_cooldown(self):
        with TempHomeContext():
            accounts.record_rate_limit_event(
                "claude", "a1", {"status": "rejected", "resetsAt": time.time() + 3600, "rateLimitType": "five_hour", "utilization": 1.0}
            )
            record = accounts.load_usage("claude")["a1"]
            self.assertNotIn("cooldown_until", record)
            self.assertEqual(record["windows"]["five_hour"]["utilization"], 1.0)


class TempHomeContext:
    """A throwaway AGT_HOME for plain unit tests."""

    def __enter__(self):
        import tempfile

        self._temp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"AGT_HOME": self._temp.name})
        self._env.start()
        return Path(self._temp.name)

    def __exit__(self, *exc):
        self._env.stop()
        self._temp.cleanup()


class AntigravityTests(TempHome):
    """Items 5 and 8."""

    def setUp(self):
        super().setUp()
        os.environ["AGT_AGY_COMMAND"] = json.dumps([sys.executable, str(FAKE_AGY)])

    def tearDown(self):
        os.environ.pop("AGT_AGY_COMMAND", None)
        os.environ.pop("FAKE_AGY_SCENARIO", None)
        super().tearDown()

    def test_print_timeout_follows_the_job_timeout(self):
        jobs.wait_for(jobs.create_job(runtime="antigravity", prompt="t", cwd=str(self.project), timeout_seconds=600)["job_id"], 60)
        self.assertIn("--print-timeout=600s", self.records()[-1]["argv"])

    def test_quota_on_the_single_account_sets_no_cooldown(self):
        # With failover turned off; it's on by default, like for the other runtimes.
        (Path(os.environ["AGT_HOME"]) / "settings.toml").write_text('[antigravity]\nfailover = "off"\n', encoding="utf-8")
        os.environ["FAKE_AGY_SCENARIO"] = "quota"
        status = jobs.wait_for(jobs.create_job(runtime="antigravity", prompt="t", cwd=str(self.project))["job_id"], 60)
        self.assertEqual(status["outcome"], "usage_limit", status)
        self.assertNotIn("cooldown_until", accounts.load_usage("antigravity").get("primary", {}))

    def test_auth_needs_login_wording(self):
        def outcome(error):
            parser = AntigravityParser()
            parser.feed({"event": "step_update", "step_update": {"step_index": 1, "step_type": "agent_response", "text_delta": "so far"}})
            parser.feed({"event": "result", "result": {"status": "ERROR", "error": error}})
            result = parser.finish(1, "")
            self.assertEqual(result.text, "so far")
            return result.outcome

        self.assertEqual(outcome("tool failed: open the page and sign in to continue"), "error")
        self.assertEqual(outcome("Authentication required"), "auth")
        self.assertEqual(outcome("UNAUTHENTICATED: request had invalid credentials"), "auth")


class RunnerTests(TempHome):
    """Items 6, 8 and 11."""

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_SCENARIO", None)
        os.environ.pop("FAKE_CLAUDE_LIMITED", None)
        super().tearDown()

    def unstarted_job(self, prompt="x", timeout=None):
        job_id = jobs.create_job(runtime="claude", prompt=prompt, cwd=str(self.project), start=False)["job_id"]
        if timeout is not None:
            path = jobs.job_dir(job_id) / "spec.json"
            spec = json.loads(path.read_text(encoding="utf-8"))
            spec["timeout_seconds"] = timeout
            path.write_text(json.dumps(spec), encoding="utf-8")
        return job_id

    def test_timeout_is_enforced_while_the_prompt_is_written(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_SCENARIO"] = "nostdin"
        job_id = self.unstarted_job(prompt="x" * 4_000_000, timeout=2)
        started = time.monotonic()
        runner.main(job_id)
        self.assertLess(time.monotonic() - started, 40)
        self.assertEqual(jobs.read_status(job_id)["outcome"], "timeout")

    def test_failed_heartbeats_do_not_end_the_job(self):
        self.write_config(accounts=("a1",))
        job_id = self.unstarted_job()
        real = jobs.update_status
        final_failures = []

        def flaky(job, **fields):
            if fields.get("state") != "done":
                raise PermissionError("status.json is locked")
            if len(final_failures) < 2:
                final_failures.append(1)
                raise PermissionError("status.json is locked")
            return real(job, **fields)

        with patch.object(runner, "update_status", side_effect=flaky):
            self.assertEqual(runner.main(job_id), 0)
        self.assertEqual(jobs.read_status(job_id)["outcome"], "success")

    def test_limited_attempt_does_not_report_the_limit_text_as_its_result(self):
        self.write_config(accounts=("a1",), failover="off")
        os.environ["FAKE_CLAUDE_LIMITED"] = "a1"
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))["job_id"]
        status = jobs.wait_for(job_id, 60)
        self.assertEqual(status["outcome"], "usage_limit")
        self.assertEqual(jobs.read_result(job_id)["result"], "")


class PermissionTests(TempHome):
    """Item 7."""

    def test_private_folders_and_logs(self):
        folder = self.root / "a" / "b"
        make_private_dir(folder)
        with open_private(folder / "log.txt") as handle:
            handle.write(b"x\n")
        with open_private(folder / "log.txt") as handle:
            handle.write(b"y\n")
        self.assertEqual((folder / "log.txt").read_bytes(), b"x\ny\n")
        self.write_config()
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)["job_id"]
        if os.name != "nt":
            for path in (self.root / "a", folder, jobs_dir(), jobs.job_dir(job_id)):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path)
            self.assertEqual(stat.S_IMODE((folder / "log.txt").stat().st_mode), 0o600)


class EnvironmentTests(unittest.TestCase):
    """Item 9."""

    def test_openai_endpoint_and_org_are_removed(self):
        source = {key: "x" for key in ("OPENAI_BASE_URL", "OPENAI_ORGANIZATION", "OPENAI_PROJECT", "OPENAI_API_BASE")}
        self.assertEqual(clean_environment({**source, "PATH": "p"}), {"PATH": "p"})


class LinkSafetyTests(TempHome):
    """Item 10."""

    def test_session_transfer_never_writes_through_a_hard_link(self):
        session = "0123456789abcdef"
        name = _project_dir_name(str(self.project))
        source = Account("a1", self.root / "homes" / "a1")
        target = Account("a2", self.root / "homes" / "a2")
        (source.home / "projects" / name).mkdir(parents=True)
        (source.home / "projects" / name / f"{session}.jsonl").write_text("new", encoding="utf-8")
        (target.home / "projects" / name).mkdir(parents=True)
        victim = self.root / "victim.txt"
        victim.write_text("victim", encoding="utf-8")
        os.link(victim, target.home / "projects" / name / f"{session}.jsonl")
        self.assertTrue(ClaudeRuntime().transfer_session(source, target, session, str(self.project)))
        self.assertEqual(victim.read_text(encoding="utf-8"), "victim")
        self.assertEqual((target.home / "projects" / name / f"{session}.jsonl").read_text(encoding="utf-8"), "new")

    def test_sync_copy_never_follows_a_planted_temporary_link(self):
        (self.primary / "CLAUDE.md").write_text("rules", encoding="utf-8")
        target = self.root / "account"
        target.mkdir()
        victim = self.root / "victim.md"
        try:
            os.symlink(victim, target / ".CLAUDE.md.agents-router.tmp")  # the old fixed name
        except OSError:
            self.skipTest("symlinks need extra rights on this system")
        report = sync.new_report()
        with patch("agents_router.sync.os.link", side_effect=OSError("cross-device")):
            sync.sync(self.primary, target, SYNC_PLAN, report)
        self.assertFalse(victim.exists())
        self.assertEqual((target / "CLAUDE.md").read_text(encoding="utf-8"), "rules")


class JobLifecycleTests(TempHome):
    """Item 12."""

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_DELAY", None)
        super().tearDown()

    def test_resume_of_an_active_job_is_refused(self):
        self.write_config(accounts=("a1",))
        os.environ["FAKE_CLAUDE_DELAY"] = "3"
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))["job_id"]
        wait_until(lambda: jobs.read_status(job_id).get("session_id"))
        with self.assertRaisesRegex(ValueError, "still running"):
            jobs.create_job(runtime="claude", prompt="more", cwd=str(self.project), resume_job=job_id)
        jobs.wait_for(job_id, 30)

    def test_accounts_reset_clears_a_cooldown(self):
        self.write_config()
        accounts.set_cooldown("claude", "a1", time.time() + 3600, "test")
        accounts.set_cooldown("claude", "primary", time.time() + 3600, "test")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["accounts", "reset", "a1"]), 0)
            self.assertEqual(cli.main(["accounts", "reset", "primary"]), 0)
            self.assertEqual(cli.main(["accounts", "reset", "nobody"]), 1)
        usage = accounts.load_usage("claude")
        self.assertIsNone(accounts.cooldown_until(usage["a1"]))
        self.assertIsNone(accounts.cooldown_until(usage["primary"]))

    def test_runner_that_cannot_start_ends_the_job(self):
        self.write_config()
        with patch.object(jobs, "spawn_detached", side_effect=OSError("no interpreter")):
            status = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project))
        self.assertEqual((status["state"], status["outcome"]), ("done", "error"))
        self.assertIn("no interpreter", jobs.read_status(status["job_id"])["message"])

    def test_old_lost_jobs_are_cleaned(self):
        self.write_config()
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)["job_id"]
        jobs.update_status(job_id, state="running", runner_pid=dead_pid())
        path = jobs.job_dir(job_id) / "status.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["updated_at"] = time.time() - 3 * 86400
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(jobs.clean_old_jobs(1), 1)
        self.assertFalse(jobs.job_dir(job_id).exists())


class HostLimitTests(TempHome):
    """Item 13."""

    def test_codex_host_waits_at_most_240_seconds(self):
        seen = []
        with patch.object(tools, "wait_any", side_effect=lambda ids, timeout, progress=None: seen.append(timeout) or {}):
            tools.call("subagent_wait", {"job_ids": ["x"], "timeout_seconds": 3600}, client="codex-mcp-client")
            tools.call("subagent_wait", {"job_ids": ["x"], "timeout_seconds": 3600}, client="claude-code")
        self.assertEqual(seen, [240, 3600])
        self.assertEqual(tools._wait({"wait_seconds": 600}, "Codex"), 240.0)
        self.assertEqual(tools._wait({"wait_seconds": 600}, "claude-code"), 600.0)

    def test_result_is_capped(self):
        self.write_config()
        job_id = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)["job_id"]
        (jobs.job_dir(job_id) / "result.md").write_text("r" * 100_000, encoding="utf-8")
        jobs.update_status(job_id, state="done", outcome="success")
        result = tools.call("subagent_result", {"job_id": job_id, "max_chars": 200_000})
        self.assertEqual(len(result["result"]), 80_000)
        self.assertTrue(result["result_truncated"])

    def test_doctor_live_model_per_runtime(self):
        self.write_config()
        models = []

        def fake_create(**kwargs):
            models.append(kwargs.get("model"))
            return {"job_id": "j"}

        with patch.object(cli.tools, "account_report", return_value={"binary": "x", "accounts": []}), \
                patch.object(cli.jobs, "create_job", side_effect=fake_create), \
                patch.object(cli.jobs, "wait_for", return_value={"outcome": "success"}), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["doctor", "--live", "--runtime", "codex"])
            cli.main(["doctor", "--live", "--runtime", "codex", "--model", "gpt-x"])
            cli.main(["doctor", "--live", "--runtime", "claude"])
        self.assertEqual(models, [None, "gpt-x", "haiku", "haiku"])


if __name__ == "__main__":
    unittest.main()


class HomeOptionTests(unittest.TestCase):
    def test_account_home_is_not_taken_as_the_data_folder(self):
        import os, tempfile
        from pathlib import Path
        from unittest.mock import patch
        from agents_router import cli
        from agents_router.config import load_config
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "acct").mkdir()
            (root / "main").mkdir()
            # A throwaway primary, so the sync never links the real ~/.claude into the temp folder.
            env = {"AGT_HOME": str(root / "agt"), "CLAUDE_CONFIG_DIR": str(root / "main")}
            with patch.dict(os.environ, env):
                self.assertEqual(cli.main(["accounts", "add", "x", "--home", str(root / "acct"), "--no-login"]), 0)
                self.assertTrue((root / "agt" / "settings.toml").exists())
                self.assertFalse((root / "acct" / "settings.toml").exists())
                self.assertEqual([a.name for a in load_config().runtime("claude").accounts], ["x"])
