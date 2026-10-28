"""agt config show|set|unset, agt share settings, agt log, and agt jobs per workspace."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import cli, jobs, synclog, ui
from agents_router.config import load_config

AGENT = {"CLAUDECODE": "1"}


class ConfigBase(TempHome):
    def setUp(self):
        super().setUp()
        self.user_home = self.root / "home"
        self.user_home.mkdir()
        (self.project / ".agt").mkdir()
        self._env2 = patch.dict(os.environ, {"HOME": str(self.user_home), "USERPROFILE": str(self.user_home)})
        self._env2.start()
        self._cwd = os.getcwd()
        os.chdir(self.project)
        self.typed = patch("sys.stdin", io.StringIO(""))
        self.typed.start()

    def tearDown(self):
        os.chdir(self._cwd)
        self.typed.stop()
        self._env2.stop()
        super().tearDown()

    def agt(self, *argv, terminal=False, env=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env or {}), patch("sys.stdin.isatty", lambda: terminal), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def json(self, *argv, **kwargs):
        code, out, err = self.agt(*argv, "--json", **kwargs)
        return code, json.loads(out)

    @property
    def user_file(self):
        return self.home / "settings.toml"

    @property
    def team_file(self):
        return self.project / ".agt" / "settings.toml"

    @property
    def local_file(self):
        return self.project / ".agt" / "settings.local.toml"

    def text(self, path):
        return path.read_text(encoding="utf-8")


class ShowTests(ConfigBase):
    def test_show_names_each_values_source(self):
        self.user_file.write_text("[codex]\nmax_jobs = 3\n", encoding="utf-8")
        self.team_file.write_text("# the team's\n[codex]\ncooldown_minutes = 9\n", encoding="utf-8")
        self.local_file.write_text("max_depth = 0\n", encoding="utf-8")
        code, shown = self.json("config", "show")
        self.assertEqual(code, 0)
        rows = {row["key"]: row for row in shown["settings"]}
        self.assertEqual(shown["workspace"], str(self.project))
        self.assertEqual([entry["level"] for entry in shown["files"]], ["user", "workspace", "local"])
        self.assertEqual((rows["codex.max_jobs"]["value"], rows["codex.max_jobs"]["source"]), (3, "user"))
        self.assertEqual((rows["codex.cooldown_minutes"]["source"], rows["codex.cooldown_minutes"]["line"]), ("workspace", 3))
        self.assertEqual((rows["max_depth"]["value"], rows["max_depth"]["source"]), (0, "local"))
        self.assertEqual((rows["claude.primary"]["source"], rows["claude.primary"]["value"]),
                         ("environment (CLAUDE_CONFIG_DIR)", str(self.primary)))
        self.assertEqual((rows["max_jobs"]["shown"], rows["max_jobs"]["source"]), ("no cap", "default"))
        self.assertTrue(rows["codex.max_jobs_per_account"]["needs_user_words"])
        self.assertEqual(rows["share.kinds"]["changed_by"], "agt share kinds add|remove <kind>")

    def test_show_in_text_lists_no_cap_and_the_parent_channel_hint(self):
        code, out, _ = self.agt("config", "show", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], f"agt config  settings in effect in {ui.short_path(self.project)}")
        self.assertRegex(out, r"\n  antigravity\.max_jobs_per_account +no cap +default\n")
        self.assertIn("Note: Antigravity workers have no parent channel; set `max_jobs_per_account = 1` under "
                      f"[antigravity] in {ui.tilde(str(self.user_file))} to get it.", out)

    def test_show_names_a_bad_value_with_its_file_and_line(self):
        self.team_file.write_text("[codex]\n\nmax_jobs_per_account = 0\n", encoding="utf-8")
        code, out, _ = self.agt("config", "show")
        self.assertEqual(code, 2)
        self.assertIn(f"Problem: {self.team_file}:3: [codex] max_jobs_per_account must be", out)

    def test_in_the_home_folder_there_are_user_settings_only(self):
        os.chdir(self.user_home)
        code, out, _ = self.agt("config", "show")
        self.assertEqual(out.splitlines()[:4], ["agt config  settings in effect, outside any workspace", "", "Settings files",
                                                f"  user   {ui.short_path(self.user_file)}   not there yet"])


class SetTests(ConfigBase):
    def test_set_writes_the_workspace_file_keeps_comments_and_saves_a_backup(self):
        self.team_file.write_text("# the team's\n[codex]\nmax_jobs = 2  # two at most\ncooldown_minutes = 9\n",
                                  encoding="utf-8")
        code, out, _ = self.agt("config", "set", "codex.max_jobs", "1", terminal=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.text(self.team_file), "# the team's\n[codex]\nmax_jobs = 1  # two at most\ncooldown_minutes = 9\n")
        self.assertEqual(out.splitlines(), [
            f"codex.max_jobs: 2 → 1 in {self.team_file} (workspace)",
            "In effect here: 1. Applies at the next job start (running jobs keep theirs).",
            "Undo: agt config set codex.max_jobs 2 --workspace",
        ])
        backups = list((self.home / "state" / "backups" / "settings").iterdir())
        self.assertEqual([self.text(path) for path in backups],
                         ["# the team's\n[codex]\nmax_jobs = 2  # two at most\ncooldown_minutes = 9\n"])
        self.assertFalse(self.user_file.exists())

    def test_set_adds_a_key_without_pinning_anything_else(self):
        code, report = self.json("config", "set", "codex.max_jobs", "4", "--user")
        self.assertEqual(code, 0)
        self.assertEqual(self.text(self.user_file), "[codex]\nmax_jobs = 4\n")
        self.assertEqual((report["old"], report["new"], report["file"], report["undo"]),
                         (None, 4, str(self.user_file), "agt config unset codex.max_jobs --user"))
        self.user_file.write_text("# mine\n\n[share]\nenabled = true\n", encoding="utf-8")
        self.json("config", "set", "max_depth", "2", "--user")
        self.assertEqual(self.text(self.user_file), "# mine\nmax_depth = 2\n\n[share]\nenabled = true\n")
        self.assertEqual(load_config().max_depth, 2)

    def test_account_picks_go_to_the_local_file_kept_out_of_git(self):
        if shutil.which("git") is None:
            self.skipTest("needs git")
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        self.write_config(accounts=("a1", "a2"))
        code, report = self.json("config", "set", "claude.accounts", "a2")
        self.assertEqual(code, 0, report)
        self.assertEqual((report["level"], report["file"]), ("local", str(self.local_file)))
        self.assertEqual(self.text(self.local_file), '[claude]\naccounts = ["a2"]\n')
        exclude = self.project / ".git" / "info" / "exclude"
        self.assertIn("/.agt/settings.local.toml", self.text(exclude).splitlines())
        self.assertEqual([a.name for a in load_config().runtime("claude").worker_accounts()], ["a2"])
        code, report = self.json("config", "set", "claude.accounts", "a1,a2")
        self.assertEqual(report["undo"], "agt config set claude.accounts a2 --local")
        code, report = self.json("config", "set", "claude.accounts", "a2")
        self.assertEqual(report["undo"], "agt config set claude.accounts a1,a2 --local")
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.project,
                                capture_output=True, text=True, check=True).stdout
        self.assertNotIn("settings.local.toml", status)

    def test_machine_wide_keys_go_to_your_user_file(self):
        code, report = self.json("config", "set", "claude.binary", "/opt/claude")
        self.assertEqual((code, report["level"]), (0, "user"))
        code, out, err = self.agt("config", "set", "claude.binary", "/opt/claude", "--local")
        self.assertEqual(code, 2)
        self.assertIn("goes in your user settings only", err)
        code, out, err = self.agt("config", "set", "share.auto_copy", "off", "--workspace")
        self.assertEqual(code, 2)
        self.assertIn("agt's share settings apply to your user-level folders", err)

    def test_share_keys_change_the_share_settings_and_keep_the_rest(self):
        self.user_file.write_text('[share]\nenabled = true\nkinds = ["agents", "skills"]\nauto_copy = true # new ones\n',
                                  encoding="utf-8")
        code, report = self.json("config", "set", "share.auto_copy", "skills")
        self.assertEqual((code, report["old"], report["new"], report["applies"]),
                         (0, True, ["skills"], "the next sync (agt share sync)"))
        self.assertEqual(self.text(self.user_file),
                         '[share]\nenabled = true\nkinds = ["agents", "skills"]\nauto_copy = ["skills"] # new ones\n')
        code, out, err = self.agt("config", "set", "share.kinds", "agents")
        self.assertEqual(code, 2)
        self.assertIn("share.kinds changes with: agt share kinds add|remove <kind>", err)

    def test_bad_input_is_refused_and_nothing_is_written(self):
        self.team_file.write_text("[codex]\nmax_jobs_by_room = [\n  [50, 2],\n  [0, 1],\n]\nmax_jobs = [\n  1,\n]\n",
                                  encoding="utf-8")
        before = self.text(self.team_file)
        for argv, message in ((("codex.max_jobs", "x"), "codex.max_jobs takes a number"),
                              (("codex.max_jobs", "0"), "must be at least 1; `agt config unset` it for no cap"),
                              (("codex.failover", "sometimes"), "codex.failover takes off, on-limit"),
                              (("max_job", "2"), "agt has no setting max_job\n  Did you mean max_jobs?"),
                              (("cooldown_minutes", "2"), "did you mean claude.cooldown_minutes or "
                                                          "codex.cooldown_minutes or antigravity.cooldown_minutes or kiro.cooldown_minutes?"),
                              (("codex.max_jobs", "2"), "max_jobs spans more than one line; change it there by hand")):
            code, out, err = self.agt("config", "set", *argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(message, err)
        self.assertEqual(self.text(self.team_file), before)

    def test_set_reports_what_still_limits_it_and_what_overrides_it(self):
        self.user_file.write_text("max_jobs = 3\n[codex]\nmax_jobs = 2\n", encoding="utf-8")
        code, report = self.json("config", "set", "codex.max_jobs_per_account", "5")
        self.assertEqual(report["still_limited_by"], [f"codex.max_jobs = 2 (user, {self.user_file})",
                                                      f"max_jobs = 3 (user, {self.user_file})"])
        self.local_file.write_text("[codex]\ncooldown_minutes = 1\n", encoding="utf-8")
        code, report = self.json("config", "set", "codex.cooldown_minutes", "30", "--user")
        self.assertEqual(report["overridden_by"], {"level": "local", "file": str(self.local_file), "value": 1,
                                                   "fix": "agt config unset codex.cooldown_minutes --local"})
        self.assertEqual(report["effective"], 1)


class UserWordsTests(ConfigBase):
    def test_a_person_at_a_terminal_is_never_asked_and_the_log_says_so(self):
        code, out, _ = self.agt("config", "set", "codex.max_jobs_per_account", "6", terminal=True)
        self.assertEqual(code, 0, out)
        logged = next(synclog.entries())
        self.assertEqual((logged["type"], logged["key"], logged["old"], logged["new"], logged["by"], logged["user_said"]),
                         ("setting", "codex.max_jobs_per_account", None, 6, "terminal", None))
        self.assertEqual((logged["workspace"], logged["level"]), (str(self.project), "workspace"))

    def test_an_agent_needs_the_users_words_to_raise_a_limit(self):
        self.user_file.write_text("[codex]\nmax_jobs_per_account = 2\n", encoding="utf-8")
        code, refused = self.json("config", "set", "codex.max_jobs_per_account", "6", terminal=True, env=AGENT)
        self.assertEqual(code, 3)
        self.assertEqual(refused["error"], "needs_user_words")
        self.assertEqual(refused["retry"], 'agt config set codex.max_jobs_per_account 6 --workspace '
                                           '--user-said "<the user\'s words>"')
        self.assertFalse(self.team_file.exists())
        self.assertEqual(list(synclog.entries()), [])
        code, report = self.json("config", "set", "codex.max_jobs", "3", env=AGENT)
        self.assertEqual(code, 0)  # no cap to a cap of 3 lowers it
        code, report = self.json("config", "set", "codex.max_jobs_per_account", "6", "--user-said", "let it run six",
                                 env=AGENT)
        self.assertEqual(code, 0)
        logged = next(synclog.entries())
        self.assertEqual((logged["by"], logged["host"], logged["user_said"]), ("agent", "claude", "let it run six"))

    def test_lowering_a_limit_never_needs_words(self):
        self.team_file.write_text("[codex]\nmax_jobs_per_account = 4\n", encoding="utf-8")
        code, report = self.json("config", "set", "codex.max_jobs_per_account", "2", env=AGENT)
        self.assertEqual((code, report["new"]), (0, 2))
        code, report = self.json("config", "set", "antigravity.sandbox", "true", env=AGENT)
        self.assertEqual(code, 0)
        code, report = self.json("config", "set", "antigravity.sandbox", "false", env=AGENT)
        self.assertEqual(code, 3)  # turning the sandbox off loosens safety

    def test_unsetting_a_cap_from_an_agent_needs_words_because_no_cap_is_looser(self):
        self.team_file.write_text("[codex]\nmax_jobs_per_account = 4\n", encoding="utf-8")
        code, refused = self.json("config", "unset", "codex.max_jobs_per_account", env=AGENT)
        self.assertEqual((code, refused["old"], refused["new"]), (3, 4, None))
        code, report = self.json("config", "unset", "codex.max_jobs_per_account", "--user-said", "no cap is fine",
                                 env=AGENT)
        self.assertEqual((code, report["new"]), (0, None))
        self.assertEqual(self.text(self.team_file), "[codex]\n")

    def test_a_limit_raised_at_user_level_counts_even_when_this_workspace_overrides_it(self):
        self.user_file.write_text("[codex]\nmax_jobs = 2\n", encoding="utf-8")
        self.local_file.write_text("[codex]\nmax_jobs = 1\n", encoding="utf-8")
        code, refused = self.json("config", "set", "codex.max_jobs", "8", "--user", env=AGENT)
        self.assertEqual(code, 3)


class UnsetTests(ConfigBase):
    def test_unset_removes_the_key_from_one_level_only(self):
        self.user_file.write_text("[codex]\nmax_jobs = 3\n", encoding="utf-8")
        self.team_file.write_text("[codex]\nmax_jobs = 2 # team\nwhen_full = \"queue\"\n", encoding="utf-8")
        code, out, _ = self.agt("config", "unset", "codex.max_jobs")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.text(self.team_file), "[codex]\nwhen_full = \"queue\"\n")
        self.assertEqual(self.text(self.user_file), "[codex]\nmax_jobs = 3\n")
        self.assertIn("In effect here: 3.", out)
        self.assertIn("Undo: agt config set codex.max_jobs 2 --workspace", out)
        code, out, _ = self.agt("config", "unset", "codex.max_jobs", "--workspace")
        self.assertEqual((code, out.strip()), (0, f"codex.max_jobs isn't set in {self.team_file} (workspace); nothing to unset."))


class ShareSettingsTests(ConfigBase):
    def test_share_settings_lists_each_with_the_command_that_changes_it(self):
        self.user_file.write_text("[share]\nenabled = true\nauto_copy = false\n", encoding="utf-8")
        code, shown = self.json("share", "settings")
        self.assertEqual(code, 0)
        rows = {row["key"]: row for row in shown["settings"]}
        self.assertEqual((rows["share.auto_copy"]["shown"], rows["share.auto_copy"]["source"]), ("off", "user"))
        self.assertEqual(rows["share.auto_copy"]["command"], "agt config set share.auto_copy on|off|<kinds>")
        self.assertEqual(rows["share.enabled"]["command"], "agt share disable")
        code, out, _ = self.agt("share", "settings")
        self.assertIn("\nSet by you\n  enabled         true             agt share enable | disable\n"
                      "  auto_copy       off              on | off | <kinds>\n", out)


class LogTests(ConfigBase):
    def test_log_shows_this_workspace_and_your_user_level_and_counts_the_rest(self):
        self.agt("config", "set", "codex.max_jobs", "2", terminal=True)
        self.agt("config", "set", "max_depth", "0", "--user", terminal=True)
        other = self.root / "other"
        (other / ".agt").mkdir(parents=True)
        os.chdir(other)
        self.agt("config", "set", "codex.cooldown_minutes", "5", terminal=True)
        os.chdir(self.project)
        code, out, _ = self.agt("log")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertIn("max_depth: not set → 0", lines[0])
        self.assertIn("(user, typed in a terminal)", lines[0])
        self.assertIn("codex.max_jobs: not set → 2", lines[1])
        self.assertEqual(lines[2], "1 more in 1 other workspace: `agt log --all`")
        code, shown = self.json("log", "--all")
        self.assertEqual([entry["key"] for entry in shown["entries"]], ["codex.cooldown_minutes", "max_depth", "codex.max_jobs"])
        code, shown = self.json("log", "--all", "--key", "max_depth")
        self.assertEqual([entry["key"] for entry in shown["entries"]], ["max_depth"])
        code, shown = self.json("log", "--since", "1h")
        self.assertEqual(len(shown["entries"]), 2)
        code, out, err = self.agt("log", "--since", "soon")
        self.assertEqual(code, 2)
        self.assertIn("--since takes 30m", err)

    def test_old_months_go_after_a_year_and_past_the_size_limit(self):
        folder = synclog.folder()
        folder.mkdir(parents=True)
        for name in ("2024-01.jsonl", "2026-01.jsonl"):
            (folder / name).write_text('{"time": "x"}\n', encoding="utf-8")
        synclog.append({"type": "setting"}, now=1793000000)  # 2026-10
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["2026-01.jsonl", "2026-10.jsonl"])
        with patch.object(synclog, "MAX_BYTES", 10):
            synclog.append({"type": "setting"}, now=1793000000)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["2026-10.jsonl"])


class JobsListTests(ConfigBase):
    def test_jobs_lists_this_workspace_and_all_lists_every_job(self):
        (self.project / "sub").mkdir()
        here = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project / "sub"), start=False)["job_id"]
        other = self.root / "other"
        other.mkdir()
        there = jobs.create_job(runtime="claude", prompt="x", cwd=str(other), start=False)["job_id"]
        code, out, _ = self.agt("jobs")
        self.assertIn(here, out)
        self.assertNotIn(there, out)
        self.assertIn("1 more in other workspaces: `agt jobs --all`", out)
        code, out, _ = self.agt("jobs", "--all")
        self.assertIn(here, out)
        self.assertIn(there, out)


if __name__ == "__main__":
    unittest.main()
