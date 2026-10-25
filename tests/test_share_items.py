"""agt share's item commands: pick, resolve, rm and undo, on a copy of this Mac's folders."""

import builtins
import contextlib
import io
import json
import os
import subprocess
from unittest.mock import patch

from test_share_plan import FLAGS, MacCopy
from test_share_sync import YES, age

from agents_router.share import apply, commands, settings


class ItemBase(MacCopy):
    def enable(self, *extra, code=0, flags=YES):
        result, out = self.run_share(*flags, *extra)
        self.assertEqual(result, code, out)
        return out

    def share(self, *args, code=0):
        result, out = self.run_share(*args)
        self.assertEqual(result, code, out)
        return out

    def record(self, key):
        return json.loads((self.agt_home / "state" / "share" / "state.json").read_text())["items"].get(key)

    def kiro(self, name):
        return json.loads((self.user / ".kiro" / "agents" / f"{name}.json").read_text(encoding="utf-8"))

    def write_kiro(self, name, **fields):
        path = self.user / ".kiro" / "agents" / f"{name}.json"
        data = {**self.kiro(name), **fields}
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        age(path)
        return path

    def backups(self, name):
        return sorted((self.agt_home / "state" / "share" / "backups").rglob(name))

    def folders(self):
        """The CLI folders only: a pick saves your answer in ~/.agt/settings.toml on purpose."""

        return {path: value for path, value in self.snapshot().items() if not path.startswith(str(self.user / ".agt"))}


