import os
from pathlib import Path
import tempfile
import unittest
import unittest.mock

import helpers  # noqa: F401  (isolates the test process from your real folders)

from agents_router.hosts import detect_host, host_context, parent_note


class HostTests(unittest.TestCase):
    def test_detect_from_client_name_and_env(self):
        self.assertEqual(detect_host("claude-code", {}), "claude")
        self.assertEqual(detect_host("codex-mcp-client", {}), "codex")
        self.assertEqual(detect_host("Antigravity", {}), "antigravity")
        self.assertEqual(detect_host(None, {"CLAUDECODE": "1"}), "claude")
        self.assertEqual(detect_host(None, {}), "unknown")

    def test_context_lists_only_existing_paths_and_note_skips_same_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "claude-config"
            (config / "skills").mkdir(parents=True)
            (config / "CLAUDE.md").write_text("rules", encoding="utf-8")
            project = root / "project"
            (project / ".claude" / "skills").mkdir(parents=True)
            context = host_context("claude", str(project), {"CLAUDE_CONFIG_DIR": str(config)})
            self.assertEqual(context["instructions"], [str(config / "CLAUDE.md")])
            self.assertEqual(context["skills"], [str(config / "skills"), str(project / ".claude" / "skills")])
            note = parent_note(context, "codex")
            self.assertIn("Claude Code session", note)
            self.assertIn(str(config / "CLAUDE.md"), note)
            self.assertEqual(parent_note(context, "claude"), "")
            self.assertEqual(parent_note({"host": "unknown"}, "codex"), "")

    def test_antigravity_skills_come_from_the_cli_or_the_app_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".gemini" / "config" / "skills").mkdir(parents=True)
            project = home / "project"
            (project / ".agents" / "skills").mkdir(parents=True)
            with unittest.mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
                context = host_context("antigravity", str(project), {})
            self.assertEqual(context["skills"], [str(home / ".gemini" / "config" / "skills"), str(project / ".agents" / "skills")])


if __name__ == "__main__":
    unittest.main()
