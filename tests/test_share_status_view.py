"""What `agt share status` and the session line say about waits, and the plan table on a narrow terminal."""

import contextlib
import io
import os
import re
import unittest
from unittest.mock import patch

from test_share_plan import FLAGS, MacCopy
from test_share_sync import YES

from pathlib import Path

from test_ui import HAVE_RICH

from agents_router import cli, fsutil
from agents_router.share import attention, status_view, watch

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plugin_wait(name, label, why="Codex doesn't load its agents (reviewer)"):
    key = f"plugins/{name}@tools"
    return {"id": f"plugin:{key}:{label}", "kind": "plugin", "item": key, "who": "choice",
            "message": f"plugin {name} waits for your answer before it goes to {label}: {why}",
            "fix": [f"agt share pick {key} --in {label}", f"agt share pick {key} --not-in {label}"],
            "label": label, "why": why}


class GroupedTests(unittest.TestCase):
    def test_one_answer_in_two_folders_is_one_thing_to_do(self):
        found = attention.grouped([plugin_wait("diagrams", "claude"), plugin_wait("diagrams", "claude-lab"),
                                   plugin_wait("deck", "codex")])
        self.assertEqual(len(found), 2)
        diagrams = found[0]
        self.assertEqual(diagrams["labels"], ["claude", "claude-lab"])
        self.assertEqual(diagrams["ids"], ["plugin:plugins/diagrams@tools:claude", "plugin:plugins/diagrams@tools:claude-lab"])
        self.assertIn("before it goes to claude and claude-lab: Codex", diagrams["message"])
        self.assertEqual(diagrams["fix"], ["agt share pick plugins/diagrams@tools --in claude,claude-lab",
                                           "agt share pick plugins/diagrams@tools --not-in claude,claude-lab"])

    def test_a_different_answer_stays_apart(self):
        found = attention.grouped([plugin_wait("diagrams", "claude"), plugin_wait("diagrams", "codex", why="other")])
        self.assertEqual([entry["labels"] for entry in found], [["claude"], ["codex"]])

    def test_missing_sections_are_one_adopt(self):
        entries = [{"id": f"adopt:instructions/instructions:{label}", "kind": "adopt", "item": "instructions/instructions",
                    "who": "choice", "message": f"{label}'s AGENTS.md has no agt share block, so it doesn't share instructions",
                    "fix": [f"agt share adopt {label}"], "label": label} for label in ("claude-lab", "codex", "kiro")]
        found = attention.grouped(entries)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["fix"], ["agt share adopt claude-lab codex kiro"])  # adopt takes labels, not a list

    def test_a_person_gets_commands_that_ask(self):
        conflict = {"fix": ["agt share resolve mcp/notes --prefer <label>"]}
        new = {"fix": ["agt share pick dana --in <label>"]}
        self.assertEqual(attention.commands(conflict), ["agt share resolve mcp/notes"])
        self.assertEqual(attention.commands(new), ["agt share pick dana"])

    def test_a_repos_wait_names_the_repo(self):
        repo = {"root": Path.home() / "code" / "site"}
        delete = {"kind": "delete", "fix": ["agt share rm dana"]}
        frozen = {"kind": "frozen", "fix": ["agt share sync --allow-deletes kiro", "agt share sync --refill kiro"]}
        self.assertEqual(status_view._commands(delete, repo), [("Remove its copies", "agt share rm dana --repo ~/code/site")])
        self.assertEqual(status_view._commands(frozen, repo)[0],
                         ("Keep them out", "cd ~/code/site && agt share sync --allow-deletes kiro"))

    def test_entries_without_a_folder_never_join(self):
        conflict = {"id": "conflict:agents/dana", "kind": "conflict", "item": "agents/dana", "who": "choice",
                    "message": "dana differs", "fix": ["agt share resolve dana"]}
        other = {**conflict, "id": "conflict:agents/rover", "item": "agents/rover"}
        found = attention.grouped([conflict, other])
        self.assertEqual([entry["labels"] for entry in found], [[], []])


def run(*argv, **env):
    out = io.StringIO()
    with patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, ANSI.sub("", out.getvalue())


