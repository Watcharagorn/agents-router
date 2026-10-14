import os
from pathlib import Path
import tempfile
import unittest

from cli_subagents.hosts import detect_host, host_context, parent_note


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


if __name__ == "__main__":
    unittest.main()
