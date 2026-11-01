"""agt share's write path: enable --yes, sync, the journal, backups, git exclude and the state."""

import contextlib
import io
import json
import os
import stat
import subprocess
import time
import tomllib
from unittest.mock import patch

from helpers import set_times
from test_share_plan import FLAGS, MacCopy

from agents_router import fsutil
from agents_router.share import apply, commands, settings, state

YES = ["enable", *FLAGS, "--resolve", "gamma-apikey=skip", "--yes"]


def age(*paths, seconds=60):
    """Make files look saved a while ago, so agt doesn't wait for them to settle."""

    then = time.time() - seconds
    for path in paths:
        set_times(path, then)


class SyncTests(MacCopy):
    def enable(self, *extra, code=0):
        result, out = self.run_share(*YES, *extra)
        self.assertEqual(result, code, out)
        return out

    def sync(self, *extra, code=0):
        result, out = self.run_share("sync", *extra)
        self.assertEqual(result, code, out)
        return out

    def record(self, key):
        return json.loads((self.agt_home / "state" / "share" / "state.json").read_text())["items"].get(key)

    def kiro(self, name):
        return json.loads((self.user / ".kiro" / "agents" / f"{name}.json").read_text(encoding="utf-8"))

    def test_enable_writes_the_plan_and_a_second_run_does_nothing(self):
        out = self.enable()
        self.assertIn("agt share enable  ✓ wrote 9 changes in 4 folders\n", out)
        kiro, codex = self.user / ".kiro", self.user / ".codex"
        for path in (self.lab / "agents" / "dana.md", codex / "agents" / "dana.toml", codex / "agents" / "day-plan.toml",
                     kiro / "agents" / "dana.json", kiro / "agents" / "day-plan.json", kiro / "agents" / "explorer.json"):
            self.assertTrue(path.is_file(), path)
        self.assertFalse((codex / "agents" / "explorer.toml").exists())
        links = {kiro / "skills" / "ask-simply": self.claude / "skills" / "ask-simply",
                 kiro / "skills" / "interactive-lesson-site": self.lab / "skills" / "interactive-lesson-site",
                 self.claude / "skills" / "interactive-lesson-site": self.lab / "skills" / "interactive-lesson-site"}
        for link, real in links.items():
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(link.resolve(), real.resolve())
        # gamma-apikey isn't shared: both versions stay, and Kiro doesn't get it.
        self.assertIn("Old steps.", (self.claude / "skills" / "gamma-apikey" / "SKILL.md").read_text())
        self.assertIn("New steps.", (self.lab / "skills" / "gamma-apikey" / "SKILL.md").read_text())
        self.assertFalse(os.path.lexists(kiro / "skills" / "gamma-apikey"))
        saved = settings.load()
        self.assertTrue(saved.enabled)
        self.assertEqual(saved.off, {"skills/gamma-apikey"})
        self.assertEqual([folder.label for folder in saved.folders], ["claude-lab", "claude", "codex", "kiro"])
        self.assertIsNone(self.record("skills/gamma-apikey"))
        self.assertEqual(sorted(self.record("agents/dana")["made"]), ["claude-lab", "codex", "kiro"])
        self.assertEqual(sorted(self.record("agents/dana")["copies"]), ["claude", "claude-lab", "codex", "kiro"])
        self.assertEqual(self.record("skills/ask-simply")["made"], ["kiro"])
        self.assertNotIn("made", self.record("skills/synced") or {})
        before = self.snapshot()
        self.assertIn("✓ everything is in sync, nothing to write", self.sync())
        self.assertEqual(self.snapshot(), before)
        code, out = self.run_share("status")
        self.assertEqual(code, 0, out)
        self.assertIn("agt share  on, all in sync\n", out)

    def test_each_copy_gets_what_its_cli_can_use(self):
        self.enable()
        # Another Claude folder gets the file as it is, model and all.
        self.assertEqual((self.lab / "agents" / "dana.md").read_bytes(), (self.claude / "agents" / "dana.md").read_bytes())
        # Kiro leaves out the tools of servers it doesn't have.
        self.assertEqual(self.kiro("dana")["tools"], ["execute_bash", "fs_read", "fs_write"])
        self.assertEqual(self.kiro("dana")["prompt"], "Dana's long instructions.")
        codex = tomllib.loads((self.user / ".codex" / "agents" / "dana.toml").read_text(encoding="utf-8"))
        self.assertEqual(set(codex), {"name", "description", "developer_instructions"})
        mode = stat.S_IMODE((self.user / ".kiro" / "agents" / "dana.json").stat().st_mode)
        self.assertEqual(mode, 0o666 & ~_umask())

    def test_if_plan_refuses_a_changed_plan(self):
        _, out = self.run_share("enable", *FLAGS, "--resolve", "gamma-apikey=skip", "--dry-run", "--json")
        digest = json.loads(out)["plan"]
        self.agent(self.claude, "dana", body="Edited after the dry run.")
        age(self.claude / "agents" / "dana.md")
        before = self.snapshot()
        out = self.enable("--if-plan", digest, code=1)
        self.assertIn("the plan changed since your dry run", out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(settings.path().exists())

    def test_an_edit_in_kiro_spreads_to_every_copy(self):
        self.enable()
        path = self.user / ".kiro" / "agents" / "explorer.json"
        data = self.kiro("explorer")
        data["prompt"] = "Explore, carefully."
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        age(path)
        out = self.sync()
        self.assertIn("wrote 2 changes", out)
        for folder in (self.claude, self.lab):
            text = (folder / "agents" / "explorer.md").read_text(encoding="utf-8")
            self.assertIn("Explore, carefully.", text)
            self.assertIn("tools: Bash, Read, mcp__brave-search", text)  # a copy's own fields stay
        backups = list((self.agt_home / "state" / "share" / "backups").rglob("explorer.md"))
        self.assertEqual(len(backups), 2)
        self.assertIn("\nExplore.\n", backups[0].read_text(encoding="utf-8"))
        self.assertIn("everything is in sync", self.sync())

    def test_different_edits_are_a_conflict_and_write_nothing(self):
        self.enable()
        self.agent(self.claude, "day-plan", body="Wrap it one way.")
        self.agent(self.lab, "day-plan", body="Wrap it another way.")
        age(self.claude / "agents" / "day-plan.md", self.lab / "agents" / "day-plan.md")
        before = self.snapshot()
        out = self.sync()
        self.assertIn("  1 thing waits for you: `agt share status` lists it.\n", out)
        self.assertEqual(self.snapshot(), before)
        code, out = self.run_share("status", "--diff")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"\n    [-+]Wrap it one way\.")
        self.assertRegex(out, r"\n    [-+]Wrap it another way\.")
        age(self.claude / "agents" / "day-plan.md", seconds=120)  # the claude-lab copy is the newer one
        out = self.run_share("status", "--diff")[1]
        self.assertRegex(out, r"\n    \+\+\+ claude-lab\S* ~/\.claude-lab/agents/day-plan\.md\n")  # toward the newest
        self.assertIn("\n    -Wrap it one way.\n", out)
        self.assertIn("\n    +Wrap it another way.\n", out)

    def test_deleting_a_copy_agt_made_stops_sharing_it_there(self):
        self.enable()
        (self.user / ".kiro" / "agents" / "explorer.json").unlink()
        self.sync()
        self.assertFalse((self.user / ".kiro" / "agents" / "explorer.json").exists())
        self.assertEqual(self.record("agents/explorer")["stopped"], ["kiro"])
        self.assertTrue((self.claude / "agents" / "explorer.md").exists())
        self.assertIn("everything is in sync", self.sync())

    def test_deleting_an_original_waits_for_you(self):
        self.enable()
        (self.claude / "agents" / "day-plan.md").unlink()
        before = self.snapshot()
        self.sync()
        self.assertEqual(self.snapshot(), before)
        code, out = self.run_share("status")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"\n  ! agent day-plan\n    Its original was deleted in claude\.\n    Remove its copies +agt share rm day-plan\n")

    def test_a_failed_write_then_a_rerun_reverts_nothing(self):
        self.enable()
        self.agent(self.claude, "dana", body="Dana, edited.", tools="Bash, Read")
        age(self.claude / "agents" / "dana.md")
        real = fsutil.write_preserving
        calls = []

        def flaky(path, data, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            return real(path, data, **kwargs)

        with patch.object(fsutil, "write_preserving", flaky):
            out = self.sync(code=1)
        self.assertIn("No space left on device", out)
        self.sync()
        for path in (self.lab / "agents" / "dana.md", self.user / ".codex" / "agents" / "dana.toml",
                     self.user / ".kiro" / "agents" / "dana.json"):
            self.assertIn("Dana, edited.", path.read_text(encoding="utf-8"), path)
        self.assertIn("Dana, edited.", (self.claude / "agents" / "dana.md").read_text(encoding="utf-8"))
        self.assertIn("everything is in sync", self.sync())

    def test_an_edit_made_while_agt_works_is_kept(self):
        self.enable()
        self.agent(self.claude, "explorer", body="Explore, v2.", tools="Bash, Read, mcp__brave-search")
        age(self.claude / "agents" / "explorer.md")
        build = commands.planner.build
        target = self.user / ".kiro" / "agents" / "explorer.json"

        def build_then_edit(*args, **kwargs):
            plan = build(*args, **kwargs)
            data = self.kiro("explorer")
            data["description"] = "Edited in Kiro meanwhile."
            target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return plan

        with patch.object(commands.planner, "build", build_then_edit):
            out = self.sync(code=1)
        self.assertIn("changed while agt was working", out)
        self.assertEqual(self.kiro("explorer")["description"], "Edited in Kiro meanwhile.")
        age(target)
        out = self.sync()  # both copies changed now, differently: that's for you to decide
        self.assertIn("  1 thing waits for you: `agt share status` lists it.\n", out)
        self.assertEqual(self.kiro("explorer")["description"], "Edited in Kiro meanwhile.")

    def test_an_interrupted_run_is_finished_from_the_journal(self):
        current = state.State()
        path = self.user / ".kiro" / "agents" / "dana.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        new = apply.fingerprint(path, "agents")
        fsutil.append_synced(apply.journal_path(), json.dumps(
            {"run": "r1", "step": "write", "key": "agents/dana", "kind": "agents", "labels": ["kiro"], "action": "copy",
             "path": str(path), "old": None, "new": new}))
        found = apply.replay(current)
        self.assertEqual(found, [f"finished an interrupted write: {path}"])
        self.assertEqual(current.items["agents/dana"], {"made": ["kiro"]})
        self.assertIsNone(current.last("agents", "dana"))  # its first sync didn't finish
        self.assertFalse(apply.journal_path().exists())

    def test_copies_in_a_git_repo_go_in_its_exclude_file(self):
        git = lambda *args: subprocess.run(["git", "-C", str(self.claude), *args], capture_output=True, text=True, check=False)
        self.assertEqual(git("init", "-q").returncode, 0)
        self.enable()
        exclude = (self.claude / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn("/skills/interactive-lesson-site\n", exclude)
        self.assertEqual(git("check-ignore", "-q", "skills/interactive-lesson-site").returncode, 0)
        self.assertNotEqual(git("check-ignore", "-q", "skills/ask-simply").returncode, 0)  # yours, not agt's
        self.assertNotIn("interactive-lesson-site", git("status", "--porcelain").stdout)
        self.enable()
        self.assertEqual((self.claude / ".git" / "info" / "exclude").read_text(encoding="utf-8").count("interactive-lesson-site"), 1)

    def test_modes_and_hard_links_survive_an_update(self):
        self.enable()
        original = self.claude / "agents" / "day-plan.md"
        original.chmod(0o600)
        other = self.root / "elsewhere.md"
        os.link(original, other)
        kiro = self.user / ".kiro" / "agents" / "day-plan.json"
        data = self.kiro("day-plan")
        data["prompt"] = "Wrap the day, then rest."
        kiro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        age(kiro)
        self.sync()
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o600)
        self.assertIn("Wrap the day, then rest.", other.read_text(encoding="utf-8"))

    def test_a_relative_skill_link_is_seen_from_any_folder(self):
        link = self.lab / "skills" / "ask-simply"
        link.unlink()
        os.symlink(os.path.relpath(self.claude / "skills" / "ask-simply", link.parent), link, target_is_directory=True)
        start = os.getcwd()
        os.chdir("/")
        try:
            self.enable()
        finally:
            os.chdir(start)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.record("skills/ask-simply")["made"], ["kiro"])

    def test_one_run_at_a_time_per_set_of_folders(self):
        self.enable()
        locks = list((self.root / "tmp").glob("agents-router-share-*.lock"))
        self.assertEqual(len(locks), 1)
        handle = fsutil.try_lock(locks[0])
        err = io.StringIO()
        quick = apply.lock
        try:
            with patch.object(apply, "lock", lambda folders, timeout=30.0: quick(folders, timeout=0.1)), \
                    contextlib.redirect_stderr(err):
                self.sync(code=1)
        finally:
            fsutil.unlock(handle)
        self.assertIn("another agt share run is using these folders", err.getvalue())

    def test_workers_never_write(self):
        before = self.snapshot()
        with patch.dict(os.environ, {"AGT_JOB_ID": "job-1"}):
            out = self.enable(code=2)
        self.assertIn("doesn't write inside agt's workers", out)
        self.assertEqual(self.snapshot(), before)

    def test_an_unreadable_state_stops_the_sync(self):
        self.enable()
        (self.agt_home / "state" / "share" / "state.json").write_text("{not json", encoding="utf-8")
        (self.user / ".kiro" / "agents" / "explorer.json").unlink()
        before = self.snapshot()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.sync(code=1)
        self.assertIn("state.json is damaged", err.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_new_items_wait_when_auto_copy_is_off(self):
        self.enable("--auto-copy", "off")
        self.agent(self.claude, "reviewer", body="Review.")
        age(self.claude / "agents" / "reviewer.md")
        before = self.snapshot()
        self.assertIn("nothing to write", self.sync())
        self.assertEqual(self.snapshot(), before)
        _, out = self.run_share("sync", "--dry-run")
        self.assertIn("reviewer", out)
        # An edit still reaches the copies that exist.
        self.agent(self.claude, "day-plan", body="Wrap the day, quickly.", tools="Bash, mcp__tracker__tracker_log_work")
        age(self.claude / "agents" / "day-plan.md")
        self.sync()
        self.assertEqual(self.kiro("day-plan")["prompt"], "Wrap the day, quickly.")

    def test_a_missing_kind_folder_is_left_alone(self):
        self.enable()
        for path in (self.user / ".codex" / "agents").iterdir():
            path.unlink()
        (self.user / ".codex" / "agents").rmdir()
        out = self.sync()
        self.assertIn("codex's agents folder is missing", out)
        self.assertFalse((self.user / ".codex" / "agents").exists())
        self.assertNotIn("stopped", self.record("agents/dana"))

    def test_many_disappearing_at_once_freezes_deletes(self):
        for index in range(5):
            self.skill(self.lab, f"extra-{index}")
        age(*self.lab.rglob("*"))
        self.enable()
        for index in range(4):
            (self.user / ".kiro" / "skills" / f"extra-{index}").unlink()
        # An edit in the frozen folder isn't recorded as synced, so it's never overwritten later.
        ask = self.user / ".kiro" / "skills" / "ask-simply"
        before = self.record("skills/ask-simply")["copies"]["kiro"]
        ask.unlink()
        self.skill(self.user / ".kiro", "ask-simply", body="Ask, from a frozen Kiro.")
        age(ask, *ask.rglob("*"))
        out = self.sync()
        self.assertIn("disappeared from kiro at once", out)
        self.assertNotIn("stopped", self.record("skills/extra-0"))
        self.assertEqual(self.record("skills/ask-simply")["copies"]["kiro"], before)
        self.assertNotIn("frozen Kiro", (self.claude / "skills" / "ask-simply" / "SKILL.md").read_text(encoding="utf-8"))


class SettingsFileTests(MacCopy):
    def test_saving_keeps_every_other_line(self):
        path = settings.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# mine\nmax_depth = 2\n\n[share]\nenabled = false\n\n[share.targets]\n\"agents/x\" = \"off\"\n\n"
                        "[claude]\nprimary = \"~/.claude\"\n", encoding="utf-8")
        saved = settings.Settings(enabled=True, off={"skills/gamma-apikey"}, not_in={"agents/dana": ["codex"]},
                                  only={"agents/explorer": ["kiro"]},
                                  folders=[settings.Folder("claude", self.claude, "claude")])
        settings.save(saved)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# mine\nmax_depth = 2\n"))
        self.assertIn('[claude]\nprimary = "~/.claude"', text)
        self.assertNotIn("agents/x", text)
        loaded = settings.load()
        self.assertEqual((loaded.enabled, loaded.off, loaded.not_in, loaded.only),
                         (True, {"skills/gamma-apikey"}, {"agents/dana": ["codex"]}, {"agents/explorer": ["kiro"]}))
        self.assertEqual(tomllib.loads(text)["max_depth"], 2)

    def test_a_bad_value_names_the_file(self):
        path = settings.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[share]\ntool_widening = \"always\"\n", encoding="utf-8")
        with self.assertRaisesRegex(settings.SettingsError, "settings.toml: \\[share\\] tool_widening"):
            settings.load()

    def test_the_file_follows_agt_home_without_a_config_dir(self):
        with patch.dict(os.environ, {"AGT_CONFIG_DIR": ""}):
            self.assertEqual(settings.path(), self.agt_home / "settings.toml")


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


class WritePreservingTests(MacCopy):
    def test_a_new_file_never_replaces_one_that_appeared(self):
        path = self.root / "new.md"
        fsutil.write_preserving(path, b"first", new=True)
        with self.assertRaises(FileExistsError):
            fsutil.write_preserving(path, b"second", new=True)
        self.assertEqual(path.read_bytes(), b"first")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])

    def test_without_hard_links_a_new_file_still_lands(self):
        path = self.root / "new.md"
        with patch.object(os, "link", side_effect=PermissionError(1, "Operation not permitted")):
            fsutil.write_preserving(path, b"data", new=True)
            with self.assertRaises(FileExistsError):
                fsutil.write_preserving(path, b"other", new=True)
        self.assertEqual(path.read_bytes(), b"data")

    def test_an_update_goes_through_a_link_to_the_real_file(self):
        real = self.root / "real.md"
        real.write_bytes(b"old")
        real.chmod(0o640)
        link = self.root / "link.md"
        os.symlink(real, link)
        fsutil.write_preserving(link, b"new")
        self.assertTrue(link.is_symlink())
        self.assertEqual((real.read_bytes(), stat.S_IMODE(real.stat().st_mode)), (b"new", 0o640))
