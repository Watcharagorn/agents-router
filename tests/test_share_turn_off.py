"""Turning sharing off: cli remove, kinds remove and disable, with keep or remove, and turning it on again."""

import builtins
import contextlib
import io
import json
import os
from unittest.mock import patch

from test_share_items import ItemBase
from test_share_plan import FLAGS
from test_share_sync import age

from agents_router.share import apply, commands, settings


class TurnOffBase(ItemBase):
    def labels(self):
        return [folder.label for folder in settings.load().folders]

    def everywhere(self, key):
        record = self.record(key) or {}
        return {label for field in ("copies", "made", "stopped") for label in (record.get(field) or [])}


class CliRemoveTests(TurnOffBase):
    def test_remove_copies_takes_codexs_agents_and_leaves_the_skills_it_reads_through_lab(self):
        self.enable()
        codex = self.user / ".codex" / "agents"
        out = self.share("cli", "remove", "codex", "--remove-copies")
        self.assertIn("codex doesn't share any more", out)
        self.assertIn("interactive-lesson-site in codex: codex reads claude-lab's folder, so it stays", out)
        self.assertEqual(list(codex.iterdir()), [])
        self.assertEqual(len(self.backups("dana.toml")), 1)
        self.assertTrue((self.lab / "skills" / "interactive-lesson-site" / "SKILL.md").is_file())
        self.assertNotIn("codex", self.labels())
        for key in ("agents/dana", "agents/day-plan", "skills/ask-simply", "skills/interactive-lesson-site"):
            self.assertNotIn("codex", self.everywhere(key), key)
        self.assertIn("everything is in sync", self.share("sync"))
        code, out = self.run_share("status")
        self.assertEqual(code, 0, out)
        # Back again: filled at once.
        self.share("cli", "add", "codex")
        self.assertTrue((codex / "dana.toml").is_file())
        self.assertIn("everything is in sync", self.share("sync"))

    def test_your_originals_stay_and_renames_are_still_spotted(self):
        self.enable()
        out = self.share("cli", "remove", "claude", "--remove-copies")
        for name in ("dana", "day-plan", "explorer"):
            self.assertTrue((self.claude / "agents" / f"{name}.md").is_file(), name)
        self.assertTrue((self.claude / "skills" / "ask-simply" / "SKILL.md").is_file())
        self.assertFalse(os.path.lexists(self.claude / "skills" / "interactive-lesson-site"))  # agt's link
        self.assertIn("dana in claude: your original stays", out)
        self.assertNotIn("claude", self.everywhere("agents/dana"))
        os.rename(self.lab / "agents" / "dana.md", self.lab / "agents" / "reviewer.md")
        age(self.lab / "agents" / "reviewer.md")
        self.assertIn("dana is reviewer now", self.share("sync"))
        self.assertEqual(self.kiro("reviewer")["name"], "reviewer")

    def test_keep_copies_leaves_them_as_your_own_files(self):
        self.enable()
        out = self.share("cli", "remove", "kiro", "--keep-copies")
        self.assertIn("shares it again, and the copies you kept stay your own files.", out)
        self.assertTrue((self.user / ".kiro" / "agents" / "dana.json").is_file())
        self.assertEqual(self.record("agents/dana")["released"], ["kiro"])
        self.agent(self.claude, "dana", body="Dana, edited.", tools="Bash, Read")
        age(self.claude / "agents" / "dana.md")
        self.share("sync")
        self.assertEqual(self.kiro("dana")["prompt"], "Dana's long instructions.")
        # Back again: your kept files aren't taken for agt's copies, and the one you deleted comes back.
        (self.user / ".kiro" / "agents" / "day-plan.json").unlink()
        out = self.share("cli", "add", "kiro")
        self.assertIn("dana in kiro is your own file (you kept it); `agt share pick dana --in kiro` takes it back", out)
        self.assertEqual(self.kiro("dana")["prompt"], "Dana's long instructions.")
        self.assertEqual(self.record("agents/dana")["released"], ["kiro"])
        self.assertTrue((self.user / ".kiro" / "agents" / "day-plan.json").is_file())
        self.assertNotIn("released", self.record("agents/day-plan"))

    def test_without_an_answer_it_asks_or_exits_3(self):
        self.enable()
        before = self.snapshot()
        answer = json.loads(self.share("cli", "remove", "kiro", "--json", code=3))
        self.assertEqual(answer["error"], "needs_choice")
        self.assertEqual(answer["retry"], "agt share cli remove kiro --keep-copies | --remove-copies")
        self.assertIn("dana", {copy["name"] for copy in answer["copies"] if copy["status"] == "agt"})
        with patch.dict(os.environ, {"CLAUDECODE": "1"}), patch("sys.stdin.isatty", lambda: True), \
                patch("sys.stdout.isatty", lambda: True):
            self.share("cli", "remove", "kiro", code=3)  # an agent, even with a terminal
        dry = json.loads(self.share("cli", "remove", "kiro", "--dry-run", "--json"))
        self.assertEqual(len(dry["needs_answer"]), 1)
        with patch.object(commands, "_interactive", lambda: True), patch.object(builtins, "input", lambda prompt: "q"):
            self.assertIn("Nothing was changed.", self.share("cli", "remove", "kiro"))
        self.assertEqual(self.snapshot(), before)
        with patch.object(commands, "_interactive", lambda: True), patch.object(builtins, "input", lambda prompt: "r"):
            self.share("cli", "remove", "kiro")
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())

    def test_a_failed_removal_changes_no_settings(self):
        self.enable()
        real = apply.shutil.move
        calls = []

        def fail_second(source, destination):
            calls.append(source)
            if len(calls) == 2:
                raise OSError(13, "Permission denied")
            return real(source, destination)

        with patch.object(apply.shutil, "move", fail_second):
            out = self.share("cli", "remove", "kiro", "--remove-copies", code=1)
        self.assertIn("kiro still shares", out)
        self.assertIn("kiro", self.labels())
        out = self.share("cli", "remove", "kiro", "--remove-copies")
        self.assertNotIn("kiro", self.labels())
        self.assertFalse(any((self.user / ".kiro" / "agents").glob("d*.json")))

    def test_an_item_only_in_that_cli_isnt_shared_any_more(self):
        self.enable()
        self.share("pick", "explorer", "--only", "kiro")
        self.share("cli", "remove", "kiro", "--keep-copies")
        self.assertIn("agents/explorer", settings.load().off)

    def test_cli_list_and_the_last_folder(self):
        self.enable()
        out = self.share("cli", "list")
        self.assertRegex(out, r"\[x\] kiro\s+~/\.kiro\s+sharing")
        err = io.StringIO()
        for label in ("codex", "kiro", "claude"):
            self.share("cli", "remove", label, "--remove-copies")
        with contextlib.redirect_stderr(err):
            self.share("cli", "remove", "claude-lab", "--remove-copies", code=2)
        self.assertIn("the last folder sharing; `agt share disable` turns sharing off", err.getvalue())

    def test_cli_add_with_auto_copy_off_waits_for_your_picks(self):
        self.enable("--auto-copy", "off")
        self.share("cli", "remove", "kiro", "--remove-copies")
        out = self.share("cli", "add", "kiro")
        self.assertIn("auto copy is off, so these don't go to kiro until you pick them", out)
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.share("pick", "dana", "--in", "kiro")
        self.assertTrue((self.user / ".kiro" / "agents" / "dana.json").exists())


