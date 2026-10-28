"""What agt share says after it writes, and the settings listings: one line per item, the next step at the end."""

import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_share_items import ItemBase
from test_share_plan import FLAGS

from agents_router import cli, config_cli, ui
from agents_router.share import apply, commands, triggers


def item_lines(outcomes, verbose=False):
    commands._verbose = verbose
    try:
        return [ui.plain(line) for line in commands._item_lines(apply.Result("run", outcomes))]
    finally:
        commands._verbose = False


def outcome(kind, name, label, action="copy", ok=True, message=""):
    return apply.Outcome(kind, name, [label], action, Path(f"/x/{label}/{kind}/{name}"), ok, message)


class ItemLineTests(unittest.TestCase):
    def test_one_line_per_item_and_change(self):
        lines = item_lines([outcome("agents", "dana", "codex"), outcome("agents", "dana", "kiro"),
                            outcome("agents", "dana", "claude", action="update")])
        self.assertEqual(lines, ["  + agents/dana   codex, kiro", "  ~ agents/dana   claude"])

    def test_v_adds_each_copy_s_file(self):
        lines = item_lines([outcome("agents", "dana", "codex"), outcome("agents", "dana", "kiro")], verbose=True)
        self.assertEqual(lines, ["  + agents/dana   codex, kiro", "      codex  /x/codex/agents/dana", "      kiro   /x/kiro/agents/dana"])

    def test_a_failed_write_is_red_with_its_error_and_when_agt_tries_again(self):
        lines = item_lines([outcome("agents", "dana", "codex"), outcome("agents", "rover", "kiro", ok=False, message="disk full"),
                            outcome("plugins", "deck@tools", "codex", ok=False, message="`codex plugin add` failed (exit 1)")])
        self.assertEqual(lines, ["  + agents/dana          codex", "  ✗ agents/rover         kiro", "      disk full",
                                 "      The next sync tries again.", "  ✗ plugins/deck@tools   codex",
                                 "      `codex plugin add` failed (exit 1)",
                                 "      agt tries again when its version changes; `agt share pick plugins/deck@tools --in codex` tries now."])


