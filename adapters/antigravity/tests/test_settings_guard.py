import json
from pathlib import Path
import tempfile
import unittest

from settings_guard import enforce_bridge_settings


class SettingsGuardTests(unittest.TestCase):
    def test_preserves_settings_disables_overages_and_adds_exact_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "project"
            workspace.mkdir()
            settings = root / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "enableTelemetry": False,
                        "useG1Credits": True,
                        "trustedWorkspaces": [str(root / "existing")],
                        "permissions": {"deny": ["command(rm -rf)"]},
                    }
                ),
                encoding="utf-8",
            )
            result = enforce_bridge_settings(settings, workspace)
            value = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(
                result,
                {
                    "overages_changed": True,
                    "workspace_added": True,
                    "workspace_write_added": True,
                },
            )
            self.assertIs(value["useG1Credits"], False)
            self.assertIn(str(workspace), value["trustedWorkspaces"])
            self.assertEqual(value["permissions"]["deny"], ["command(rm -rf)"])
            self.assertIn(
                f"write_file({workspace.as_posix()})",
                value["permissions"]["allow"],
            )

    def test_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "project"
            workspace.mkdir()
            settings = root / "settings.json"
            settings.write_text(
                json.dumps(
                    {"useG1Credits": False, "trustedWorkspaces": [str(workspace)]}
                ),
                encoding="utf-8",
            )
            before = settings.read_bytes()
            result = enforce_bridge_settings(settings, workspace)
            self.assertEqual(
                result,
                {
                    "overages_changed": False,
                    "workspace_added": False,
                    "workspace_write_added": True,
                },
            )
            self.assertNotEqual(settings.read_bytes(), before)
            second_before = settings.read_bytes()
            second = enforce_bridge_settings(settings, workspace)
            self.assertEqual(
                second,
                {
                    "overages_changed": False,
                    "workspace_added": False,
                    "workspace_write_added": False,
                },
            )
            self.assertEqual(settings.read_bytes(), second_before)


if __name__ == "__main__":
    unittest.main()
