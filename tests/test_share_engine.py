"""agt share's 3-way decision: one test per row of the decision table in share/engine.py."""

import unittest

import helpers  # noqa: F401  (isolates the test process from your real folders)

from agents_router.share.engine import (
    CONFLICT, NOTHING, PENDING_DELETE, STOP, WAIT, WRITE, Copy, Last, decide, frozen,
)

TARGETS = {"claude", "claude-lab", "codex", "kiro"}


def copies(**versions):
    return [Copy(label.replace("_", "-"), version) for label, version in versions.items()]


def synced(version="v1", made=("claude-lab", "codex", "kiro"), **extra):
    labels = {"claude": version, "claude-lab": version, "codex": version, "kiro": version}
    return Last(version=version, copies=labels, made=set(made), **extra)


class FirstSightTests(unittest.TestCase):
    def test_agreeing_copies_fill_the_missing_targets(self):
        decision = decide(copies(claude="v1", claude_lab="v1"), TARGETS)
        self.assertEqual((decision.action, decision.version, decision.fills, decision.updates), (WRITE, "v1", ["codex", "kiro"], []))
        self.assertIn(decision.source, ("claude", "claude-lab"))

    def test_differing_copies_are_a_conflict_and_write_nothing(self):
        decision = decide(copies(claude="v1", claude_lab="v2"), TARGETS)
        self.assertEqual((decision.action, decision.labels, decision.writes), (CONFLICT, ["claude", "claude-lab"], []))
        self.assertIn("first time", decision.reason)

    def test_present_everywhere_is_nothing(self):
        decision = decide(copies(claude="v1", claude_lab="v1", codex="v1", kiro="v1"), TARGETS)
        self.assertEqual((decision.action, decision.writes), (NOTHING, []))
        self.assertEqual(decide([], TARGETS).action, NOTHING)


class KnownItemTests(unittest.TestCase):
    def test_no_change_fills_only_what_is_missing(self):
        decision = decide(copies(claude="v1", claude_lab="v1", codex="v1", kiro="v1"), TARGETS | {"antigravity"}, synced())
        self.assertEqual((decision.action, decision.fills, decision.updates), (WRITE, ["antigravity"], []))
        self.assertEqual(decide(copies(claude="v1", claude_lab="v1", codex="v1", kiro="v1"), TARGETS, synced()).action, NOTHING)

    def test_one_edit_reaches_every_other_copy(self):
        decision = decide(copies(claude="v1", claude_lab="v1", codex="v1", kiro="v2"), TARGETS, synced())
        self.assertEqual((decision.action, decision.version, decision.source), (WRITE, "v2", "kiro"))
        self.assertEqual(decision.updates, ["claude", "claude-lab", "codex"])

    def test_two_edits_that_agree_are_written_once_everywhere_else(self):
        decision = decide(copies(claude="v2", claude_lab="v2", codex="v1", kiro="v1"), TARGETS, synced())
        self.assertEqual((decision.action, decision.version, decision.updates), (WRITE, "v2", ["codex", "kiro"]))

    def test_edits_that_disagree_are_a_conflict(self):
        decision = decide(copies(claude="v2", claude_lab="v3", codex="v1", kiro="v1"), TARGETS, synced())
        self.assertEqual((decision.action, decision.labels, decision.writes), (CONFLICT, ["claude", "claude-lab"], []))

    def test_deleted_in_one_place_and_edited_in_another_keeps_the_edit(self):
        decision = decide(copies(claude="v1", claude_lab="v1", kiro="v2"), TARGETS, synced())
        self.assertEqual((decision.action, decision.labels, decision.writes), (CONFLICT, ["codex", "kiro"], []))
        self.assertIn("deleted", decision.reason)

    def test_a_deleted_agt_copy_stops_sharing_to_that_cli(self):
        decision = decide(copies(claude="v1", claude_lab="v1", kiro="v1"), TARGETS, synced())
        self.assertEqual((decision.action, decision.stopped, decision.writes), (STOP, ["codex"], []))
        # Next run: the item stays out of Codex.
        last = synced(stopped={"codex"})
        del last.copies["codex"]
        self.assertEqual(decide(copies(claude="v1", claude_lab="v1", kiro="v1"), TARGETS, last).action, NOTHING)

    def test_a_deleted_original_waits_for_share_rm(self):
        decision = decide(copies(claude_lab="v1", codex="v1", kiro="v1"), TARGETS, synced())
        self.assertEqual((decision.action, decision.labels, decision.writes), (PENDING_DELETE, ["claude"], []))
        self.assertIn("agt share rm", decision.reason)

    def test_an_absent_folder_is_never_filled_or_deleted(self):
        decision = decide(copies(claude="v1", claude_lab="v1", codex="v1"), TARGETS, synced(), absent={"kiro"})
        self.assertEqual((decision.action, decision.writes), (NOTHING, []))
        decision = decide(copies(claude="v2", claude_lab="v1", codex="v1"), TARGETS, synced(), absent={"kiro"})
        self.assertEqual((decision.action, decision.updates), (WRITE, ["claude-lab", "codex"]))
        self.assertEqual(decide(copies(claude="v1"), TARGETS, None, absent={"codex", "kiro"}).fills, ["claude-lab"])

    def test_a_new_copy_that_matches_is_no_edit(self):
        last = synced()
        del last.copies["kiro"]
        decision = decide(copies(claude="v1", claude_lab="v1", codex="v1", kiro="v1"), TARGETS, last)
        self.assertEqual(decision.action, NOTHING)
        decision = decide(copies(claude="v1", claude_lab="v1", codex="v1", kiro="v9"), TARGETS, last)
        self.assertEqual((decision.action, decision.version, decision.source), (WRITE, "v9", "kiro"))

    def test_released_copies_are_not_part_of_the_item(self):
        last = synced(released={"kiro"})
        del last.copies["kiro"]
        decision = decide(copies(claude="v1", claude_lab="v1", codex="v1", kiro="mine"), TARGETS, last)
        self.assertEqual((decision.action, decision.writes), (NOTHING, []))


class MassDeleteTests(unittest.TestCase):
    def test_more_than_max_3_or_a_quarter_disappearing_freezes_the_folder(self):
        self.assertEqual(frozen({"kiro": 10, "codex": 40}, {"kiro": 3, "codex": 10}), set())
        self.assertEqual(frozen({"kiro": 10, "codex": 40}, {"kiro": 4, "codex": 11}), {"kiro", "codex"})
        self.assertEqual(frozen({"claude": 2}, {"claude": 2}), set())


class WaitTests(unittest.TestCase):
    def test_an_unparseable_copy_makes_the_item_wait(self):
        broken = [Copy("claude", "v1"), Copy("kiro", None, problem="kiro: agents/x.json line 3: Expecting ','")]
        decision = decide(broken, TARGETS, synced())
        self.assertEqual((decision.action, decision.labels, decision.writes), (WAIT, ["kiro"], []))
        self.assertIn("line 3", decision.reason)

    def test_a_fresh_file_makes_the_whole_item_wait(self):
        fresh = [Copy("claude", "v2", fresh=True), Copy("claude-lab", "v1"), Copy("codex", "v1"), Copy("kiro", "v1")]
        decision = decide(fresh, TARGETS, synced())
        self.assertEqual((decision.action, decision.labels, decision.writes), (WAIT, ["claude"], []))


if __name__ == "__main__":
    unittest.main()