class ReportTests(ItemBase):
    def test_enable_lists_items_then_what_runs_on_its_own_and_the_next_step(self):
        out = self.enable()
        self.assertTrue(out.startswith("agt share enable  ✓ wrote 9 changes in 4 folders\n\n"
                                       "  + agents/dana                      claude-lab, codex, kiro\n"), out)
        self.assertIn("\nSyncs on its own\n  The background watcher checks every minute", out)
        self.assertIn("\n  In claude-lab and claude, agt also catches up at each session start and each save in Claude Code.\n", out)
        self.assertTrue(out.endswith("\nNext: start a new session in a CLI to use what it got.\n"
                                     "Saved your answers in ~/.agt/settings.toml.\n"), out)
        self.assertNotIn("Syncs on its own", self.share("kinds", "add", "mcp"))  # nothing new runs on its own

    def test_a_watcher_that_didn_t_start_shows_every_time(self):
        self.enable()
        failed = (False, "the background watcher didn't start: launchctl bootstrap failed (no)")
        with patch.object(triggers, "install_watcher", return_value=failed):
            out = self.share("kinds", "add", "mcp")
        self.assertIn("\nSyncs on its own\n  The background watcher didn't start: launchctl bootstrap failed (no); "
                      "`agt doctor --fix` tries again.\n", out)

    def test_a_pick_that_writes_nothing_says_it_saved(self):
        self.enable()
        out = self.share("pick", "dana", "--not-in", "kiro", "--keep-copies")
        self.assertTrue(out.startswith("agt share pick  ✓ saved, nothing to write\n"), out)
        self.assertIn("\nNotes\n  dana stays in kiro as your own file; agt no longer updates it.\n", out)

    def test_a_removal_gives_the_way_back_and_the_backup(self):
        self.enable()
        out = self.share("pick", "dana", "--not-in", "kiro")
        self.assertTrue(out.startswith("agt share pick  ✓ removed 1 copy\n\n  - agents/dana   kiro\n\n"
                                       "  Bring it back   agt share undo dana --in kiro\n  Backup          "), out)
        self.assertIn("Next", self.share("pick", "dana", "--in", "kiro", "-v"))

    def test_v_lists_the_files(self):
        self.enable()
        self.share("pick", "dana", "--not-in", "kiro")
        out = self.share("pick", "dana", "--in", "kiro", "-v")
        self.assertIn("  + agents/dana   kiro\n      kiro  ~/.kiro/agents/dana.json\n", out)
        self.assertNotIn("dana.json", self.share("pick", "dana", "--not-in", "kiro"))

    def test_a_conflict_on_enable_asks_with_one_flag_per_answer(self):
        code, out = self.run_share("enable", *FLAGS, "--yes")
        self.assertEqual(code, 3)
        self.assertEqual(out, "agt share enable  ! nothing written: 1 item needs your choice\n\n"
                              "  skills/gamma-apikey differs between claude-lab (newer) and claude.\n"
                              "  Which version should every CLI get? Add one of these to the command:\n"
                              "    claude-lab   --resolve gamma-apikey=claude-lab\n"
                              "    claude       --resolve gamma-apikey=claude\n"
                              "    neither      --resolve gamma-apikey=skip   every copy stays as it is\n")

    def test_turning_a_kind_off_lists_each_copy_with_its_folders(self):
        self.enable()
        code, out = self.run_share("kinds", "remove", "skills")
        self.assertEqual(code, 3)
        self.assertEqual(out, "agt share kinds remove skills  ! nothing changed: keep or remove 3 copies?\n\n"
                              "Turning off skills leaves the 3 copies agt made:\n"
                              "  ask-simply                kiro\n"
                              "  interactive-lesson-site   claude, kiro\n\n"
                              "Your originals stay either way:\n"
                              "  ask-simply                claude-lab, codex, claude\n"
                              "  interactive-lesson-site   claude-lab, codex\n\n"
                              "  Keep them as your own files   agt share kinds remove skills --keep-copies\n"
                              "  Remove them, with a backup    agt share kinds remove skills --remove-copies\n")
        out = self.share("kinds", "remove", "skills", "--remove-copies")
        self.assertTrue(out.startswith("agt share kinds remove skills  ✓ skills don't share any more, removed 3 copies\n"), out)
        self.assertIn("\nYour originals stay:\n  ask-simply ", out)
        self.assertIn("\n  Share them again   agt share kinds add skills\n", out)

    def test_already_on(self):
        self.enable()
        self.share("kinds", "add", "mcp")
        self.assertEqual(self.share("kinds", "add", "mcp"), "agt share kinds add mcp  already on\n")
        self.assertEqual(self.share("kinds", "add", "skills"), "agt share kinds add skills  already on\n")

    def test_already_off_only_when_nothing_changed(self):
        self.enable()
        self.share("kinds", "add", "mcp")
        self.assertIn("✓ MCP servers don't share any more", self.share("kinds", "remove", "mcp"))
        self.assertEqual(self.share("kinds", "remove", "mcp"), "agt share kinds remove mcp  already off\n")
        self.assertEqual(json.loads(self.share("kinds", "remove", "mcp", "--json"))["mcp"], "off")  # --json as before

    def test_disable_says_what_stays_and_the_way_back(self):
        self.enable()
        out = self.share("disable")
        self.assertTrue(out.startswith("agt share disable  ✓ off\n\n  Every copy stays as it is, and your answers stay in "
                                       "~/.agt/settings.toml.\n  Removed the background watcher and agt's entries in "), out)
        self.assertTrue(out.endswith("\n  Turn it on again   agt share enable\n"), out)
        self.assertEqual(self.share("disable"), "agt share disable  already off\n\n  Turn it on again   agt share enable\n")

    def test_settings_split_into_yours_and_the_defaults(self):
        self.enable()
        out = self.share("settings")
        self.assertTrue(out.startswith("agt share settings  ~/.agt/settings.toml\n\nSet by you\n"
                                       "  enabled         true   "), out)
        self.assertIn("\n  targets         1 item picked ", out)  # gamma-apikey, left out
        self.assertIn("\nDefaults\n  instructions    off ", out)
        self.assertIn("\n  plugins         false ", out)
        self.assertTrue(out.endswith("\nSet a value with `agt config set share.<setting> <value>`.\n"), out)
        keys = [row["key"] for row in json.loads(self.share("settings", "--json"))["settings"]]
        self.assertEqual(keys[-3:], ["share.folders", "share.targets", "share.plugins"])  # added last, the rest in place


class ConfigShowTests(ItemBase):
    def show(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["config", "show", *extra])
        return code, out.getvalue()

    def test_only_what_is_set_unless_all(self):
        self.enable()
        code, out = self.show()
        self.assertEqual(code, 0)
        self.assertIn("\nSet explicitly\n  claude.primary ", out)
        self.assertIn("\n  share.enabled         true             user, line 4   agt share disable\n", out)
        self.assertNotIn("max_depth", out)
        rest = len(config_cli.KEYS) - 6
        self.assertIn(f"\n{rest} more settings use their defaults; `agt config show --all` lists every one.\n", out)
        code, out = self.show("--all")
        listed = [line.split()[0] for line in out.split("\nEvery setting\n")[1].split("\n\n")[0].splitlines()]
        self.assertEqual(listed, list(config_cli.KEYS))
        settings = json.loads(self.show("--json")[1])["settings"]
        self.assertEqual([row["key"] for row in settings], list(config_cli.KEYS))


if __name__ == "__main__":
    unittest.main()
