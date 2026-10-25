"""agt share's folder list: which CLI folders exist, which are suggested, and why some are greyed out."""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from helpers import FAKE_HOST, ShareHome

from agents_router.config import load_config
from agents_router.share import providers


class FolderListTests(ShareHome):
    def machine(self):
        """This Mac in miniature: two Claude folders, a personal profile, Codex and Kiro."""

        claude, lab = self.folder(".claude"), self.folder(".claude-lab")
        self.agent(claude, "dana", tools="Bash, Read")
        self.agent(lab, "day-plan")
        self.skill(lab, "ask-simply")
        personal = self.folder(".claude-other")
        (personal / "settings.json").write_text("{}", encoding="utf-8")
        (self.folder(".claude-unrelated") / "notes.txt").write_text("x", encoding="utf-8")  # no Claude markers
        self.folder(".codex")
        (self.user / ".agents").mkdir()
        os.symlink(lab / "skills", self.user / ".agents" / "skills", target_is_directory=True)
        self.kiro_agent("kirocrew-conductor")
        return claude, lab, personal

    def rows(self, **kwargs):
        return {row.provider.label: row for row in providers.candidates(load_config(), **kwargs)}

    def test_finds_every_cli_folder_with_labels(self):
        self.machine()
        rows = self.rows()
        self.assertEqual(list(rows), ["claude", "claude-lab", "claude-other", "codex", "kiro"])
        self.assertEqual({label: row.provider.cli for label, row in rows.items()},
                         {"claude": "claude", "claude-lab": "claude", "claude-other": "claude", "codex": "codex", "kiro": "kiro"})
        for row in rows.values():
            self.assert_inside(row.provider.folder, *(location.path for kind in row.provider.locations.values() for location in kind))

    def test_suggests_installed_clis_with_agents_or_skills(self):
        self.machine()
        rows = self.rows()
        self.assertEqual({label: row.suggested for label, row in rows.items()},
                         {"claude": True, "claude-lab": True, "claude-other": False, "codex": True, "kiro": True})
        self.assertEqual(rows["claude"].holds, {"agents": 1, "skills": 0})
        self.assertEqual(rows["codex"].holds, {"agents": 0, "skills": 1})  # through ~/.agents/skills
        self.assertEqual(rows["kiro"].holds, {"agents": 0, "skills": 0})  # KiroCrew agents aren't shared,
        self.assertTrue(rows["kiro"].in_use)  # but Kiro is in use, so it's a suggested target
        self.assertIsNone(rows["kiro"].reason)

    def test_codex_skills_and_a_claude_folder_are_one_location_by_real_path(self):
        _, lab, _ = self.machine()
        codex = self.rows()["codex"].provider
        first = codex.locations["skills"][0]
        self.assertEqual(first.path, self.user / ".agents" / "skills")
        self.assertEqual(first.real, str((lab / "skills").resolve()))
        self.assertFalse(codex.locations["skills"][1].writable)  # ~/.codex/skills is only read
        self.assertEqual(codex.writable("skills"), first)

    def test_greyed_out_rows_say_why(self):
        claude, _, _ = self.machine()
        gemini = self.folder(".gemini")
        (gemini / "antigravity-cli").mkdir()
        account = self.root / "accounts" / "claude-2"
        account.mkdir(parents=True)
        agt_account = self.agt_home / "homes" / "claude" / "max-2"
        agt_account.mkdir(parents=True)
        self.agt_home.mkdir(exist_ok=True)
        (self.agt_home / "config.toml").write_text(
            f"[claude]\nprimary = {json.dumps(str(claude))}\n[[claude.accounts]]\nname = \"claude-2\"\nhome = {json.dumps(str(account))}\n",
            encoding="utf-8")
        folders = [("claude", claude), ("claude", account), ("claude", agt_account), ("antigravity", gemini)]
        rows = self.rows(folders=folders)
        self.assertEqual(rows["claude-2"].reason, f"agt account, follows {claude} through links")
        self.assertEqual(rows["claude-max-2"].reason, "agt's own account folders")
        self.assertIn("agy isn't installed", rows["antigravity"].reason)
        self.assertFalse(any(row.suggested for label, row in rows.items() if label != "claude"))
        with patch.dict(os.environ, {"AGT_AGY_COMMAND": json.dumps([sys.executable, str(FAKE_HOST), "agy"])}):
            self.assertIsNone(self.rows(folders=folders)["antigravity"].reason)
            self.assertEqual(self.rows(folders=folders, no_agy=True)["antigravity"].reason, "left out with --no-agy")

    def test_same_folder_names_get_path_labels(self):
        first, second = self.root / "a" / ".claude", self.root / "b" / ".claude"
        for folder in (first, second):
            folder.mkdir(parents=True)
        made = providers.make_providers([("claude", first), ("claude", second), ("claude", first)], load_config())
        self.assertEqual([provider.label for provider in made], [f"claude@{first}", f"claude@{second}"])

    def test_a_missing_cli_binary_is_found_without_running_anything(self):
        self.machine()
        with patch.dict(os.environ, {"AGT_KIRO_COMMAND": ""}), patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            rows = self.rows()
        self.assertIn("kiro-cli isn't installed", rows["kiro"].reason)
        run.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