class SessionLineTests(MacCopy):
    def test_one_line_per_plugin_capped_at_three_and_a_count(self):
        run("share", *YES)
        waits = [plugin_wait("diagrams", "claude"), plugin_wait("diagrams", "claude-lab")]
        waits += [plugin_wait(name, "codex") for name in ("deck", "drafts", "formatter")]
        fsutil.write_json(attention.path(), {"entries": waits})
        lines = watch.attention_lines(remember=False)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "agt share: plugin diagrams waits for your answer before it goes to claude and claude-lab: "
                                   "Codex doesn't load its agents (reviewer); "
                                   "`agt share pick plugins/diagrams@tools --in claude,claude-lab`.")
        self.assertEqual(lines[3], "agt share: 1 more; `agt share status` lists them.")

    def test_a_failed_install_points_at_status(self):
        run("share", *YES)
        failed = {**plugin_wait("formatter", "codex", why="`codex plugin add formatter@tools` failed (exit 1)"),
                  "id": "failing:plugins/formatter@tools:codex", "kind": "failing", "who": "action",
                  "message": "agt couldn't install plugin formatter in codex: `codex plugin add formatter@tools` failed (exit 1)"}
        fsutil.write_json(attention.path(), {"entries": [failed]})
        self.assertEqual(watch.attention_lines(remember=False),
                         ["agt share: agt couldn't install plugin formatter in codex; `agt share status` shows what to run."])

    def test_a_failed_write_names_its_folders_and_points_at_status(self):
        run("share", *YES)
        failed = {"id": "failing:agents/dana", "kind": "failing", "item": "agents/dana", "who": "action",
                  "message": "agt couldn't write dana in the last 3 runs: disk full", "fix": [], "folders": ["codex"], "runs": 3}
        fsutil.write_json(attention.path(), {"entries": [failed]})
        self.assertEqual(watch.attention_lines(remember=False),
                         ["agt share: agt couldn't write dana in codex (3 runs in a row); `agt share status` shows why."])

    def test_the_badge_counts_what_status_counts(self):
        run("share", *YES)
        fsutil.write_json(attention.path(), {"entries": [plugin_wait("diagrams", "claude"), plugin_wait("diagrams", "claude-lab")]})
        self.assertEqual(watch.badge(), "share:1!")

    def test_a_grouped_line_is_marked_shown_for_every_folder(self):
        run("share", *YES)
        fsutil.write_json(attention.path(), {"entries": [plugin_wait("diagrams", "claude"), plugin_wait("diagrams", "claude-lab")]})
        self.assertEqual(len(watch.attention_lines()), 1)
        self.assertEqual(watch.attention_lines(), [])


class NarrowTableTests(MacCopy):
    @unittest.skipUnless(HAVE_RICH, "rich isn't installed")
    def test_a_narrow_terminal_gets_one_line_per_item(self):
        code, out = run("share", "enable", *FLAGS, "--dry-run", AGT_COLOR="always", COLUMNS="50")
        self.assertEqual(code, 0)
        self.assertIn("    dana          + claude-lab, codex, kiro\n", out)
        self.assertIn("    explorer      + kiro  -¹ codex\n", out)
        self.assertIn("    interactive-lesson-site\n                  + claude, kiro\n", out)
        self.assertIn("  Folders not named already have the item.\n", out)
        self.assertIn("  ¹ Codex has a built-in agent named explorer.\n", out)

    def test_plain_output_always_gets_the_grid(self):
        code, out = run("share", "enable", *FLAGS, "--dry-run", COLUMNS="50")
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertIn("                             claude-lab  claude  codex  kiro\n", out)
        self.assertIn("    explorer                 ·           ·       -¹     +\n", out)
        self.assertIn("  + copy  · has it  - can't be copied there  ! conflict\n", out)
        self.assertNotIn("Folders not named", out)


class StatusTests(MacCopy):
    def test_status_off_and_all_in_sync(self):
        self.assertEqual(run("share", "status"), (0, "agt share  not set up yet\n"
                                                     "  See what it would do  agt share enable --dry-run\n"))
        run("share", *YES)
        code, out = run("share", "status")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:5], ["agt share  on, all in sync", "",
                                                "  Folders   claude-lab, claude, codex, kiro",
                                                "  Shared    5 agents and skills in sync, 11 not shared",
                                                "  Watcher   on, no run yet"])
        code, out = run("share", "sync")
        self.assertEqual((code, out), (0, "agt share sync  ✓ everything is in sync, nothing to write\n"))

    def test_a_sync_that_leaves_a_conflict_isnt_in_sync(self):
        run("share", "enable", *FLAGS, "--yes", "--accept-suggested")  # gamma-apikey differs, and waits for you
        code, out = run("share", "sync", "--dry-run")
        self.assertIn("\n    Pick one   agt share resolve gamma-apikey\n", out)  # a sync takes no --resolve
        self.assertNotIn("will wait", out)
        code, out = run("share", "sync")
        self.assertEqual(out.splitlines()[:2], ["agt share sync  nothing to write",
                                                "  1 thing waits for you: `agt share status` lists it."])

if __name__ == "__main__":
    unittest.main()
