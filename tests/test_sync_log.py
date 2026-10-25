"""agt's log: sync runs, turn-offs, hand edits and commits, refused values, and agt log over them."""

import contextlib
import io
import json
import os
import shutil
import subprocess
from unittest.mock import patch

from helpers import TempHome
from test_share_items import ItemBase

from agents_router import cli, jobs, levels, synclog
from agents_router.share import settings


def logged(kind=None):
    return [entry for entry in synclog.entries() if kind is None or entry["type"] == kind]


class SyncRunTests(ItemBase):
    def test_a_sync_run_logs_its_command_and_each_write(self):
        self.enable()
        run = logged("sync")[0]
        self.assertEqual((run["command"], run["trigger"], run["workspace"]), ("agt share enable", "command", None))
        made = [change for change in run["changes"] if change["ok"]]
        self.assertEqual(len(made), 9)
        self.assertIn("dana", run["items"])
        dana = next(change for change in made if change["item"] == "agents/dana" and change["labels"] == ["kiro"])
        self.assertEqual(dana["path"], str(self.user / ".kiro" / "agents" / "dana.json"))
        self.assertGreaterEqual(run["seconds"], 0)
        self.assertEqual(logged("setting"), [])  # agt's own save of your answers isn't a hand edit
        self.share("sync")
        quiet = logged("sync")[0]
        self.assertEqual((quiet["command"], quiet["changes"]), ("agt share sync", []))
        self.assertTrue(synclog.line(quiet).endswith("agt share sync: nothing to change"), synclog.line(quiet))
        self.assertIn("agt share enable: ", synclog.line(run))

    def test_removing_a_copy_logs_it_with_its_backup(self):
        self.enable()
        self.share("pick", "dana", "--not-in", "kiro")
        run = logged("sync")[0]
        removed = [change for change in run["changes"] if change["action"] == "remove"]
        self.assertEqual([(change["item"], change["labels"]) for change in removed], [("agents/dana", ["kiro"])])
        self.assertTrue(removed[0]["backup"].endswith("dana.json"))

    def test_a_turn_off_logs_the_answer_and_each_copy(self):
        self.enable()
        self.share("cli", "remove", "kiro", "--keep-copies")
        run = logged("sync")[0]
        self.assertEqual((run["turned_off"], run["answer"]), ("kiro", "keep"))
        self.assertIn({"item": "agents/dana", "labels": ["kiro"], "path": str(self.user / ".kiro" / "agents" / "dana.json"),
                       "status": "agt"}, run["copies"])
        self.share("disable")
        off = logged("turn-off")[0]
        self.assertEqual((off["what"], off["answer"], off["by"]), ("sharing", "keep", "command"))

    def test_log_finds_an_items_runs(self):
        self.enable()
        self.share("pick", "dana", "--not-in", "kiro")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["log", "--item", "dana", "--json"]), 0)
        found = json.loads(out.getvalue())["entries"]
        self.assertEqual([entry["command"] for entry in found], ["agt share pick", "agt share enable"])


class HandEditTests(ItemBase):
    def user_settings(self):
        return self.user / ".agt" / "settings.toml"

    def edit(self, old, new):
        path = self.user_settings()
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def test_a_hand_edit_is_logged_at_the_next_sync_and_agts_own_writes_are_not(self):
        self.enable()
        self.edit('tool_widening = "allow-if-shell"', 'tool_widening = "skip"')
        self.share("sync")
        edits = logged("setting")
        self.assertEqual([(entry["key"], entry["old"], entry["new"], entry["by"], entry["level"]) for entry in edits],
                         [("share.tool_widening", "allow-if-shell", "skip", "hand edit", "user")])
        self.share("sync")
        self.assertEqual(len(logged("setting")), 1)  # noticed once
        self.share("pick", "dana", "--not-in", "kiro")  # agt saves the file again
        self.share("sync")
        self.assertEqual(len(logged("setting")), 1)

    def test_a_turn_off_by_hand_keeps_the_copies_and_lists_each_one(self):
        self.enable()
        link = self.user / ".kiro" / "skills" / "interactive-lesson-site"
        self.assertTrue(os.path.lexists(link))
        self.edit('kinds = ["agents", "skills"]', 'kinds = ["agents"]')
        self.share("sync")
        off = logged("turn-off")[0]
        self.assertEqual((off["what"], off["answer"], off["by"]), ("kind skills", "kept, nobody to ask", "hand edit"))
        self.assertIn("skills/interactive-lesson-site in kiro", off["copies"])
        self.assertTrue(os.path.lexists(link))  # kept, and no longer updated


