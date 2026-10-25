"""Settings levels: one settings.toml each for you, a workspace and your own copy of it."""

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

from agents_router import config, jobs, levels
from agents_router.config import Account, load_config, save_accounts
from agents_router.runner import Runner
from agents_router.share import settings as share_settings


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


class LevelsBase(TempHome):
    def setUp(self):
        super().setUp()
        self.user_home = self.root / "home"
        self.user_home.mkdir()
        self._home = patch.dict(os.environ, {"HOME": str(self.user_home), "USERPROFILE": str(self.user_home)})
        self._home.start()

    def tearDown(self):
        self._home.stop()
        super().tearDown()

    def user(self, text):
        (self.home / "settings.toml").write_text(text, encoding="utf-8")

    def workspace(self, folder, text="", local=None):
        (folder / ".agt").mkdir(parents=True, exist_ok=True)
        (folder / ".agt" / "settings.toml").write_text(text, encoding="utf-8")
        if local is not None:
            (folder / ".agt" / "settings.local.toml").write_text(local, encoding="utf-8")
        return folder


class PrecedenceTests(LevelsBase):
    def test_local_beats_workspace_beats_user_beats_the_default(self):
        repo = self.workspace(self.project, "[codex]\nmax_jobs = 2\n", local="[codex]\nmax_jobs = 1\n")
        self.user("max_depth = 2\n[codex]\nmax_jobs = 3\ncooldown_minutes = 5\n")
        loaded = load_config(cwd=repo)
        self.assertEqual(loaded.runtime("codex").max_jobs, 1)
        self.assertEqual(loaded.runtime("codex").cooldown_minutes, 5)  # keys merge one by one
        self.assertEqual(loaded.max_depth, 2)
        (repo / ".agt" / "settings.local.toml").unlink()
        self.assertEqual(load_config(cwd=repo).runtime("codex").max_jobs, 2)
        (repo / ".agt" / "settings.toml").unlink()
        self.assertEqual(load_config(cwd=repo).runtime("codex").max_jobs, 3)
        (self.home / "settings.toml").unlink()
        self.assertIsNone(load_config(cwd=repo).runtime("codex").max_jobs)

    def test_another_folder_sees_only_your_user_settings(self):
        self.workspace(self.project, "[codex]\nmax_jobs = 2\n")
        self.user("[codex]\nmax_jobs = 3\n")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(load_config(cwd=elsewhere).runtime("codex").max_jobs, 3)

    def test_the_user_file_follows_the_environment(self):
        with patch.dict(os.environ, {"AGT_CONFIG_DIR": str(self.root / "cfg")}):
            self.assertEqual(config.user_settings_path(), self.root / "cfg" / "settings.toml")
        with patch.dict(os.environ, {"AGT_CONFIG_DIR": ""}):
            self.assertEqual(config.user_settings_path(), self.home / "settings.toml")  # AGT_HOME alone keeps it inside
            self.assertEqual(share_settings.path(), config.user_settings_path())
            with patch.dict(os.environ, {"AGT_HOME": ""}):
                self.assertEqual(config.user_settings_path(), self.user_home / ".agt" / "settings.toml")

    def test_a_job_uses_its_own_workspace(self):
        self.workspace(self.project, '[claude]\ndefault_model = "from-the-repo"\n')
        status = jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)
        self.assertEqual(Runner(status["job_id"]).config.runtime("claude").default_model, "from-the-repo")
        self.assertIsNone(load_config(cwd=self.root).runtime("claude").default_model)


