"""Your own Claude Code profiles as accounts: found with no setup, never used by the session that runs in them."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from helpers import FAKE_CLAUDE, fake_launchd, stop_job_runners

from agents_router import accounts, hooks, jobs, profiles
from agents_router.config import load_config, set_session
from agents_router.runtimes.base import JobSpec
from agents_router.runtimes.claude import ClaudeRuntime

TEAM = {"accountUuid": "uuid-team", "organizationUuid": "org-team", "organizationType": "claude_team"}
MAX = {"accountUuid": "uuid-max", "organizationUuid": "org-max", "organizationType": "claude_max"}


class ProfileHome(unittest.TestCase):
    """A home folder with three Claude profiles: ~/.claude (Max), ~/.claude-ntb (Team), ~/.claude-personal (Max again)."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name).resolve()
        self.home = self.root / "home"
        self.agt = self.root / "agt"
        self.project = self.root / "project"
        for folder in (self.home, self.agt, self.project):
            folder.mkdir()
        self.record = self.root / "record.jsonl"
        self._env = patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home), "AGT_HOME": str(self.agt),
            "AGT_CLAUDE_COMMAND": json.dumps([sys.executable, str(FAKE_CLAUDE)]),
            "FAKE_CLAUDE_RECORD": str(self.record),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            **fake_launchd(self.root),
        })
        self._env.start()
        for key in ("CLAUDE_CONFIG_DIR", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AGT_DEPTH", "FAKE_CLAUDE_LIMITED"):
            os.environ.pop(key, None)
        self.default = self.profile(".claude", MAX)
        self.ntb = self.profile(".claude-ntb", TEAM)
        self.personal = self.profile(".claude-personal", MAX)

    def tearDown(self):
        stop_job_runners(self.agt)
        self._env.stop()
        self._temp.cleanup()

    def profile(self, name, account, logged_in=True):
        folder = self.home / name
        folder.mkdir(exist_ok=True)
        # Claude Code keeps the default folder's state next to it, in ~/.claude.json.
        state = self.home / ".claude.json" if name == ".claude" else folder / ".claude.json"
        state.write_text(json.dumps({"oauthAccount": account, "userID": "x"}), encoding="utf-8")
        (folder / "logged_in").unlink(missing_ok=True)
        if logged_in:
            (folder / "logged_in").write_text("", encoding="utf-8")
        profiles.forget()
        return folder

    def session(self, folder):
        """Act as a Claude Code session running in `folder` (None: no session, a plain terminal)."""

        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        if folder is None:
            return
        os.environ["CLAUDECODE"] = "1"
        if folder != self.default:
            os.environ["CLAUDE_CONFIG_DIR"] = str(folder)

    def claude(self, settings=""):
        if settings:
            (self.agt / "settings.toml").write_text(settings, encoding="utf-8")
        return load_config().runtime("claude")

    def names(self, accounts_list):
        return [account.name for account in accounts_list]


class DiscoveryTests(ProfileHome):
    def test_profiles_are_found_with_no_settings(self):
        self.session(self.ntb)
        config = self.claude()
        found = {account.name: account for account in config.found_accounts()}
        self.assertEqual(set(found), {"claude", "claude-ntb", "claude-personal"})
        self.assertTrue(all(account.setup == "own" and account.found for account in found.values()))
        self.assertEqual(found["claude"].login, found["claude-personal"].login)
        self.assertNotEqual(found["claude"].login, found["claude-ntb"].login)
        self.assertFalse((self.agt / "settings.toml").exists())  # nothing is written

    def test_a_logged_out_folder_is_listed_but_never_picked(self):
        self.profile(".claude", MAX, logged_in=False)
        self.session(self.ntb)
        config = self.claude()
        self.assertEqual(self.names(config.worker_accounts()), ["claude-personal"])
        self.assertIn("claude", self.names(config.listed_accounts()))

    def test_a_folder_never_logged_in_is_not_a_profile(self):
        (self.home / ".claude-empty").mkdir()
        (self.home / ".claude-empty" / "settings.json").write_text("{}", encoding="utf-8")
        self.assertNotIn("claude-empty", self.names(self.claude().found_accounts()))

    def test_ignore_leaves_a_profile_alone(self):
        self.session(self.ntb)
        config = self.claude('[claude]\nignore = ["~/.claude-personal", "claude"]\n')
        self.assertEqual(self.names(config.found_accounts()), ["claude-ntb"])

    def test_found_profiles_are_never_saved_with_the_accounts(self):
        from agents_router.config import save_accounts
        from agents_router import setup

        self.session(self.ntb)
        loaded = load_config()
        loaded.runtime("claude").found_accounts()
        setup.register_account(loaded, "claude", "spare", self.agt / "homes" / "claude" / "spare")
        save_accounts(loaded)
        text = (self.agt / "settings.toml").read_text(encoding="utf-8")
        self.assertIn('name = "spare"', text)
        self.assertNotIn("claude-ntb", text)
        self.assertNotIn("setup", text)  # an agt folder stays linked


class SessionTests(ProfileHome):
    def test_the_session_profile_never_takes_its_own_jobs(self):
        self.session(self.ntb)
        config = self.claude()
        self.assertEqual(self.names(config.worker_accounts()), ["claude"])  # claude-personal: same login as claude
        with self.assertRaisesRegex(ValueError, "this session runs in"):
            accounts.ranked_accounts(config, pinned="claude-ntb")
        with self.assertRaisesRegex(ValueError, "this session runs in"):
            accounts.ranked_accounts(config, pinned="primary")

    def test_the_other_session_sees_it_the_other_way_round(self):
        self.session(self.default)  # plain `claude`: no CLAUDE_CONFIG_DIR
        config = self.claude()
        self.assertEqual(self.names(config.worker_accounts()), ["claude-ntb"])
        with self.assertRaisesRegex(ValueError, "this session runs in"):
            accounts.ranked_accounts(config, pinned="claude")
        # A folder with the session's login is skipped unpinned, but may be named.
        ranked, _ = accounts.ranked_accounts(config, pinned="claude-personal")
        self.assertEqual(self.names(ranked), ["claude-personal"])

    def test_a_short_name_picks_a_profile(self):
        self.session(self.default)
        ranked, _ = accounts.ranked_accounts(self.claude(), pinned="ntb")
        self.assertEqual(self.names(ranked), ["claude-ntb"])
        with self.assertRaisesRegex(ValueError, "Known: .*claude-ntb"):
            accounts.ranked_accounts(self.claude(), pinned="nope")

    def test_no_other_profile_refuses(self):
        self.profile(".claude", MAX, logged_in=False)
        self.profile(".claude-personal", MAX, logged_in=False)
        self.session(self.ntb)
        with self.assertRaisesRegex(ValueError, "No other Claude profile can take this job"):
            accounts.ranked_accounts(self.claude())

    def test_without_a_session_nothing_is_excluded(self):
        self.session(None)
        config = self.claude()
        self.assertIsNone(config.session_home)
        self.assertEqual(self.names(config.worker_accounts()), ["claude", "claude-ntb"])

    def test_a_scrubbed_environment_means_the_default_folder(self):
        os.environ["CLAUDECODE"] = "1"
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.assertTrue(profiles.same_folder(self.claude().session_home, self.default))

    def test_order_sets_the_priority(self):
        self.session(self.ntb)
        config = self.claude('[claude]\norder = ["claude-personal", "claude"]\n')
        self.assertEqual(self.names(config.worker_accounts()), ["claude-personal"])
        ranked, _ = accounts.ranked_accounts(config)
        self.assertEqual(self.names(ranked), ["claude-personal"])

    def test_agt_accounts_come_before_found_profiles(self):
        self.session(self.ntb)
        homes = self.agt / "homes" / "claude"
        config = self.claude(f'[[claude.accounts]]\nname = "pool"\nhome = {json.dumps(str(homes / "pool"))}\n')
        ranked, _ = accounts.ranked_accounts(config)
        self.assertEqual(self.names(ranked), ["pool", "claude"])

    def test_two_sessions_at_once(self):
        # Each agt process takes the session from its own caller; nothing about it is shared.
        first, second = load_config(), load_config()
        set_session(first, self.ntb)
        set_session(second, self.default)
        self.assertEqual(self.names(first.runtime("claude").worker_accounts()), ["claude"])
        self.assertEqual(self.names(second.runtime("claude").worker_accounts()), ["claude-ntb"])


class SingleProfileTests(ProfileHome):
    """Inside a Claude Code session whose profile is the only one: reports and other runtimes keep working."""

    def setUp(self):
        super().setUp()
        self.profile(".claude", MAX, logged_in=False)
        self.profile(".claude-personal", MAX, logged_in=False)
        self.session(self.ntb)

    def test_reports_and_screens_do_not_raise(self):
        import contextlib
        import io

        from agents_router import cli, home, tools

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            tools.room_note()
            report = tools._call("subagent_accounts", {"runtime": "claude"}, client="claude-code")
            self.assertTrue(any(entry.get("this_session") for entry in report["runtimes"][0]["accounts"]))
            self.assertEqual(cli.main(["accounts"]), 0)
            cli.main(["doctor"])
            cli.main(["launch"])  # refuses with a message, doesn't raise
            home.show()
            tools._call("subagent_list", {}, client="claude-code")

    def test_a_claude_job_is_refused_with_a_clear_message(self):
        from agents_router import tools

        with self.assertRaisesRegex(ValueError, "No other Claude profile can take this job"):
            tools._call("subagent_start", {"prompt": "x", "cwd": str(self.project)}, client="claude-code")


class UsageTests(ProfileHome):
    def test_one_login_shares_its_cooldown(self):
        self.session(self.ntb)
        config = self.claude()
        accounts.set_cooldown("claude", "claude-personal", time.time() + 600, "limit")
        claude = config.find("claude")
        self.assertIsNotNone(accounts.cooldown_until(accounts.account_record(config, claude, accounts.load_usage("claude"))))
        usable, earliest = accounts.ranked_accounts(config)
        self.assertEqual(usable, [])  # claude's login is cooling down through claude-personal
        self.assertIsNotNone(earliest)

    def test_usage_from_another_login_is_ignored(self):
        self.session(self.ntb)
        config = self.claude()
        claude = config.find("claude")
        accounts.update_usage("claude", "claude", login="someone-else", windows={"five_hour": {"utilization": 1.0}})
        self.assertIsNone(accounts.account_record(config, claude, accounts.load_usage("claude")))


class RuntimeTests(ProfileHome):
    def test_the_default_folder_runs_without_claude_config_dir(self):
        self.session(self.ntb)
        config = self.claude()
        claude = config.find("claude")
        self.assertNotIn("CLAUDE_CONFIG_DIR", ClaudeRuntime().account_env(config, claude))
        link = self.root / "alias"
        link.symlink_to(self.default)
        self.assertNotIn("CLAUDE_CONFIG_DIR", ClaudeRuntime().account_env(config, claude.__class__(name="x", home=link)))
        self.assertEqual(ClaudeRuntime().account_env(config, config.find("claude-personal"))["CLAUDE_CONFIG_DIR"],
                         str(self.personal))

    def test_logins_are_checked_the_way_claude_code_finds_them(self):
        self.session(self.ntb)
        found = {account.name: account for account in self.claude().found_accounts()}
        self.assertTrue(found["claude"].logged_in)  # ~/.claude is logged in only without CLAUDE_CONFIG_DIR

    def test_a_profile_with_its_own_setup_is_left_alone(self):
        self.session(self.ntb)
        config = self.claude()
        claude = config.find("claude")
        spec = JobSpec(job_id="probe", runtime="claude", prompt="", cwd=str(self.project))
        report = ClaudeRuntime().prepare(config, claude, spec)
        self.assertIsNone(report["settings_path"])
        self.assertEqual(report["plugin_dirs"], [])
        self.assertFalse((self.default / ".agents-router-managed.json").exists())

    def test_a_job_runs_on_the_named_profile_and_records_its_folder(self):
        self.session(self.ntb)
        job = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), account="claude", host="claude")
        status = jobs.wait_for(job["job_id"], 60)
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(status["attempts"][-1]["account"], "claude")
        self.assertTrue(profiles.same_folder(status["attempts"][-1]["home"], self.default))
        self.assertNotIn("CLAUDE_CONFIG_DIR", [record for record in self.records() if "-p" in record["argv"]][-1]["env"])
        spec = jobs.read_spec(job["job_id"])
        self.assertTrue(profiles.same_folder(spec.parent["config_dir"], self.ntb))

    def test_a_job_pinned_to_the_session_profile_is_refused(self):
        self.session(self.ntb)
        with self.assertRaisesRegex(ValueError, "this session runs in"):
            jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), account="ntb", host="claude")

    def test_pending_results_stay_with_the_session_that_started_them(self):
        self.session(self.ntb)
        job = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), account="claude", host="claude")
        jobs.wait_for(job["job_id"], 60)
        self.assertEqual([item["job_id"] for item in hooks.pending_jobs(str(self.project), session=self.ntb)], [job["job_id"]])
        self.assertEqual(hooks.pending_jobs(str(self.project), session=self.default), [])
        self.assertEqual(jobs.list_jobs(session=self.default), [])
        self.assertEqual(len(jobs.list_jobs()), 1)

    def test_an_agt_account_keeps_one_setup_source_across_sessions(self):
        from agents_router.config import Account
        from agents_router.runtimes.claude import _setup_source

        (self.ntb / "CLAUDE.md").write_text("team rules", encoding="utf-8")
        pool = Account(name="pool", home=self.agt / "homes" / "claude" / "pool")
        self.session(self.ntb)
        first = self.claude()
        spec = JobSpec(job_id="probe", runtime="claude", prompt="", cwd=str(self.project))
        ClaudeRuntime().prepare(first, pool, spec)
        self.session(self.default)
        second = self.claude()
        self.assertTrue(profiles.same_folder(_setup_source(second, pool), self.ntb))
        ClaudeRuntime().prepare(second, pool, spec)
        self.assertEqual((pool.home / "CLAUDE.md").read_text(encoding="utf-8"), "team rules")

    def test_flag_files_belong_to_the_job(self):
        from agents_router.config import Account, jobs_dir

        self.session(self.ntb)
        config = self.claude()
        pool = Account(name="pool", home=self.agt / "homes" / "claude" / "pool")
        (jobs_dir() / "job-1").mkdir(parents=True)
        (self.ntb / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
        report = ClaudeRuntime().prepare(config, pool, JobSpec(job_id="job-1", runtime="claude", prompt="", cwd=str(self.project)))
        self.assertTrue(Path(report["settings_path"]).is_relative_to(jobs_dir() / "job-1"))

    def test_a_different_transcript_is_kept_not_lost(self):
        from agents_router.config import Account
        from agents_router.runtimes.claude import _project_dir_name

        session = "0123456789abcdef"
        name = _project_dir_name(str(self.project))
        source, target = Account("a", self.root / "a"), Account("b", self.root / "b")
        for folder, text in ((source.home, "one\ntwo\n"), (target.home, "one\nelse\n")):
            (folder / "projects" / name).mkdir(parents=True)
            (folder / "projects" / name / f"{session}.jsonl").write_text(text, encoding="utf-8")
        self.assertTrue(ClaudeRuntime().transfer_session(source, target, session, str(self.project)))
        kept = list((target.home / "projects" / name).glob(f"{session}.jsonl.agt-replaced-*"))
        self.assertEqual([path.read_text(encoding="utf-8") for path in kept], ["one\nelse\n"])
        # A longer version of the same conversation just replaces the shorter one.
        (source.home / "projects" / name / f"{session}.jsonl").write_text("one\ntwo\nthree\n", encoding="utf-8")
        self.assertTrue(ClaudeRuntime().transfer_session(source, target, session, str(self.project)))
        self.assertEqual(len(list((target.home / "projects" / name).glob("*.agt-replaced-*"))), 1)

    def records(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
