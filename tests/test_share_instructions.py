"""Sharing instructions: agt's block in CLAUDE.md, AGENTS.md, GEMINI.md and Kiro's steering, kept the same."""

import contextlib
import io
import json
import os
import time
import unittest
from unittest.mock import patch

from test_share_items import ItemBase
from test_share_sync import age

from agents_router import cli, rules
from agents_router.share import commands, instructions, settings

START, END = instructions.START, instructions.END


def block(text):
    return f"{START}\n{text}\n{END}\n"


class TextTests(unittest.TestCase):
    def test_the_shared_text_leaves_out_imports_the_rules_block_and_edge_blank_lines(self):
        raw = f"\n@~/notes.md\nUse tabs.  \r\n\n{rules.START}\nrules\n{rules.END}\nShort answers.\n\n"
        self.assertEqual(instructions.normalize(raw), "Use tabs.\n\nShort answers.")


class InstructionTests(ItemBase):
    def setUp(self):
        super().setUp()
        self.codex = self.user / ".codex"
        self.kiro_dir = self.user / ".kiro"

    def files(self):
        return {"claude": self.claude / "CLAUDE.md", "claude-lab": self.lab / "CLAUDE.md", "codex": self.codex / "AGENTS.md",
                "kiro": self.kiro_dir / "steering" / "AGENTS.md"}

    def write(self, label, text):
        path = self.files()[label]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        age(path)
        return path

    def read(self, label):
        return self.files()[label].read_text(encoding="utf-8")

    def test_off_by_default_and_nothing_reads_the_files(self):
        self.write("claude", block("Mine."))
        self.enable()
        self.assertFalse(self.files()["codex"].exists())
        self.assertEqual(settings.load().instructions, "off")

    def test_turning_them_on_gives_the_command_for_files_without_the_section(self):
        self.write("claude", block("Mine."))
        self.write("codex", "Codex only.\n")
        self.enable()
        out = self.share("kinds", "add", "instructions")
        self.assertTrue(out.startswith("agt share kinds add instructions  ✓ instructions share now\n"), out)
        self.assertIn("\n  agt keeps a shared section in each CLI's instruction file the same everywhere.\n", out)
        self.assertRegex(out, r"\n  Add the section   agt share adopt [a-z -]*codex")
        self.assertEqual(self.share("kinds", "add", "instructions"), "agt share kinds add instructions  already on (block)\n")

    def test_empty_sections_say_where_to_write(self):
        self.write("codex", "Codex only.\n")
        self.enable("--instructions", "block")
        out = self.share("adopt", "codex", "claude")
        self.assertTrue(out.startswith("agt share adopt  ✓ added a shared section to 2 instruction files\n\n"
                                       "  ~ claude   ~/.claude/CLAUDE.md\n  ~ codex    ~/.codex/AGENTS.md\n\n"
                                       "  The sections are empty: write your shared instructions in any one of them, "
                                       "and the others get them.\n"), out)

    def test_adopted_blocks_share_and_the_rest_of_each_file_stays(self):
        self.write("claude", "# My Claude notes\n@~/extra.md\n\n" + block("Answer briefly.\nUse tabs."))
        self.write("codex", "Codex only.\n")
        self.enable("--instructions", "block")
        status = self.share("status")
        self.assertIn("Codex's AGENTS.md has no shared section yet", status)
        out = self.share("adopt", "codex", "kiro", "claude-lab")
        self.assertIn("  ~ codex        ~/.codex/AGENTS.md\n", out)
        self.assertIn("Each one now holds your shared instructions", out)  # adopting syncs at once
        self.assertEqual(self.read("codex"), "Codex only.\n\n" + block("Answer briefly.\nUse tabs."))
        self.assertEqual(self.read("kiro"), block("Answer briefly.\nUse tabs."))
        self.assertNotIn("@~/extra.md", self.read("claude-lab"))
        # An edit in Codex spreads; Claude keeps its own import and heading.
        self.write("codex", "Codex only.\n\n" + block("Answer briefly.\nUse spaces."))
        self.share("sync")
        claude = self.read("claude")
        self.assertTrue(claude.startswith("# My Claude notes\n@~/extra.md\n"))
        self.assertIn(f"{START}\n@~/extra.md\n\nAnswer briefly.\nUse spaces.\n{END}", claude.replace("# My Claude notes\n", ""))
        self.assertIn("Use spaces.", self.read("kiro"))
        self.assertIn("everything is in sync", self.share("sync"))
        backups = list((self.agt_home / "state" / "share" / "backups").rglob("instructions/claude-CLAUDE.md"))
        self.assertEqual(len(backups), 1)

    def test_different_edits_are_a_conflict_until_you_pick(self):
        self.write("claude", block("One."))
        self.write("codex", block("One."))
        self.enable("--instructions", "block")
        self.write("claude", block("Two."))
        self.write("codex", block("Three."))
        out = self.share("status", code=1)
        self.assertIn("  Instructions   differ between claude and codex\n", out)
        self.assertRegex(out, r"\n  ! instructions, in claude and codex\n    The copies were edited differently\.\n"
                              r"    Pick a version +agt share resolve instructions\n")
        self.share("sync")
        self.assertIn("Three.", self.read("codex"))
        answers = iter(["cluade", "claude"])
        with patch.object(commands, "_interactive", lambda: True), \
                patch("builtins.input", lambda prompt: next(answers)):  # a typo asks again
            self.share("resolve", "instructions")
        self.assertIn("Two.", self.read("codex"))
        self.write("codex", block("Three."))
        out = self.share("resolve", "instructions", "--prefer", "claude")
        self.assertIn("Two.", self.read("codex"))
        self.assertIn("everything is in sync", self.share("sync"))

    def test_wrap_puts_your_text_in_the_block_and_keeps_the_rules_outside(self):
        self.write("claude", f"Be kind.\n\n{rules.START}\nrules\n{rules.END}\n")
        self.enable("--instructions", "block")
        self.share("adopt", "claude", "--wrap")
        text = self.read("claude")
        self.assertTrue(text.startswith(block("Be kind.")))
        self.assertIn(rules.START, text.split(END)[1])

    def test_whole_mode_shares_the_whole_file_and_makes_missing_ones(self):
        self.write("claude", "All of it.\n")
        self.enable("--instructions", "whole")
        self.assertEqual(self.read("codex"), "All of it.\n")
        self.assertEqual(self.read("kiro"), "All of it.\n")

    def test_kinds_add_and_remove_instructions(self):
        self.write("claude", block("Shared."))
        self.enable()
        self.share("kinds", "add", "instructions")
        self.assertEqual(settings.load().instructions, "block")
        self.share("kinds", "remove", "instructions")
        self.assertEqual(settings.load().instructions, "off")
        self.assertIn("Shared.", self.read("claude"))  # nothing is removed

    def test_broken_markers_wait_and_say_where(self):
        self.write("claude", f"{START}\nOne.\n")
        self.write("codex", block("One."))
        self.enable("--instructions", "block")
        out = self.share("status")
        self.assertIn("markers are incomplete or repeated", out)

    def test_dry_run_shows_the_instructions_and_writes_nothing(self):
        self.write("claude", block("Shared."))
        self.write("codex", block(""))
        before = self.read("codex")
        out = self.share("enable", "--instructions", "block", "--dry-run")
        self.assertIn("\n  Instructions   1 to write (codex), from claude\n", out)
        self.assertEqual(self.read("codex"), before)
        proposal = json.loads(self.share("enable", "--instructions", "block", "--dry-run", "--json"))
        self.assertEqual(proposal["instructions"]["writes"], ["codex"])

    def test_a_dry_run_counts_what_will_wait_once_applied(self):
        self.write("claude", block("Shared."))
        self.write("codex", "Codex only.\n")
        out = self.share("enable", "--instructions", "block", "--dry-run")
        # Codex's file has no shared section; the conflict listed above isn't counted again.
        self.assertTrue(out.endswith("\n  1 more thing will wait for you once you apply this plan: "
                                     "`agt share status` lists it then.\n"), out[-300:])

    def test_the_watcher_sees_an_instructions_edit(self):
        from agents_router.share import watch

        self.write("claude", block("One."))
        self.write("codex", block("One."))
        self.enable("--instructions", "block")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            watch.run_once()
            watch.run_once()
        self.write("claude", block("Two."))
        self.assertNotEqual(watch.background_sync("watcher")[0], "nothing moved")
        self.assertIn("Two.", self.read("codex"))


if __name__ == "__main__":
    unittest.main()
