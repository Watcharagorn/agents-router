"""agt share's attention list and event log, and the folder freeze that waits for your answer."""

import json
import os
from unittest.mock import patch

from test_share_items import ItemBase
from test_share_plan import FLAGS
from test_share_sync import age

from agents_router import fsutil
from agents_router.share import attention


class AttentionTests(ItemBase):
    def entries(self):
        return {entry["id"]: entry for entry in attention.load()}

    def test_a_conflict_is_listed_with_its_fix_and_keeps_its_first_seen_time(self):
        self.enable(flags=["enable", *FLAGS, "--yes", "--accept-suggested"])
        entry = self.entries()["conflict:skills/gamma-apikey"]
        self.assertEqual((entry["who"], entry["fix"]), ("choice", ["agt share resolve gamma-apikey"]))
        since = entry["since"]
        with patch.object(attention.time, "strftime", lambda fmt, *args: "2099-01-01T00:00:00+0000"):
            self.share("sync")
        self.assertEqual(self.entries()["conflict:skills/gamma-apikey"]["since"], since)
        code, out = self.run_share("status")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"\n  ! skill gamma-apikey, in [a-z, -]+\n    The copies were edited differently\.\n"
                              r"    Pick a version +agt share resolve gamma-apikey\n")
        self.share("resolve", "gamma-apikey", "--prefer", "claude-lab")
        self.assertNotIn("conflict:skills/gamma-apikey", self.entries())

    def test_a_command_for_one_item_leaves_the_others_entries_alone(self):
        self.enable(flags=["enable", *FLAGS, "--yes", "--accept-suggested"])
        self.share("pick", "dana", "--not-in", "kiro")
        self.assertIn("conflict:skills/gamma-apikey", self.entries())

    def test_dry_runs_and_status_never_write_it(self):
        self.enable()
        attention.path().unlink(missing_ok=True)
        self.agent(self.claude, "day-plan", body="One way.", tools="Bash")
        self.agent(self.lab, "day-plan", body="Another way.", tools="Bash")
        age(self.claude / "agents" / "day-plan.md", self.lab / "agents" / "day-plan.md")
        self.share("sync", "--dry-run")
        code, out = self.run_share("status", "--json")
        (entry,) = json.loads(out)["attention"]
        self.assertEqual(entry["id"], "conflict:agents/day-plan")
        self.assertTrue(entry["message"].startswith("day-plan differs between claude, claude-lab ("), entry["message"])
        self.assertFalse(attention.path().exists())

    def test_a_new_item_waiting_for_targets_and_an_unreadable_copy(self):
        self.enable("--auto-copy", "off")
        self.agent(self.claude, "reviewer", body="Review.")
        kiro = self.user / ".kiro" / "agents" / "explorer.json"
        kiro.write_text("{not json", encoding="utf-8")
        age(self.claude / "agents" / "reviewer.md", kiro)
        self.share("sync")
        entries = self.entries()
        self.assertEqual(entries["new:agents/reviewer"]["fix"], ["agt share pick reviewer --in <label>"])
        self.assertEqual(entries["unreadable:agents/explorer"]["who"], "action")
        self.assertIn("explorer.json", entries["unreadable:agents/explorer"]["message"])

    def test_a_write_failing_three_runs_in_a_row_is_listed_and_clears_after_a_success(self):
        self.enable()
        self.agent(self.claude, "dana", body="Dana, edited.", tools="Bash, Read")
        age(self.claude / "agents" / "dana.md")
        real = fsutil.write_preserving

        def full(path, data, **kwargs):
            if path.name == "dana.json":
                raise OSError(28, "No space left on device")
            return real(path, data, **kwargs)

        with patch.object(fsutil, "write_preserving", full):
            for _ in range(2):
                self.share("sync", code=1)
            self.assertNotIn("failing:agents/dana", self.entries())
            self.share("sync", code=1)
        entry = self.entries()["failing:agents/dana"]
        self.assertEqual(entry["who"], "action")
        self.assertIn("No space left on device", entry["message"])
        lines = [json.loads(line) for line in attention.events_path().read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["kind"] for line in lines if line["item"] == "agents/dana"], ["write-failed"])  # once a day
        self.share("sync")
        self.assertNotIn("failing:agents/dana", self.entries())
        self.assertNotIn("failing", self.record("agents/dana"))


class FreezeTests(ItemBase):
    def setUp(self):
        super().setUp()
        for index in range(5):
            self.skill(self.lab, f"extra-{index}")
        age(*self.lab.rglob("*"))
        self.enable()
        self.kiro_skills = self.user / ".kiro" / "skills"
        for index in range(4):
            (self.kiro_skills / f"extra-{index}").unlink()

    def test_the_freeze_lasts_until_you_answer(self):
        for _ in range(2):
            self.share("sync")
            self.assertFalse(os.path.lexists(self.kiro_skills / "extra-0"))
            self.assertNotIn("stopped", self.record("skills/extra-0"))
        entry = self.entries()["frozen:folder:kiro"]
        self.assertEqual(entry["fix"], ["agt share sync --allow-deletes kiro", "agt share sync --refill kiro"])
        code, out = self.run_share("status")
        self.assertEqual(code, 1)
        self.assertIn("  ! skills in kiro\n    4 of 7 skills disappeared from kiro at once", out)
        self.assertRegex(out, r"\n    Keep them out +agt share sync --allow-deletes kiro\n    Put them back +agt share sync --refill kiro\n")

    def test_refill_puts_the_copies_back(self):
        self.share("sync")
        self.share("sync", "--refill", "kiro")
        for index in range(4):
            self.assertTrue((self.kiro_skills / f"extra-{index}").is_symlink(), index)
        self.assertNotIn("frozen:folder:kiro", self.entries())
        self.assertIn("everything is in sync", self.share("sync"))

    def test_allow_deletes_lets_them_go(self):
        self.share("sync", "--allow-deletes", "kiro")
        self.assertEqual(self.record("skills/extra-0")["stopped"], ["kiro"])
        self.assertNotIn("frozen:folder:kiro", self.entries())
        self.assertIn("everything is in sync", self.share("sync"))
        self.assertFalse(os.path.lexists(self.kiro_skills / "extra-0"))

    entries = AttentionTests.entries


class OriginalsDontFreezeTests(ItemBase):
    def test_deleting_many_originals_waits_for_rm_without_a_freeze(self):
        for index in range(5):
            self.agent(self.claude, f"helper-{index}", body=f"Help {index}.")
        age(*(self.claude / "agents").iterdir())
        self.enable()
        for index in range(4):
            (self.claude / "agents" / f"helper-{index}.md").unlink()
        before = self.folders()
        self.share("sync")
        self.assertEqual(self.folders(), before)
        code, out = self.run_share("status")
        self.assertEqual(code, 1)
        self.assertNotIn("disappeared from", out)
        self.assertEqual(out.count("Remove its copies   agt share rm helper-"), 4)