class KindsTests(TurnOffBase):
    def test_remove_copies_then_add_fills_them_again(self):
        self.enable()
        out = self.share("kinds", "remove", "agents", "--remove-copies")
        self.assertIn("agents don't share any more", out)
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertTrue((self.claude / "agents" / "dana.md").exists())
        self.assertEqual(settings.load().kinds, ("skills",))
        self.assertIn("everything is in sync", self.share("sync"))
        self.share("kinds", "add", "agents")
        self.assertTrue((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertEqual(sorted(self.record("agents/dana")["made"]), ["claude-lab", "codex", "kiro"])
        self.share("pick", "dana", "--not-in", "kiro")  # still agt's copy: it's removed, not kept
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())

    def test_keep_copies_and_an_edit_there_never_spreads(self):
        self.enable()
        self.share("kinds", "remove", "agents", "--keep-copies")
        self.write_kiro("dana", prompt="Dana, my way.")
        self.share("kinds", "add", "agents")
        self.assertIn("Dana's long instructions.", (self.claude / "agents" / "dana.md").read_text(encoding="utf-8"))
        self.assertEqual(self.kiro("dana")["prompt"], "Dana, my way.")
        out = self.share("pick", "dana", "--in", "kiro")  # taken back: it differs, so it's for you to decide
        self.assertIn("dana (agents)", out)

    def test_later_kinds_and_the_last_kind(self):
        self.enable()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.share("kinds", "add", "hooks", code=2)
            self.share("kinds", "remove", "skills", "--keep-copies")
            self.share("kinds", "remove", "agents", "--keep-copies", code=2)
        self.assertIn("sharing hooks comes in a later phase", err.getvalue())
        self.assertIn("agents is the last kind sharing", err.getvalue())


class DisableTests(TurnOffBase):
    def test_disable_keeps_every_copy_and_enable_resumes(self):
        self.enable("--auto-copy", "off")
        before = self.folders()
        out = self.share("disable")
        self.assertIn("agt share is off. Every copy stays as it is", out)
        self.assertEqual(self.folders(), before)
        self.assertIn("agt share is off. `agt share enable` turns it on again", self.share("sync"))
        proposal = json.loads(self.share("enable", "--dry-run", "--json"))
        self.assertEqual([folder["label"] for folder in proposal["folders"] if folder["used"]],
                         ["claude-lab", "claude", "codex", "kiro"])
        self.assertEqual(proposal["changes"], [])
        self.share("enable", "--resolve", "gamma-apikey=skip", "--yes")
        self.assertEqual(settings.load().auto_copy, ())  # your saved answer, not the default

    def test_remove_copies_keeps_originals_and_edited_copies(self):
        self.enable()
        self.write_kiro("explorer", prompt="Explore my way.")
        out = self.share("disable", "--remove-copies")
        self.assertIn("agt share is off", out)
        self.assertIn("explorer in kiro: it has an edit the other copies don't have", out)
        self.assertEqual(self.kiro("explorer")["prompt"], "Explore my way.")
        self.assertFalse((self.user / ".kiro" / "agents" / "dana.json").exists())
        self.assertFalse((self.lab / "agents" / "dana.md").exists())
        self.assertTrue((self.claude / "agents" / "dana.md").exists())
        self.assertTrue((self.lab / "agents" / "day-plan.md").exists())  # your own copy there
        self.assertFalse(settings.load().enabled)
        self.assertIn("agt share is off", self.share("undo", "dana"))
        self.share("enable", *FLAGS, "--resolve", "gamma-apikey=skip", "--yes")
        self.assertIn("dana's last backup is from turning sharing off, which undo doesn't reverse", self.share("undo", "dana"))

    def test_remove_copies_puts_back_a_skill_folder_resolve_replaced(self):
        self.enable(flags=["enable", *FLAGS, "--yes", "--accept-suggested"])
        self.share("resolve", "gamma-apikey", "--prefer", "claude-lab")
        folder = self.claude / "skills" / "gamma-apikey"
        self.assertTrue(folder.is_symlink())
        self.share("disable", "--remove-copies")
        self.assertFalse(folder.is_symlink())
        self.assertIn("Old steps.", (folder / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("New steps.", (self.lab / "skills" / "gamma-apikey" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertFalse(os.path.lexists(self.user / ".kiro" / "skills" / "gamma-apikey"))
        # On again, the two versions are a conflict, never an edit.
        self.share("enable", *FLAGS, "--yes", "--accept-suggested")
        code, out = self.run_share("status")
        self.assertIn("Conflict: gamma-apikey", out)