class WorkspaceTests(LevelsBase):
    def test_a_subfolder_finds_the_checkout_and_a_nearer_agt_folder_wins(self):
        (self.project / ".git").mkdir()
        deep = self.project / "src" / "deep"
        deep.mkdir(parents=True)
        self.assertEqual(levels.find_workspace(deep).root, self.project)  # no .agt/ yet: the checkout's root
        self.workspace(self.project)
        self.assertEqual(levels.find_workspace(deep).root, self.project)
        self.workspace(self.project / "src")
        self.assertEqual(levels.find_workspace(deep).root, self.project / "src")

    def test_a_nested_clone_or_submodule_never_uses_the_outer_repos_settings(self):
        (self.project / ".git").mkdir()
        self.workspace(self.project, "[codex]\nmax_jobs = 2\n")
        nested = self.project / "vendor" / "lib"
        (nested / ".git").mkdir(parents=True)
        self.assertEqual(levels.find_workspace(nested).root, nested)
        self.assertIsNone(load_config(cwd=nested).runtime("codex").max_jobs)
        submodule = self.project / "sub"
        submodule.mkdir()
        (submodule / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")
        found = levels.find_workspace(submodule)
        self.assertEqual((found.root, found.main), (submodule, None))

    def test_outside_git_the_nearest_agt_folder_above_or_the_folder_itself(self):
        inner = self.project / "a" / "b"
        inner.mkdir(parents=True)
        self.assertEqual(levels.find_workspace(inner).root, inner)
        self.workspace(self.project)
        self.assertEqual(levels.find_workspace(inner).root, self.project)

    def test_your_home_folder_is_never_a_workspace(self):
        (self.user_home / ".agt").mkdir()  # the user level, not a workspace
        self.assertIsNone(levels.find_workspace(self.user_home))
        below = self.user_home / "notes"
        below.mkdir()
        self.assertEqual(levels.find_workspace(below).root, below)

    @unittest.skipIf(shutil.which("git") is None, "needs git")
    def test_a_worktree_without_its_own_local_file_reads_the_main_checkouts(self):
        git("init", "-q", cwd=self.project)
        (self.project / "README").write_text("x\n", encoding="utf-8")
        git("add", "README", cwd=self.project)
        git("commit", "-qm", "init", cwd=self.project)
        tree = self.root / "tree"
        git("worktree", "add", "-q", str(tree), cwd=self.project)
        self.workspace(self.project, local="[claude]\ndefault_model = \"mine\"\n")
        found = levels.find_workspace(tree)
        self.assertEqual((found.root, found.main), (tree.resolve(), self.project.resolve()))
        self.assertEqual(load_config(cwd=tree).runtime("claude").default_model, "mine")
        self.workspace(tree, local="[claude]\ndefault_model = \"this-tree\"\n")
        self.assertEqual(load_config(cwd=tree).runtime("claude").default_model, "this-tree")


class UserOnlyTests(LevelsBase):
    def test_machine_wide_keys_in_a_workspace_file_are_noted_and_ignored(self):
        self.user(f'[claude]\nprimary = {json.dumps(str(self.primary))}\n')
        self.workspace(self.project, '[claude]\nprimary = "/elsewhere"\nbinary = "/bin/false"\nmax_jobs = 2\n\n'
                                     '[[claude.accounts]]\nname = "sneaky"\nhome = "/tmp/x"\n')
        loaded = load_config(cwd=self.project)
        claude = loaded.runtime("claude")
        self.assertEqual((claude.primary, claude.binary, claude.accounts, claude.max_jobs), (self.primary, None, [], 2))
        file = self.project / ".agt" / "settings.toml"
        self.assertEqual(loaded.notes, [
            f"{file}:2: [claude] primary is set in your user settings only ({self.home / 'settings.toml'}); ignored here",
            f"{file}:3: [claude] binary is set in your user settings only ({self.home / 'settings.toml'}); ignored here",
            f"{file}:6: [claude] accounts is set in your user settings only ({self.home / 'settings.toml'}); ignored here",
        ])

    def test_a_workspace_picks_its_accounts_by_name(self):
        self.write_config(accounts=("a1", "a2", "a3"))
        self.workspace(self.project, local='[claude]\naccounts = ["a3", "nobody", "a1"]\n')
        loaded = load_config(cwd=self.project)
        self.assertEqual([account.name for account in loaded.runtime("claude").worker_accounts()], ["a1", "a3"])
        self.assertEqual(len(loaded.runtime("claude").accounts), 3)  # a pinned job can still name the others
        self.assertEqual(loaded.notes, [f"{self.project / '.agt' / 'settings.local.toml'}:2: [claude] accounts names "
                                        "'nobody', which isn't one of your claude accounts; ignored"])
        self.assertEqual([a.name for a in load_config(cwd=self.root).runtime("claude").worker_accounts()], ["a1", "a2", "a3"])

    def test_picking_only_unknown_accounts_keeps_the_usual_ones(self):
        self.write_config(accounts=("a1", "a2"))
        self.workspace(self.project, local='[claude]\naccounts = ["nobody"]\n')
        self.assertEqual([a.name for a in load_config(cwd=self.project).runtime("claude").worker_accounts()], ["a1", "a2"])


class BadValueTests(LevelsBase):
    def test_a_bad_value_names_its_file_and_line_and_stops_jobs_there(self):
        self.workspace(self.project, "# team settings\n[codex]\nmax_jobs_per_account = 0\n")
        with self.assertRaises(levels.SettingsError) as caught:
            load_config(cwd=self.project)
        self.assertTrue(str(caught.exception).startswith(f"{self.project / '.agt' / 'settings.toml'}:3: [codex] "
                                                         "max_jobs_per_account must be"), caught.exception)
        with self.assertRaisesRegex(ValueError, "settings.toml:3"):
            jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.assertTrue(jobs.create_job(runtime="claude", prompt="x", cwd=str(elsewhere), start=False)["job_id"])

    def test_a_bad_top_level_value_and_broken_toml_are_named_too(self):
        self.user("max_depth = \"deep\"\n")
        with self.assertRaisesRegex(levels.SettingsError, r"settings.toml:1: max_depth: invalid literal"):
            load_config(cwd=self.root)
        self.user("[codex\n")
        with self.assertRaisesRegex(levels.SettingsError, r"settings.toml: .*line 1"):
            load_config(cwd=self.root)


class SaveAccountsTests(LevelsBase):
    def test_saving_accounts_keeps_every_other_line_and_never_pins_the_primary(self):
        text = ('# my settings\nmax_depth = 2\n\n[claude]\nfailover = "off"  # keep it\n\n[[claude.accounts]]\n'
                'name = "old"\nhome = "/tmp/old"\n\n[share]\nenabled = true\n')
        self.user(text)
        loaded = load_config(cwd=self.root)
        loaded.runtime("claude").accounts.append(Account("new", self.root / "new"))
        loaded.runtime("codex").accounts.append(Account("cx", self.root / "cx", fallback=True))
        save_accounts(loaded)
        saved = (self.home / "settings.toml").read_text(encoding="utf-8")
        self.assertTrue(saved.startswith('# my settings\nmax_depth = 2\n\n[claude]\nfailover = "off"  # keep it\n\n'
                                         '[share]\nenabled = true\n\n[[claude.accounts]]\nname = "old"'), saved)
        self.assertNotIn("primary", saved)
        self.assertTrue(share_settings.load().enabled)
        again = load_config(cwd=self.root)
        self.assertEqual([a.name for a in again.runtime("claude").accounts], ["old", "new"])
        self.assertTrue(again.runtime("codex").accounts[0].fallback)
        again.runtime("claude").accounts.clear()
        again.runtime("codex").accounts.clear()
        save_accounts(again)
        self.assertEqual((self.home / "settings.toml").read_text(encoding="utf-8"),
                         '# my settings\nmax_depth = 2\n\n[claude]\nfailover = "off"  # keep it\n\n[share]\nenabled = true\n')

    def test_saving_share_answers_keeps_agts_own_settings(self):
        self.user('[codex]\nmax_jobs = 2 # two at most\n')
        share_settings.save(share_settings.Settings(enabled=True))
        self.assertTrue((self.home / "settings.toml").read_text(encoding="utf-8").startswith(
            "[codex]\nmax_jobs = 2 # two at most\n\n[share]\nenabled = true\n"))
        self.assertEqual(load_config(cwd=self.root).runtime("codex").max_jobs, 2)


class OldConfigTests(LevelsBase):
    def test_the_old_config_file_is_no_longer_read_and_says_so(self):
        (self.home / "config.toml").write_text("[codex]\nmax_jobs = 2\n", encoding="utf-8")
        config._legacy_noted = False
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            loaded = load_config(cwd=self.root)
            load_config(cwd=self.root)
        self.assertIsNone(loaded.runtime("codex").max_jobs)
        note = f"{self.home / 'config.toml'} is no longer read; move its lines into {self.home / 'settings.toml'}"
        self.assertEqual(loaded.notes, [note])
        self.assertEqual(error.getvalue(), f"agt: {note}\n")  # once per process


if __name__ == "__main__":
    unittest.main()