class SettingsFileTests(TempHome):
    def setUp(self):
        super().setUp()
        (self.project / ".agt").mkdir()
        self.team = self.project / ".agt" / "settings.toml"

    def start(self):
        return jobs.create_job(runtime="claude", prompt="x", cwd=str(self.project), start=False)

    def test_the_first_sighting_is_silent_and_a_later_edit_is_logged_at_a_job_start(self):
        self.team.write_text("[codex]\nmax_jobs = 2\n", encoding="utf-8")
        self.start()
        self.assertEqual(logged(), [])
        self.team.write_text("[codex]\nmax_jobs = 3\nwhen_full = \"queue\"\n", encoding="utf-8")
        self.start()
        edits = sorted((entry["key"], entry["old"], entry["new"], entry["by"]) for entry in logged("setting"))
        self.assertEqual(edits, [("codex.max_jobs", 2, 3, "hand edit"), ("codex.when_full", None, "queue", "hand edit")])
        self.assertEqual({entry["workspace"] for entry in logged("setting")}, {str(self.project)})
        self.team.unlink()
        self.start()
        self.assertEqual(sorted((entry["key"], entry["new"]) for entry in logged("setting")[:2]),
                         [("codex.max_jobs", None), ("codex.when_full", None)])

    def test_a_committed_change_names_its_commit(self):
        if shutil.which("git") is None:
            self.skipTest("needs git")
        git = lambda *args: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                                           cwd=self.project, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        self.team.write_text("max_depth = 1\n", encoding="utf-8")
        git("add", ".agt/settings.toml")
        git("commit", "-qm", "team settings")
        self.start()
        self.team.write_text("max_depth = 2\n", encoding="utf-8")
        git("commit", "-qam", "deeper nesting")
        self.start()
        entry = logged("setting")[0]
        self.assertEqual((entry["key"], entry["by"], entry["commit"]), ("max_depth", "git", git("rev-parse", "HEAD")))
        self.assertIn("from git commit", synclog.line(entry))

    def test_a_refused_value_is_logged_once(self):
        self.team.write_text("[codex]\nmax_jobs_per_account = 0\n", encoding="utf-8")
        for _ in range(2):
            with self.assertRaises(levels.SettingsError):
                self.start()
        refused = logged("refused")
        self.assertEqual(len(refused), 1)
        self.assertTrue(refused[0]["message"].startswith(f"{self.team}:2: [codex] max_jobs_per_account"))

    def test_values_that_can_hold_secrets_are_logged_as_hashes(self):
        self.team.write_text('[mcp.env]\nAPI_TOKEN = "first-secret"\n', encoding="utf-8")
        self.start()
        self.team.write_text('[mcp.env]\nAPI_TOKEN = "second-secret"\n', encoding="utf-8")
        self.start()
        entry = logged("setting")[0]
        self.assertEqual(entry["key"], "mcp.env.API_TOKEN")
        self.assertTrue(entry["old"].startswith("sha256:") and entry["new"].startswith("sha256:"))
        text = "".join(path.read_text(encoding="utf-8") for path in synclog.folder().iterdir())
        self.assertNotIn("secret", text.replace("sha256:", ""))

    def test_config_set_is_logged_once_not_again_as_a_hand_edit(self):
        self.start()
        cwd = os.getcwd()
        os.chdir(self.project)
        try:
            with contextlib.redirect_stdout(io.StringIO()), patch("sys.stdin", io.StringIO("")):
                self.assertEqual(cli.main(["config", "set", "codex.max_jobs", "2"]), 0)
        finally:
            os.chdir(cwd)
        self.start()
        self.assertEqual([(entry["key"], entry["by"]) for entry in logged("setting")], [("codex.max_jobs", "command")])