class PickTests(ItemBase):
    def test_not_in_removes_agts_copy_with_a_backup_and_undo_brings_it_back(self):
        self.enable()
        kiro = self.user / ".kiro" / "agents" / "dana.json"
        before = kiro.read_bytes()
        out = self.share("pick", "dana", "--not-in", "kiro")
        self.assertIn("- agents/dana in kiro", out)
        self.assertIn("`agt share undo dana --in kiro` brings a removed copy back", out)
        self.assertFalse(kiro.exists())
        self.assertEqual([path.read_bytes() for path in self.backups("dana.json")], [before])
        self.assertEqual(settings.load().not_in, {"agents/dana": ["kiro"]})
        self.assertNotIn("kiro", self.record("agents/dana")["copies"])
        self.assertNotIn("kiro", self.record("agents/dana")["made"])
        self.assertIn("everything is in sync", self.share("sync"))  # never refilled
        self.assertFalse(kiro.exists())
        # An edit while it's out, then undo: it comes back and catches up.
        self.agent(self.claude, "dana", body="Dana, edited.", tools="Bash, Read")
        age(self.claude / "agents" / "dana.md")
        self.share("sync")
        out = self.share("undo", "dana", "--in", "kiro")
        self.assertIn("put back", out)
        self.assertEqual(self.kiro("dana")["prompt"], "Dana, edited.")
        self.assertEqual(settings.load().not_in, {})
        self.assertIn("kiro", self.record("agents/dana")["made"])
        self.assertIn("everything is in sync", self.share("sync"))

    def test_keep_copies_keeps_it_as_your_own_file(self):
        self.enable()
        out = self.share("pick", "day-plan", "--not-in", "kiro", "--keep-copies")
        self.assertIn("day-plan in kiro: kept as your own file (--keep-copies)", out)
        self.assertTrue((self.user / ".kiro" / "agents" / "day-plan.json").exists())
        self.assertEqual(self.record("agents/day-plan")["released"], ["kiro"])
        self.agent(self.claude, "day-plan", body="Wrap the day, v2.", tools="Bash, mcp__tracker__tracker_log_work")
        age(self.claude / "agents" / "day-plan.md")
        self.share("sync")
        self.assertEqual(self.kiro("day-plan")["prompt"], "Wrap the day.")  # agt no longer updates it
        # Picked back after it fell behind: it differs, so it's for you to decide.
        out = self.share("pick", "day-plan", "--in", "kiro", code=0)
        self.assertIn("day-plan (agents)", out)
        self.assertEqual(self.kiro("day-plan")["prompt"], "Wrap the day.")
        self.assertNotIn("released", self.record("agents/day-plan"))

    def test_a_kept_copy_you_delete_stays_out_while_the_pick_says_so(self):
        self.enable()
        self.share("pick", "day-plan", "--not-in", "kiro", "--keep-copies")
        (self.user / ".kiro" / "agents" / "day-plan.json").unlink()
        self.share("sync")
        self.assertFalse((self.user / ".kiro" / "agents" / "day-plan.json").exists())

    def test_a_copy_with_an_edit_the_others_dont_have_is_kept(self):
        self.enable()
        self.write_kiro("explorer", prompt="Explore my way.")
        out = self.share("pick", "explorer", "--not-in", "kiro")
        self.assertIn("it has an edit the other copies don't have, so it stays as your own file", out)
        self.assertEqual(self.kiro("explorer")["prompt"], "Explore my way.")
        self.assertIn("\nExplore.\n", (self.claude / "agents" / "explorer.md").read_text(encoding="utf-8"))

    def test_your_original_is_never_removed(self):
        self.enable()
        before = self.folders()
        out = self.share("pick", "dana", "--not-in", "claude")
        self.assertIn("dana in claude: your original: agt never removes it, so it stays shared", out)
        self.assertEqual(self.folders(), before)

    def test_codex_keeps_a_skill_it_reads_through_claude_labs_folder(self):
        self.enable()
        before = self.folders()
        out = self.share("pick", "interactive-lesson-site", "--not-in", "codex")
        self.assertIn("codex reads claude-lab's folder, so it keeps seeing it", out)
        self.assertEqual(self.folders(), before)

    def test_a_skill_link_is_removed_as_a_link(self):
        git = lambda *args: subprocess.run(["git", "-C", str(self.claude), *args], capture_output=True, text=True, check=False)
        self.assertEqual(git("init", "-q").returncode, 0)
        self.enable()
        link = self.claude / "skills" / "interactive-lesson-site"
        self.share("pick", "interactive-lesson-site", "--not-in", "claude")
        self.assertFalse(os.path.lexists(link))
        self.assertTrue((self.lab / "skills" / "interactive-lesson-site" / "SKILL.md").is_file())  # the original stays
        (backup,) = self.backups("interactive-lesson-site")
        self.assertTrue(backup.is_symlink())
        self.assertNotIn("interactive-lesson-site", (self.claude / ".git" / "info" / "exclude").read_text(encoding="utf-8"))
        self.share("undo", "interactive-lesson-site", "--in", "claude")
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (self.lab / "skills" / "interactive-lesson-site").resolve())
        self.assertIn("/skills/interactive-lesson-site", (self.claude / ".git" / "info" / "exclude").read_text(encoding="utf-8"))

    def test_only_removes_agts_copies_everywhere_else(self):
        self.enable()
        self.share("pick", "dana", "--only", "claude,claude-lab")
        self.assertFalse((self.user / ".codex" / "agents" / "dana.toml").exists())
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertTrue((self.lab / "agents" / "dana.md").exists())
        self.assertEqual(settings.load().only, {"agents/dana": ["claude", "claude-lab"]})
        self.share("pick", "dana", "--in", "kiro")
        self.assertEqual(settings.load().only, {"agents/dana": ["claude", "claude-lab", "kiro"]})
        self.assertTrue((self.user / ".kiro" / "agents" / "dana.json").exists())

    def test_a_pick_writes_only_that_item(self):
        self.enable()
        self.write_kiro("day-plan", prompt="Wrap it, from Kiro.")
        self.share("pick", "dana", "--not-in", "codex")
        self.assertNotIn("from Kiro", (self.claude / "agents" / "day-plan.md").read_text(encoding="utf-8"))
        self.share("sync")
        self.assertIn("from Kiro", (self.claude / "agents" / "day-plan.md").read_text(encoding="utf-8"))

    def test_with_no_flags_it_shows_the_table_and_writes_nothing(self):
        self.enable()
        before = self.snapshot()
        with patch.object(commands, "_interactive", lambda: True):
            out = self.share("pick", "dana")
        self.assertIn("agt share pick: what goes where. Nothing was changed.", out)
        self.assertIn("dana", out)
        out = self.share("pick", "--json", code=3)
        answer = json.loads(out)
        self.assertEqual(answer["error"], "needs_choice")
        self.assertIn("dana", [choice["name"] for choice in answer["choices"]])
        self.assertEqual(self.snapshot(), before)

    def test_a_new_item_waiting_for_targets_goes_where_you_pick(self):
        self.enable("--auto-copy", "off")
        self.agent(self.claude, "reviewer", body="Review.")
        age(self.claude / "agents" / "reviewer.md")
        self.share("sync")
        self.share("pick", "reviewer", "--in", "kiro")
        self.assertTrue((self.user / ".kiro" / "agents" / "reviewer.json").exists())
        self.assertFalse((self.lab / "agents" / "reviewer.md").exists())
        self.assertEqual(settings.load().only, {"agents/reviewer": ["claude", "kiro"]})

    def test_a_dry_run_shows_the_removal(self):
        self.enable()
        settings_now = settings.load()
        settings_now.not_in["agents/dana"] = ["kiro"]
        settings.save(settings_now)
        before = self.snapshot()
        out = self.share("sync", "--dry-run")
        self.assertIn("rm removed, with a backup", out)
        self.assertNotIn("Would save", out)  # a sync saves nothing; these picks are saved already
        self.assertEqual(self.snapshot(), before)
        settings_now.not_in.clear()
        settings.save(settings_now)
        out = self.share("sync", "--dry-run")
        self.assertIn("0 changes in 0 folders.", out)
        self.assertNotIn("To apply it", out)

    def test_unknown_names_and_labels_are_refused(self):
        self.enable()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.share("pick", "nobody", "--not-in", "kiro", code=2)
            self.share("pick", "dana", "--not-in", "gemini", code=2)
        self.assertIn("no agent or skill named nobody", err.getvalue())
        self.assertIn("no folder labelled gemini", err.getvalue())

    def test_an_interrupted_removal_is_finished_from_the_journal(self):
        self.enable()
        path = self.user / ".kiro" / "agents" / "dana.json"
        real_move = apply.shutil.move

        def move_then_stop(source, destination):
            real_move(source, destination)
            raise KeyboardInterrupt

        with patch.object(apply.shutil, "move", move_then_stop):
            self.assertEqual(self.run_share("pick", "dana", "--not-in", "kiro")[0], 130)  # cut off mid-run
        self.assertFalse(path.exists())
        out = self.share("sync")
        self.assertIn(f"finished an interrupted write: {path}", out)
        self.assertNotIn("kiro", self.record("agents/dana")["copies"])
        self.assertNotIn("stopped", self.record("agents/dana"))


