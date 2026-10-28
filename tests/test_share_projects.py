"""Project scope: a listed repo's own agents and skills, shared between its CLI folders and its worktrees."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

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

    def test_a_repos_wait_in_status_names_the_repo_in_its_commands(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        self.agent(self.repo / ".claude", "builder", body="Builds it one way.", tools="Bash, Read")
        kiro = self.repo / ".kiro" / "agents" / "builder.json"
        data = json.loads(kiro.read_text())
        data["prompt"] = "Builds it another way."
        kiro.write_text(json.dumps(data, indent=2) + "\n")
        age(self.repo / ".claude" / "agents" / "builder.md", kiro)
        status = self.share("status", code=1)
        self.assertIn("agt share  on, 1 thing waits for you\n", status)
        self.assertRegex(status, r"\n  ! agent builder, in [a-z, -]+ \(repo app\)\n    The copies were edited differently\.\n"
                                 r"    Pick a version +agt share resolve builder --repo \S+/app\n")
        self.share("sync")  # a sync saves the repo's waits
        self.assertEqual(watch.badge(), "share:1!")  # the badge reads them too, without git

    def test_edits_in_a_repo_spread_and_status_names_the_repo(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        kiro = self.repo / ".kiro" / "agents" / "builder.json"
        data = json.loads(kiro.read_text())
        data["prompt"] = "Builds the app, fast."
        kiro.write_text(json.dumps(data, indent=2) + "\n")
        age(kiro)
        status = self.share("status")
        self.assertIn("agt share  on, 2 changes to write\n", status)  # to claude and codex
        self.assertRegex(status, r"\n  Repos +app  \d+ in sync, 2 to write\n")
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

    def repo_files(self):
        return {str(path): os.readlink(path) if path.is_symlink() else ("dir" if path.is_dir() else path.read_bytes())
                for path in sorted(self.repo.rglob("*")) if ".git" not in path.relative_to(self.repo).parts}

    def exclude(self):
        return (self.repo / ".git" / "info" / "exclude").read_text()

    def test_cli_remove_takes_that_cli_out_of_listed_repos_too(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        kiro = self.repo / ".kiro" / "agents" / "builder.json"
        self.assertTrue(kiro.exists())
        out = self.share("cli", "remove", "kiro", "--remove-copies")
        self.assertIn("app", out)
        self.assertFalse(kiro.exists())
        self.assertFalse(os.path.lexists(self.repo / ".kiro" / "skills" / "release-notes"))
        self.assertNotIn("/.kiro/", self.exclude())
        self.assertTrue((self.repo / ".claude" / "agents" / "builder.md").exists())
        self.assertTrue(list((self.agt_home / "state" / "share" / "projects").rglob("builder.json")))  # backed up
        self.share("sync")
        self.assertFalse(kiro.exists())  # it doesn't come back
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_cli_add_fills_listed_repos_at_once(self):
        self.enable()
        self.share("cli", "remove", "kiro", "--remove-copies")
        self.share("repo", "add", str(self.repo))
        kiro = self.repo / ".kiro" / "agents" / "builder.json"
        self.assertFalse(kiro.exists())
        self.share("cli", "add", "kiro")
        self.assertTrue(kiro.exists())  # not at the next sync: now

    def test_removing_one_of_two_claude_folders_leaves_repos_alone(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        before = self.repo_files()
        self.share("cli", "remove", "claude-lab", "--remove-copies")
        self.assertEqual(self.repo_files(), before)  # the repo's Claude folder still shares, through ~/.claude

    def test_kinds_remove_takes_the_kind_out_of_listed_repos_too(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        self.share("kinds", "remove", "skills", "--remove-copies")
        self.assertFalse(os.path.lexists(self.repo / ".kiro" / "skills" / "release-notes"))
        self.assertFalse(os.path.lexists(self.repo / ".agents" / "skills" / "release-notes"))
        self.assertTrue((self.repo / ".claude" / "skills" / "release-notes" / "SKILL.md").exists())  # your original
        self.assertTrue((self.repo / ".kiro" / "agents" / "builder.json").exists())  # agents still share

    def test_one_answer_covers_your_folders_and_the_repos(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        for name in ("dana", "day-plan"):  # then Codex holds no copy of yours that agt made
            self.share("rm", name)
        out = json.loads(self.share("cli", "remove", "codex", "--json", code=3))
        self.assertEqual(out["error"], "needs_choice")
        self.assertIn(os.path.realpath(self.repo), {entry.get("repo") for entry in out["copies"]})
        builder = self.repo / ".codex" / "agents" / "builder.toml"
        self.assertTrue(builder.exists())
        self.share("cli", "remove", "codex", "--keep-copies")
        self.assertTrue(builder.exists())  # kept as your own file
        self.assertNotIn("/.codex/agents/builder.toml", self.exclude())

    def test_a_repo_in_the_middle_of_a_merge_stops_a_turn_off(self):
        self.enable()
        self.share("repo", "add", str(self.repo))
        (self.repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.share("cli", "remove", "kiro", "--remove-copies", code=2)
        self.assertIn("middle of a merge", err.getvalue())
        self.assertTrue((self.repo / ".kiro" / "agents" / "builder.json").exists())
        self.assertIn("kiro", [folder.label for folder in settings.load().folders])  # nothing changed

    def agents_md(self, text="Use tabs.\n"):
        (self.repo / "AGENTS.md").write_text(text, encoding="utf-8")
        self.git("add", "AGENTS.md")
        self.git("commit", "-qm", "agents md")

    def test_a_repo_with_only_agents_md_gets_a_claude_md_that_imports_it(self):
        self.agents_md()
        self.enable()
        self.share("repo", "add", str(self.repo))
        claude_md = self.repo / "CLAUDE.md"
        self.assertFalse(claude_md.exists())  # instructions don't share yet
        self.share("kinds", "add", "instructions")
        self.assertEqual(claude_md.read_text(encoding="utf-8"), "@AGENTS.md\n")
        self.assertIn("/CLAUDE.md", self.exclude())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.share("sync")
        self.assertEqual(claude_md.read_text(encoding="utf-8"), "@AGENTS.md\n")
        self.git("rm", "-q", "AGENTS.md")  # a branch without AGENTS.md
        self.git("commit", "-qm", "no agents md")
        self.share("sync")
        self.assertFalse(claude_md.exists())
        self.assertTrue(list((self.agt_home / "state" / "share" / "projects").rglob("CLAUDE.md")))  # backed up
        self.git("checkout", "-q", "HEAD~1")
        self.share("sync")
        self.assertTrue(claude_md.exists())  # and back with it
        out = self.share("kinds", "remove", "instructions")
        self.assertFalse(claude_md.exists())
        self.assertNotIn("/CLAUDE.md", self.exclude())
        self.assertIn("CLAUDE.md", out)

    def test_turning_instructions_off_in_a_worker_writes_nothing(self):
        self.agents_md()
        self.enable("--instructions", "block")
        self.share("repo", "add", str(self.repo))
        with patch.dict(os.environ, {"AGT_DEPTH": "1"}):
            self.share("kinds", "remove", "instructions", code=2)
        self.assertTrue((self.repo / "CLAUDE.md").exists())
        self.assertEqual(settings.load().instructions, "block")

    def test_a_claude_md_of_the_repos_own_is_left_alone(self):
        self.agents_md()
        self.enable("--instructions", "block")
        (self.repo / ".claude" / "CLAUDE.md").write_text("Claude's own.\n", encoding="utf-8")
        self.share("repo", "add", str(self.repo))
        self.assertFalse((self.repo / "CLAUDE.md").exists())
        (self.repo / ".claude" / "CLAUDE.md").unlink()
        (self.repo / "CLAUDE.md").write_text("Mine.\n", encoding="utf-8")
        self.share("sync")
        self.assertEqual((self.repo / "CLAUDE.md").read_text(encoding="utf-8"), "Mine.\n")
        self.assertNotIn("CLAUDE.md", self.exclude())

    def test_an_edited_import_is_yours(self):
        self.agents_md()
        self.enable("--instructions", "block")
        self.share("repo", "add", str(self.repo))
        claude_md = self.repo / "CLAUDE.md"
        claude_md.write_text("@AGENTS.md\n\nAlso: run the linter.\n", encoding="utf-8")
        out = self.share("sync")
        self.assertIn("it's yours now", out)
        self.assertNotIn("/CLAUDE.md", self.exclude())
        self.share("kinds", "remove", "instructions")
        self.assertTrue(claude_md.exists())

    def test_repo_remove_keeps_or_removes_the_import(self):
        self.agents_md()
        self.enable("--instructions", "block")
        self.share("repo", "add", str(self.repo))
        self.share("repo", "remove", str(self.repo), "--keep-copies")
        self.assertTrue((self.repo / "CLAUDE.md").exists())
        self.assertNotIn("/CLAUDE.md", self.exclude())
        (self.repo / "CLAUDE.md").unlink()
        self.share("repo", "add", str(self.repo))
        self.assertTrue((self.repo / "CLAUDE.md").exists())
        self.share("repo", "remove", str(self.repo), "--remove-copies")
        self.assertFalse((self.repo / "CLAUDE.md").exists())

    def test_the_watcher_sees_an_agents_md_added_later(self):
        self.enable("--instructions", "block")
        self.share("repo", "add", str(self.repo))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            watch.run_once()
            watch.run_once()
        (self.repo / "AGENTS.md").write_text("Use tabs.\n", encoding="utf-8")
        watch.background_sync("watcher")
        self.assertTrue((self.repo / "CLAUDE.md").exists())

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
