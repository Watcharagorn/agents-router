import json
from pathlib import Path
import unittest
from unittest.mock import patch

from executor_workspace import gated_executor_workspace


class ExecutorWorkspaceTests(unittest.TestCase):
    def test_creates_gate_without_touching_project(self):
        project = Path.cwd().resolve()
        marker = project / ".agents" / "hooks.json"
        existed_before = marker.exists()
        with patch("broker_security._AVAILABLE_TOKENS", ["a" * 32]):
            with gated_executor_workspace(
                project_root=project,
                objective="test objective",
                agy_cmd="agy.exe",
                audit_dir=project / ".test-audit",
            ) as harness:
                workspace = harness.workspace
                data = json.loads((workspace / ".agents" / "hooks.json").read_text())
                command = data["codex-gemini-review-gate"]["PreToolUse"][0]["hooks"][0]["command"]
                self.assertIn("review_gate.py", command)
                self.assertIn("--config-b64", command)
                self.assertEqual((workspace / "objective.txt").read_text(), "test objective")
                self.assertTrue(harness.request_dir.is_dir())
                self.assertTrue(harness.request_dir.is_relative_to(workspace))
                self.assertEqual(harness.broker_path.name, "reviewed_exec.cmd")
                self.assertEqual(harness.approval_id, "a" * 32)
        self.assertEqual(marker.exists(), existed_before)


if __name__ == "__main__":
    unittest.main()