class ResolveTests(ItemBase):
    def setUp(self):
        super().setUp()
        self.enable(flags=["enable", *FLAGS, "--yes", "--accept-suggested"])  # gamma-apikey stays a conflict

    def test_without_an_answer_an_agent_gets_needs_choice(self):
        before = self.snapshot()
        out = self.share("resolve", "gamma-apikey", "--json", code=3)
        answer = json.loads(out)
        self.assertEqual(answer["error"], "needs_choice")
        self.assertEqual(answer["retry"], "agt share resolve gamma-apikey --prefer <choice>")
        values = {choice["value"]: choice.get("newer") for choice in answer["choices"][0]["choices"]}
        self.assertEqual(values, {"claude-lab": True, "claude": False, "skip": None})
        # An agent with a terminal still never gets a prompt.
        with patch.dict(os.environ, {"CLAUDECODE": "1"}), patch("sys.stdin.isatty", lambda: True), \
                patch("sys.stdout.isatty", lambda: True):
            self.share("resolve", "gamma-apikey", code=3)
        self.assertEqual(self.snapshot(), before)

    def test_prefer_a_label_moves_the_other_version_to_the_backups_and_links(self):
        out = self.share("resolve", "gamma-apikey", "--prefer", "claude-lab")
        self.assertIn("~ skills/gamma-apikey in claude", out)
        link = self.claude / "skills" / "gamma-apikey"
        self.assertTrue(link.is_symlink())
        self.assertIn("New steps.", (link / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((self.user / ".kiro" / "skills" / "gamma-apikey").is_symlink())
        (backup,) = self.backups("gamma-apikey")
        self.assertIn("Old steps.", (backup / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("everything is in sync", self.share("sync"))
        # Undo brings the conflict back, with both versions as they were.
        self.share("undo", "gamma-apikey")
        self.assertFalse(link.is_symlink())
        self.assertIn("Old steps.", (link / "SKILL.md").read_text(encoding="utf-8"))
        code, out = self.run_share("status")
        self.assertEqual(code, 1)
        self.assertIn("Conflict: gamma-apikey", out)

    def test_in_a_terminal_it_shows_the_difference_and_asks(self):
        answers = iter(["9", "2"])
        with patch.object(commands, "_interactive", lambda: True), \
                patch.object(builtins, "input", lambda prompt: next(answers)):
            out = self.share("resolve", "gamma-apikey")
        self.assertIn("-New steps.", out)
        self.assertIn("+Old steps.", out)
        self.assertIn("Old steps.", (self.lab / "skills" / "gamma-apikey" / "SKILL.md").read_text(encoding="utf-8"))
        with patch.object(commands, "_interactive", lambda: True), patch.object(builtins, "input", lambda prompt: "q"):
            self.assertIn("no conflict", self.share("resolve", "gamma-apikey"))

    def test_quit_changes_nothing(self):
        before = self.snapshot()
        with patch.object(commands, "_interactive", lambda: True), patch.object(builtins, "input", lambda prompt: "q"):
            self.assertIn("Nothing was changed.", self.share("resolve", "gamma-apikey"))
        self.assertEqual(self.snapshot(), before)

    def test_skip_stops_sharing_it(self):
        before = self.folders()
        out = self.share("resolve", "gamma-apikey", "--prefer", "skip")
        self.assertIn("gamma-apikey: not shared any more; every copy stays as it is", out)
        self.assertEqual(settings.load().off, {"skills/gamma-apikey"})
        self.assertEqual(self.folders(), before)
        self.assertIn("isn't shared", self.share("resolve", "gamma-apikey"))

    def test_all_with_newest_and_an_item_without_a_conflict(self):
        self.agent(self.claude, "day-plan", body="Wrap it one way.", tools="Bash")
        self.agent(self.lab, "day-plan", body="Wrap it another way.", tools="Bash")
        age(self.claude / "agents" / "day-plan.md", seconds=20)
        age(self.lab / "agents" / "day-plan.md", seconds=10)
        self.share("sync")
        self.share("resolve", "--all", "--prefer", "newest")
        self.assertIn("another way", (self.claude / "agents" / "day-plan.md").read_text(encoding="utf-8"))
        self.assertIn("New steps.", (self.claude / "skills" / "gamma-apikey" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("no conflict", self.share("resolve", "dana"))
        self.assertIn("no conflicts to resolve", self.share("resolve", "--all", "--prefer", "newest"))

    def test_a_label_without_a_copy_is_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.share("resolve", "gamma-apikey", "--prefer", "kiro", code=2)
        self.assertIn("kiro has no copy of gamma-apikey", err.getvalue())


class RemoveTests(ItemBase):
    def test_rm_moves_every_copy_to_the_backups_and_undo_puts_them_back(self):
        self.enable()
        paths = [self.claude / "agents" / "dana.md", self.lab / "agents" / "dana.md",
                 self.user / ".codex" / "agents" / "dana.toml", self.user / ".kiro" / "agents" / "dana.json"]
        contents = {path: path.read_bytes() for path in paths}
        out = self.share("rm", "dana")
        self.assertIn("agt share rm: wrote 4 changes in 4 folders.", out)
        self.assertIn("`agt share undo dana` puts it back.", out)
        self.assertFalse(any(path.exists() for path in paths))
        self.assertIsNone(self.record("agents/dana"))
        self.assertIn("everything is in sync", self.share("sync"))
        self.share("undo", "dana")
        self.assertEqual({path: path.read_bytes() for path in paths}, contents)
        self.assertIn("everything is in sync", self.share("sync"))

    def test_after_rm_one_copy_alone_doesnt_come_back(self):
        self.enable()
        self.share("rm", "dana")
        before = self.snapshot()
        self.assertIn("comes back everywhere or not at all", self.share("undo", "dana", "--in", "kiro"))
        self.assertEqual(self.snapshot(), before)

    def test_rm_a_skill_removes_the_links_and_the_folder(self):
        self.enable()
        entries = [self.claude / "skills" / "ask-simply", self.lab / "skills" / "ask-simply",
                   self.user / ".kiro" / "skills" / "ask-simply"]
        self.share("rm", "ask-simply")
        self.assertFalse(any(os.path.lexists(path) for path in entries))
        self.share("undo", "ask-simply")
        self.assertTrue((self.claude / "skills" / "ask-simply" / "SKILL.md").is_file())
        for link in entries[1:]:
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(link.resolve(), (self.claude / "skills" / "ask-simply").resolve())
        self.assertIn("everything is in sync", self.share("sync"))

    def test_rm_after_you_deleted_the_original(self):
        self.enable()
        (self.claude / "agents" / "dana.md").unlink()
        self.share("sync")
        code, out = self.run_share("status")
        self.assertEqual(code, 1)
        self.share("rm", "dana")
        code, out = self.run_share("status")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())

    def test_rm_an_item_that_isnt_shared_leaves_it_alone(self):
        self.enable()
        before = self.snapshot()
        out = self.share("rm", "gamma-apikey", code=1)
        self.assertIn("isn't shared", out)
        self.assertEqual(self.snapshot(), before)


class UndoTests(ItemBase):
    def test_undo_puts_back_an_edit_that_spread_and_again_redoes_it(self):
        self.enable()
        self.write_kiro("explorer", prompt="Explore, badly.")
        self.share("sync")
        self.assertIn("Explore, badly.", (self.claude / "agents" / "explorer.md").read_text(encoding="utf-8"))
        self.share("undo", "explorer")
        for path in (self.claude / "agents" / "explorer.md", self.lab / "agents" / "explorer.md"):
            self.assertIn("\nExplore.\n", path.read_text(encoding="utf-8"), path)
        self.assertEqual(self.kiro("explorer")["prompt"], "Explore.")
        self.assertIn("everything is in sync", self.share("sync"))
        self.share("undo", "explorer")
        self.assertEqual(self.kiro("explorer")["prompt"], "Explore, badly.")
        self.assertIn("Explore, badly.", (self.claude / "agents" / "explorer.md").read_text(encoding="utf-8"))

    def test_a_run_that_only_made_copies_has_nothing_to_put_back(self):
        self.enable()
        before = self.snapshot()
        out = self.share("undo", "dana")
        self.assertIn("nothing to put back", out)
        self.assertEqual(self.snapshot(), before)

    def test_undo_in_brings_back_a_copy_you_deleted(self):
        self.enable()
        (self.user / ".kiro" / "agents" / "explorer.json").unlink()
        self.share("sync")
        self.assertEqual(self.record("agents/explorer")["stopped"], ["kiro"])
        self.share("undo", "explorer", "--in", "kiro")
        self.assertEqual(self.kiro("explorer")["prompt"], "Explore.")
        self.assertNotIn("stopped", self.record("agents/explorer"))

    def test_workers_never_write(self):
        self.enable()
        before = self.snapshot()
        with patch.dict(os.environ, {"AGT_DEPTH": "1"}):
            for args in (["pick", "dana", "--not-in", "kiro"], ["rm", "dana"], ["undo", "explorer", "--in", "kiro"]):
                result, out = self.run_share(*args)
                self.assertIn(result, (0, 2), out)
        self.assertEqual(self.snapshot(), before)


class MoveTests(ItemBase):
    def test_mv_an_agent_renames_every_copy_and_keeps_their_own_fields(self):
        git = lambda *args: subprocess.run(["git", "-C", str(self.claude), *args], capture_output=True, text=True, check=False)
        self.assertEqual(git("init", "-q").returncode, 0)
        self.enable()
        self.share("pick", "dana", "--not-in", "codex")
        kiro_tools = self.kiro("dana")["tools"]
        out = self.share("mv", "dana", "reviewer")
        self.assertIn("dana is reviewer now, in every CLI. `agt share mv reviewer dana` renames it back.", out)
        for folder in (self.claude, self.lab):
            self.assertFalse((folder / "agents" / "dana.md").exists())
            text = (folder / "agents" / "reviewer.md").read_text(encoding="utf-8")
            self.assertIn("name: reviewer", text)
            self.assertIn("Dana's long instructions.", text)
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertEqual(self.kiro("reviewer")["name"], "reviewer")
        self.assertEqual(self.kiro("reviewer")["tools"], kiro_tools)
        self.assertIsNone(self.record("agents/dana"))
        self.assertEqual(sorted(self.record("agents/reviewer")["made"]), ["claude-lab", "kiro"])
        self.assertEqual(settings.load().not_in, {"agents/reviewer": ["codex"]})
        self.assertEqual(len(self.backups("dana.md")), 2)
        self.assertIn("everything is in sync", self.share("sync"))
        # agt's copies only: ~/.claude holds the original, so its exclude file never lists it.
        self.assertNotIn("reviewer", (self.claude / ".git" / "info" / "exclude").read_text(encoding="utf-8"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.share("undo", "reviewer", code=2)
        self.assertIn("`agt share mv reviewer dana` renames it back", err.getvalue())
        self.share("mv", "reviewer", "dana")
        self.assertTrue((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertIn("everything is in sync", self.share("sync"))

    def test_mv_keeps_the_model_line_of_a_claude_copy(self):
        self.agent(self.claude, "dana", tools="Bash", body="Dana's long instructions.")
        path = self.claude / "agents" / "dana.md"
        path.write_text(path.read_text(encoding="utf-8").replace("tools: Bash\n", "tools: Bash\nmodel: opus\n"), encoding="utf-8")
        age(path)
        self.enable()
        self.share("mv", "dana", "reviewer")
        for folder in (self.claude, self.lab):
            self.assertIn("model: opus", (folder / "agents" / "reviewer.md").read_text(encoding="utf-8"))

    def test_mv_a_skill_renames_its_folder_and_agts_links(self):
        git = lambda *args: subprocess.run(["git", "-C", str(self.claude), *args], capture_output=True, text=True, check=False)
        self.assertEqual(git("init", "-q").returncode, 0)
        self.enable()
        self.share("mv", "interactive-lesson-site", "lesson-site")
        exclude = (self.claude / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn("/skills/lesson-site\n", exclude)
        self.assertNotIn("interactive-lesson-site", exclude)
        real = self.lab / "skills" / "lesson-site"
        self.assertTrue(real.is_dir() and not real.is_symlink())
        self.assertIn("name: lesson-site", (real / "SKILL.md").read_text(encoding="utf-8"))
        for link in (self.claude / "skills" / "lesson-site", self.user / ".kiro" / "skills" / "lesson-site"):
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(link.resolve(), real.resolve())
        for old in (self.lab, self.claude, self.user / ".kiro"):
            self.assertFalse(os.path.lexists(old / "skills" / "interactive-lesson-site"))
        self.assertIn("everything is in sync", self.share("sync"))

    def test_mv_refuses_a_skill_with_your_own_link(self):
        self.enable()
        before = self.snapshot()
        out = self.share("mv", "ask-simply", "ask-well", code=1)
        self.assertIn("~/.claude-lab/skills/ask-simply is a link you made to it", out)
        self.assertEqual(self.snapshot(), before)

    def test_mv_refuses_a_taken_name_or_an_item_out_of_sync(self):
        self.enable()
        before = self.snapshot()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.share("mv", "dana", "day-plan", code=2)
            self.share("mv", "dana", "Dana_2", code=2)
        self.assertIn("there's already an item named day-plan", err.getvalue())
        self.assertIn("names must be lowercase", err.getvalue())
        self.write_kiro("dana", prompt="Edited in Kiro.")
        self.assertIn("haven't reached every copy", self.share("mv", "dana", "reviewer", code=1))
        self.assertEqual({k: v for k, v in self.snapshot().items() if "dana.json" not in k},
                         {k: v for k, v in before.items() if "dana.json" not in k})

    def test_a_failed_rename_puts_back_the_ones_done(self):
        self.enable()
        before = self.folders()
        real = apply.fsutil.write_preserving
        calls = []

        def fail_second(path, data, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            return real(path, data, **kwargs)

        with patch.object(apply.fsutil, "write_preserving", fail_second):
            out = self.share("mv", "day-plan", "wrap-up", code=1)
        self.assertIn("No space left on device", out)
        self.assertIn("put back: the rename is all or nothing", out)
        self.assertEqual({k: v for k, v in self.folders().items()}, before)
        self.assertIsNotNone(self.record("agents/day-plan"))
        self.assertIsNone(self.record("agents/wrap-up"))


class RenameDetectionTests(ItemBase):
    def rename_file(self, folder, old, new):
        os.rename(folder / "agents" / f"{old}.md", folder / "agents" / f"{new}.md")
        age(folder / "agents" / f"{new}.md")

    def test_a_file_you_renamed_is_renamed_everywhere(self):
        self.enable()
        self.share("pick", "dana", "--not-in", "codex")
        self.rename_file(self.claude, "dana", "reviewer")  # its name field still says dana
        before = self.snapshot()
        out = self.share("sync", "--dry-run")
        self.assertIn("Renames (agt renames every copy", out)
        self.assertIn("dana -> reviewer (agents) in claude, claude-lab, kiro", out)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("dana is reviewer now, in every CLI.", self.share("sync"))
        self.assertIn("name: reviewer", (self.claude / "agents" / "reviewer.md").read_text(encoding="utf-8"))
        self.assertIn("name: reviewer", (self.lab / "agents" / "reviewer.md").read_text(encoding="utf-8"))
        self.assertEqual(self.kiro("reviewer")["name"], "reviewer")
        for path in (self.lab / "agents" / "dana.md", self.user / ".kiro" / "agents" / "dana.json"):
            self.assertFalse(path.exists(), path)
        self.assertFalse((self.user / ".codex" / "agents" / "reviewer.toml").exists())  # the pick came along
        self.assertEqual(settings.load().not_in, {"agents/reviewer": ["codex"]})
        self.assertIsNone(self.record("agents/dana"))
        self.assertIn("everything is in sync", self.share("sync"))

    def test_a_skill_folder_you_renamed_gets_agts_links_again(self):
        self.enable()
        old, new = self.lab / "skills" / "interactive-lesson-site", self.lab / "skills" / "lesson-site"
        os.rename(old, new)
        manifest = new / "SKILL.md"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace("name: interactive-lesson-site", "name: lesson-site"),
                            encoding="utf-8")
        age(new, manifest)
        self.share("sync")
        for link in (self.claude / "skills" / "lesson-site", self.user / ".kiro" / "skills" / "lesson-site"):
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(link.resolve(), new.resolve())
        for link in (self.claude / "skills" / "interactive-lesson-site", self.user / ".kiro" / "skills" / "interactive-lesson-site"):
            self.assertFalse(os.path.lexists(link), link)
        self.assertEqual(sorted(self.record("skills/lesson-site")["made"]), ["claude", "kiro"])
        self.assertIn("everything is in sync", self.share("sync"))

    def test_an_item_command_waits_for_sync_to_finish_a_rename(self):
        self.enable()
        self.share("pick", "dana", "--not-in", "codex")
        self.rename_file(self.claude, "dana", "reviewer")
        before = self.folders()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.share("pick", "reviewer", "--not-in", "kiro", code=2)
        self.assertIn("`agt share sync` finishes the rename", err.getvalue())
        self.assertEqual(self.folders(), before)
        self.share("sync")
        self.share("pick", "reviewer", "--not-in", "kiro")
        self.assertEqual(settings.load().not_in, {"agents/reviewer": ["codex", "kiro"]})
        self.assertFalse((self.user / ".codex" / "agents" / "reviewer.toml").exists())

    def test_an_edited_copy_left_under_the_old_name_stops_it(self):
        self.enable()
        self.write_kiro("dana", prompt="Dana, edited in Kiro.")
        self.rename_file(self.claude, "dana", "reviewer")
        out = self.share("sync")
        self.assertNotIn("Renames", out)
        self.assertTrue((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertFalse((self.lab / "agents" / "reviewer.md").exists())

    def test_two_matching_items_stop_it(self):
        self.enable()
        self.rename_file(self.claude, "dana", "reviewer")
        (self.claude / "agents" / "critic.md").write_text(
            (self.claude / "agents" / "reviewer.md").read_text(encoding="utf-8"), encoding="utf-8")
        age(self.claude / "agents" / "critic.md")
        out = self.share("sync")
        self.assertIn("more than one item matches it", out)
        self.assertTrue((self.lab / "agents" / "dana.md").exists())

    def test_a_fresh_file_waits(self):
        self.enable()
        os.rename(self.claude / "agents" / "dana.md", self.claude / "agents" / "reviewer.md")
        os.utime(self.claude / "agents" / "reviewer.md")  # saved just now
        self.assertNotIn("Renames", self.share("sync", "--dry-run"))
        age(self.claude / "agents" / "reviewer.md")
        self.assertIn("Renames", self.share("sync", "--dry-run"))

    def test_a_frozen_folder_stops_it(self):
        for index in range(5):
            self.skill(self.lab, f"extra-{index}")
        age(*self.lab.rglob("*"))
        self.enable()
        for index in range(4):
            (self.user / ".kiro" / "skills" / f"extra-{index}").unlink()
        self.rename_file(self.claude, "dana", "reviewer")
        kiro = self.user / ".kiro" / "agents"
        os.rename(kiro / "dana.json", kiro / "reviewer.json")
        age(kiro / "reviewer.json")
        self.assertIn("Renames", self.share("sync", "--dry-run"))  # the agents folder isn't frozen
        os.rename(self.user / ".kiro" / "skills" / "ask-simply", self.user / ".kiro" / "skills" / "ask-better")
        self.assertNotIn("ask-simply -> ask-better", self.share("sync", "--dry-run"))

    def test_a_mv_cut_off_is_finished_by_the_next_sync(self):
        self.enable()
        real = apply._rename
        calls = []

        def rename_then_stop(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        with patch.object(apply, "_rename", rename_then_stop):
            self.assertEqual(self.run_share("mv", "day-plan", "wrap-up")[0], 130)
        self.share("sync")
        for folder in (self.claude, self.lab):
            self.assertFalse((folder / "agents" / "day-plan.md").exists(), folder)
            self.assertIn("name: wrap-up", (folder / "agents" / "wrap-up.md").read_text(encoding="utf-8"))
        self.assertEqual(self.kiro("wrap-up")["name"], "wrap-up")
        self.assertIsNone(self.record("agents/day-plan"))
        self.assertIn("everything is in sync", self.share("sync"))
