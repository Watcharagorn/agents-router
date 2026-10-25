"""agt share's dry runs: the item × folder table, worked out only by reading."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import time
import unittest

from helpers import ShareHome

from agents_router import cli

DANA_TOOLS = "Bash, Read, Edit, mcp__tracker__tracker_search_issues, mcp__brave-search"
FLAGS = ["--claude-dir", "~/.claude-lab", "--claude-dir", "~/.claude", "--codex-dir", "~/.codex", "--kiro-dir", "~/.kiro",
         "--no-agy", "--projects", "none", "--tool-widening", "allow-if-shell", "--auto-copy", "on"]


class MacCopy(ShareHome):
    """A small copy of this Mac's folders: what the read-only gate ran against, in a temp home."""

    def setUp(self):
        super().setUp()
        claude, lab = self.folder(".claude"), self.folder(".claude-lab")
        self.claude, self.lab = claude, lab
        self.agent(claude, "dana", tools=DANA_TOOLS, body="Dana's long instructions.")
        for folder in (claude, lab):
            self.agent(folder, "day-plan", tools="Bash, mcp__tracker__tracker_log_work", body="Wrap the day.")
            self.agent(folder, "explorer", tools="Bash, Read, mcp__brave-search", body="Explore.")
            (folder / "skills" / "synced").mkdir(parents=True)
        (claude / "agents" / "templates").mkdir()
        (claude / "skills" / ".trash").mkdir(parents=True)
        self.skill(claude, "ask-simply")
        os.symlink(claude / "skills" / "ask-simply", lab / "skills" / "ask-simply", target_is_directory=True)
        self.skill(claude, "gamma-apikey", body="Old steps.")
        self.skill(lab, "gamma-apikey", body="New steps.")
        (claude / "skills" / "ui-polish-kit").mkdir()
        self.skill(lab, "interactive-lesson-site")
        codex = self.folder(".codex")
        hatch = self.skill(codex, "haven-kit")
        (codex / "config.toml").write_text(f"[[skills.config]]\npath = {json.dumps(str(hatch / 'SKILL.md'))}\nenabled = false\n",
                                           encoding="utf-8")
        (self.user / ".agents").mkdir()
        os.symlink(lab / "skills", self.user / ".agents" / "skills", target_is_directory=True)
        for name in ("kirocrew-conductor", "kirocrew-heartbeat", "kirocrew-knowledge", "kirocrew-scout", "kirocrew"):
            self.kiro_agent(name, tools=["fs_read", "@kirocrew-core"])
        (self.user / ".kiro" / "agents" / "kirocrew.lock").write_text("", encoding="utf-8")
        self.kiro_agent("ops-reporter")
        old = time.time() - 60
        for path in self.root.rglob("*"):
            os.utime(path, (old, old), follow_symlinks=False)
        newer = time.time() - 30
        os.utime(lab / "skills" / "gamma-apikey" / "SKILL.md", (newer, newer))  # the lab copy is the newer one

    def snapshot(self):
        found = {}
        for path in sorted(self.user.rglob("*")):
            found[str(path)] = ("link", os.readlink(path)) if path.is_symlink() else (
                "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest())
        return found

    def run_share(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["share", *args])
        return code, out.getvalue()

    def dry_run(self, *extra):
        before = self.snapshot()
        code, out = self.run_share("enable", *FLAGS, *extra, "--dry-run", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.snapshot(), before, "a dry run wrote something")
        return json.loads(out)

    def cells(self, proposal, name):
        item = next(item for item in proposal["items"] if item["name"] == name)
        return {label: cell["state"] for label, cell in item["cells"].items()}


class DryRunTests(MacCopy):
    def test_the_plan_on_a_copy_of_this_mac(self):
        proposal = self.dry_run()
        changes = sorted((c["name"], tuple(c["labels"]), c["action"]) for c in proposal["changes"])
        self.assertEqual(changes, [
            ("ask-simply", ("kiro",), "link"),
            ("dana", ("claude-lab",), "copy"), ("dana", ("codex",), "copy"), ("dana", ("kiro",), "copy"),
            ("day-plan", ("codex",), "copy"), ("day-plan", ("kiro",), "copy"),
            ("explorer", ("kiro",), "copy"),
            ("interactive-lesson-site", ("claude",), "link"), ("interactive-lesson-site", ("kiro",), "link"),
        ])
        self.assertEqual(proposal["summary"], "9 changes in 4 folders, 1 conflicts")
        self.assertEqual(self.cells(proposal, "explorer")["codex"], "blocked")
        explorer = next(item for item in proposal["items"] if item["name"] == "explorer")
        self.assertEqual(explorer["cells"]["codex"]["note"], "Codex has a built-in agent named explorer")
        # Codex sees the lab skills through ~/.agents/skills, so it needs no links.
        self.assertEqual(self.cells(proposal, "interactive-lesson-site"), {"claude": "copy", "claude-lab": "has", "codex": "has", "kiro": "copy"})
        self.assertEqual(sorted(proposal["new_folders"]), sorted([str(self.user / ".codex" / "agents"), str(self.user / ".kiro" / "skills")]))
        skill_link = next(c for c in proposal["changes"] if c["name"] == "ask-simply")
        self.assertEqual(skill_link["source"], str((self.claude / "skills" / "ask-simply").resolve()))

    def test_conflicts_need_an_answer(self):
        proposal = self.dry_run()
        need = proposal["needs_answer"]
        self.assertEqual([entry["id"] for entry in need], ["resolve:gamma-apikey"])
        choices = {choice["value"]: choice["newer"] for choice in need[0]["choices"]}
        self.assertEqual(choices, {"claude-lab": True, "claude": False, "skip": False})
        self.assertEqual(self.cells(proposal, "gamma-apikey"), {"claude": "has", "claude-lab": "has", "codex": "has", "kiro": "conflict"})

    def test_what_is_never_shared_is_listed_with_the_reason(self):
        excluded = {(entry["kind"], entry["name"]): entry["reason"] for entry in self.dry_run()["excluded"]}
        self.assertEqual(excluded[("agents", "kirocrew-scout")], "a KiroCrew agent")
        self.assertEqual(excluded[("agents", "ops-reporter")], "relies on KiroCrew-only tools")
        self.assertEqual(excluded[("skills", "synced")], "skills/synced is never shared")
        self.assertEqual(excluded[("skills", "ui-polish-kit")], "no SKILL.md")
        self.assertEqual(excluded[("skills", "haven-kit")], "disabled in Codex")
        self.assertNotIn(("agents", "templates"), excluded)
        self.assertFalse([name for _, name in excluded if name.startswith(".")])

    def test_missing_servers_and_tools_are_notes(self):
        notes = [note for item in self.dry_run()["items"] for note in item["notes"]]
        left = "; the Kiro copy leaves out its {} tools"
        self.assertIn("dana in claude-lab, codex, kiro: no `tracker` server (no CLI here has one)" + left.format("tracker"), notes)
        self.assertIn("dana in claude-lab, codex, kiro: no `brave-search` server (no CLI here has one)" + left.format("brave-search"), notes)
        self.assertIn("explorer in kiro: no `brave-search` server (no CLI here has one)" + left.format("brave-search"), notes)

    def test_a_server_one_folder_has_is_found_by_name_only(self):
        (self.user / ".claude.json").write_text(json.dumps({"mcpServers": {"tracker": {"command": "tracker", "env": {"TOKEN": "x"}}}}),
                                                encoding="utf-8")
        notes = [note for item in self.dry_run()["items"] for note in item["notes"]]
        self.assertIn("dana in claude-lab, codex, kiro: no `tracker` server (Phase 3 brings it along); the Kiro copy leaves out its tracker tools",
                      notes)

    def test_skip_unticks_one_cell_and_shows_what_it_would_save(self):
        proposal = self.dry_run("--skip", "dana@codex")
        self.assertEqual(proposal["summary"], "8 changes in 4 folders, 1 conflicts")
        self.assertEqual(self.cells(proposal, "dana")["codex"], "skipped")
        _, text = self.run_share("enable", *FLAGS, "--skip", "dana@codex", "--dry-run")
        self.assertIn("Would save in your user settings:\n  dana: not in codex", text)

    def test_resolve_picks_a_version_for_a_conflict(self):
        proposal = self.dry_run("--resolve", "gamma-apikey=claude-lab")
        resolved = [c for c in proposal["changes"] if c["name"] == "gamma-apikey"]
        self.assertEqual(sorted((tuple(c["labels"]), c["action"]) for c in resolved), [(("claude",), "relink"), (("kiro",), "link")])
        self.assertEqual(proposal["needs_answer"], [])
        self.assertEqual(self.dry_run("--resolve", "gamma-apikey=skip")["needs_answer"], [])

    def test_only_keeps_an_item_in_some_clis(self):
        proposal = self.dry_run("--only", "dana=claude-lab")
        self.assertEqual(self.cells(proposal, "dana"), {"claude": "has", "claude-lab": "copy", "codex": "skipped", "kiro": "skipped"})

    def test_tool_widening_skip_keeps_tool_limited_agents_out_of_codex(self):
        proposal = self.dry_run("--tool-widening", "skip")
        self.assertEqual(self.cells(proposal, "dana")["codex"], "blocked")
        self.assertEqual(self.cells(proposal, "day-plan")["codex"], "blocked")

    def test_text_table_and_command(self):
        code, text = self.run_share("enable", *FLAGS, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("agt share: dry run, nothing was written.", text)
        self.assertIn("claude-lab  claude      codex       kiro", text)  # columns follow the --claude-dir order
        self.assertRegex(text, r"\n  dana +\+ +· +\+ +\+")
        self.assertRegex(text, r"\n  interactive-lesson-site +· +\+ +· +\+")
        self.assertIn("explorer in codex: Codex has a built-in agent named explorer", text)
        self.assertIn("gamma-apikey (skills):", text)
        self.assertIn("9 changes in 4 folders.", text)
        digest = self.dry_run()["plan"]
        self.assertIn(f"Plan {digest}. To apply it:", text)
        self.assertIn(f"  agt share enable --yes --if-plan {digest} --claude-dir '~/.claude-lab'", text)

    def test_the_plan_id_changes_when_an_item_changes(self):
        first = self.dry_run()["plan"]
        self.assertEqual(self.dry_run()["plan"], first)
        self.agent(self.claude, "dana", tools=DANA_TOOLS, body="Edited.")
        os.utime(self.claude / "agents" / "dana.md", (time.time() - 60,) * 2)
        self.assertNotEqual(self.dry_run()["plan"], first)

    def test_a_just_saved_file_makes_its_item_wait(self):
        self.agent(self.claude, "dana", tools=DANA_TOOLS, body="Being edited.")
        proposal = self.dry_run()
        dana = next(item for item in proposal["items"] if item["name"] == "dana")
        self.assertEqual((dana["decision"], dana["reason"]), ("wait", "saved less than 3 s ago"))
        self.assertNotIn("dana", [change["name"] for change in proposal["changes"]])

    def test_suggested_folders_are_used_without_flags(self):
        before = self.snapshot()
        code, out = self.run_share("enable", "--dry-run", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.snapshot(), before)
        used = {folder["label"] for folder in json.loads(out)["folders"] if folder["used"]}
        self.assertEqual(used, {"claude", "claude-lab", "codex", "kiro"})

    def test_scan_lists_every_choice_and_writes_nothing(self):
        before = self.snapshot()
        code, out = self.run_share("scan", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(), before)
        data = json.loads(out)
        self.assertEqual([kind["kind"] for kind in data["kinds"] if kind["suggested"]], ["agents", "skills"])
        self.assertIn("claude-lab", [folder["label"] for folder in data["folders"]])
        self.assertEqual(data["needs_answer"][0]["id"], "resolve:gamma-apikey")

    def test_nothing_is_written_without_yes_or_with_an_open_conflict(self):
        before = self.snapshot()
        code, out = self.run_share("enable", *FLAGS)
        self.assertEqual(code, 2)
        self.assertIn("Nothing was written. Add --yes", out)
        code, out = self.run_share("enable", *FLAGS, "--yes", "--json")
        self.assertEqual(code, 3)
        answer = json.loads(out)
        self.assertEqual(answer["error"], "needs_choice")
        self.assertIn("--resolve gamma-apikey=<choice>", answer["retry"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.agt_home / "state" / "share" / "state.json").exists())
        for args in (["sync"], ["sync", "--dry-run"], ["status"]):
            with self.subTest(args=args):
                code, out = self.run_share(*args)
                self.assertEqual((code, out.strip()), (0, "agt share isn't enabled yet. `agt share enable --dry-run` shows what it would do."))

    def test_bad_flags_are_errors(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for args in (["--kinds", "commands"], ["--kinds", "hooks"], ["--kinds", "plugins"], ["--kinds", "nope"], ["--skip", "dana@nowhere"], ["--skip", "dana"],
                         ["--projects", str(self.root / "missing")], ["--claude-dir", str(self.root / "missing")]):
                with self.subTest(args=args):
                    code, _ = self.run_share("enable", *args, "--dry-run")
                    self.assertEqual(code, 2)
        self.assertIn("later phase", err.getvalue())
        self.assertIn("hooks aren't a kind of item", err.getvalue())
        self.assertIn("no folder labelled nowhere", err.getvalue())


if __name__ == "__main__":
    unittest.main()
