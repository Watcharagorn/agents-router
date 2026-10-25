"""Project scope: a listed repo's own agents and skills, shared between its CLI folders and its worktrees."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from test_share_items import ItemBase
from test_share_sync import age

from agents_router import cli
from agents_router.share import scope, settings, watch


@unittest.skipIf(shutil.which("git") is None, "needs git")
class ProjectTests(ItemBase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "work" / "app"
        self.repo.mkdir(parents=True)
        self.git("init", "-q")
        self.agent(self.repo / ".claude", "builder", body="Builds the app.", tools="Bash, Read")
        self.skill(self.repo / ".claude", "release-notes", body="Write the notes.")
        age(*(self.repo / ".claude").rglob("*"))
        self.git("add", ".")
        self.git("commit", "-qm", "project agents")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd or self.repo,
                              check=True, capture_output=True, text=True).stdout.strip()

    def in_repo(self, *args, code=0, cwd=None):
        before = os.getcwd()
        os.chdir(cwd or self.repo)
        try:
            return self.share(*args, code=code)
        finally:
            os.chdir(before)

    def test_a_listed_repo_shares_its_own_items_between_its_cli_folders(self):
        self.enable()
        user_before = self.folders()
        out = self.share("repo", "add", str(self.repo))
        self.assertIn("app", out)
        kiro = self.repo / ".kiro" / "agents" / "builder.json"
        self.assertEqual(json.loads(kiro.read_text())["prompt"], "Builds the app.")
        self.assertIn('name = "builder"', (self.repo / ".codex" / "agents" / "builder.toml").read_text())
        link = self.repo / ".agents" / "skills" / "release-notes"
        self.assertTrue(link.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(link)))  # relative, so a worktree's links stay inside it
        self.assertEqual(link.resolve(), (self.repo / ".claude" / "skills" / "release-notes").resolve())
        exclude = (self.repo / ".git" / "info" / "exclude").read_text()
        self.assertIn("/.kiro/agents/builder.json", exclude)
        self.assertEqual(self.git("status", "--porcelain"), "")  # nothing to commit by accident
        self.assertEqual({k: v for k, v in self.folders().items() if not k.startswith(str(self.root / "work"))}, user_before)
        self.assertTrue(list((self.agt_home / "state" / "share" / "projects").glob("app-*/state.json")))
        listed = json.loads(self.share("repo", "list", "--json"))
        self.assertEqual([entry["root"] for entry in listed["repos"]], [os.path.realpath(self.repo)])

    def test_edits_in_a_repo_spread_and_status_names_the_repo(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        kiro = self.repo / ".kiro" / "agents" / "builder.json"
        data = json.loads(kiro.read_text())
        data["prompt"] = "Builds the app, fast."
        kiro.write_text(json.dumps(data, indent=2) + "\n")
        age(kiro)
        status = self.share("status")
        self.assertIn("Repo ", status)
        self.assertIn("2 changes for the next sync", status.split("Repo ")[1])  # to claude and codex
        self.share("sync")
        self.assertIn("Builds the app, fast.", (self.repo / ".claude" / "agents" / "builder.md").read_text())
        self.assertIn("builder.md", self.git("status", "--porcelain"))  # your review is git diff; nothing is staged

    def test_picks_for_a_repos_item_go_in_the_repos_settings(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        self.in_repo("pick", "builder", "--not-in", "kiro")
        self.assertFalse((self.repo / ".kiro" / "agents" / "builder.json").exists())
        text = (self.repo / ".agt" / "settings.toml").read_text()
        self.assertIn('"agents/builder" = { not_in = ["kiro"] }', text)
        self.assertNotIn("builder", (self.user / ".agt" / "settings.toml").read_text())

    def test_worktrees_join_by_themselves(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        tree = self.root / "work" / "app-feature"
        self.git("worktree", "add", "-q", str(tree))
        self.share("sync")
        link = tree / ".agents" / "skills" / "release-notes"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (tree / ".claude" / "skills" / "release-notes").resolve())  # inside the worktree
        self.assertTrue((tree / ".kiro" / "agents" / "builder.json").exists())

    def test_worktrees_wait_for_repo_add_with_worktrees_command(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["config", "set", "share.worktrees", "command"])
        self.enable()
        self.assertEqual(settings.load().worktrees, "command")  # enable again keeps it
        tree = self.root / "work" / "app-later"
        self.git("worktree", "add", "-q", str(tree))
        self.share("sync")
        self.assertFalse((tree / ".kiro" / "agents" / "builder.json").exists())
        self.in_repo("repo", "add", ".", cwd=tree)
        self.assertTrue((tree / ".kiro" / "agents" / "builder.json").exists())

    def test_a_merge_in_progress_leaves_the_repo_alone(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        (self.repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
        self.agent(self.repo / ".claude", "builder", body="Mid-merge.")
        age(self.repo / ".claude" / "agents" / "builder.md")
        out = self.share("sync")
        self.assertIn("merge", out)
        self.assertNotIn("Mid-merge.", (self.repo / ".kiro" / "agents" / "builder.json").read_text())

    def test_a_cleaned_copy_is_made_again(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        (self.repo / ".kiro" / "agents" / "builder.json").unlink()
        self.share("sync")
        self.assertTrue((self.repo / ".kiro" / "agents" / "builder.json").exists())

    def test_a_committed_original_gone_on_this_branch_takes_its_copies_away_and_back(self):
        self.agent(self.repo / ".claude", "tester", body="Tests the app.")  # so .claude/agents stays on the branch
        self.git("add", ".")
        self.git("commit", "-qm", "tester")
        self.enable()
        self.share("repo", "add", str(self.repo))
        self.git("checkout", "-q", "-b", "slim")
        self.git("rm", "-q", ".claude/agents/builder.md")
        self.git("commit", "-qm", "no builder here")
        self.share("sync")
        self.assertFalse((self.repo / ".kiro" / "agents" / "builder.json").exists())
        self.git("checkout", "-q", "-")
        age(self.repo / ".claude" / "agents" / "builder.md")
        self.share("sync")
        self.assertTrue((self.repo / ".kiro" / "agents" / "builder.json").exists())

    def test_repo_remove_with_remove_copies(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        self.share("repo", "remove", str(self.repo), "--remove-copies")
        self.assertFalse((self.repo / ".kiro" / "agents" / "builder.json").exists())
        self.assertTrue((self.repo / ".claude" / "agents" / "builder.md").exists())
        self.assertEqual(scope.listed(), [])

    def test_what_agt_refuses_to_list(self):
        self.enable()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.share("repo", "add", str(self.user), code=2)
            plain = self.root / "plain"
            plain.mkdir()
            self.share("repo", "add", str(plain), code=2)
            self.share("repo", "add", str(self.repo / ".claude"), code=2)
        text = err.getvalue()
        self.assertIn("home folder", text)
        self.assertIn("isn't in a git checkout", text)
        self.assertIn("add that instead", text)

    def test_enable_can_list_the_repos_your_clis_know(self):
        (self.claude / ".claude.json").write_text(json.dumps({"projects": {str(self.repo): {}, str(self.root): {}}}))
        proposal = json.loads(self.share("enable", "--projects", "all-with-items", "--dry-run", "--json",
                                         "--resolve", "gamma-apikey=skip"))
        self.assertEqual(proposal["repos"], [os.path.realpath(self.repo)])
        self.enable("--projects", "all-with-items")
        self.assertTrue((self.repo / ".kiro" / "agents" / "builder.json").exists())

    def test_the_watcher_covers_listed_repos(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            watch.run_once()
            watch.run_once()
        self.agent(self.repo / ".claude", "builder", body="Watched.")
        age(self.repo / ".claude" / "agents" / "builder.md")
        watch.background_sync("watcher")
        self.assertIn("Watched.", (self.repo / ".kiro" / "agents" / "builder.json").read_text())

    def test_a_session_in_an_unlisted_repo_with_items_says_so_once(self):
        self.enable()
        payload = json.dumps({"cwd": str(self.repo)})
        first = watch.session("claude", payload)
        self.assertIn("this repo isn't shared: `agt share repo add .`", first)
        second = watch.session("claude", payload)
        self.assertTrue(second is None or "isn't shared" not in second)

    def test_user_scope_settings_never_take_project_folders(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        self.assertEqual([folder.label for folder in settings.load().folders], ["claude-lab", "claude", "codex", "kiro"])
        self.assertFalse((self.repo / ".claude" / "settings.json").exists())  # no hook entries in a repo


if __name__ == "__main__":
    unittest.main()
