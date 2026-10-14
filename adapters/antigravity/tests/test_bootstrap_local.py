import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bootstrap_local import prepare


class BootstrapLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bridge = self.root / "repo" / "adapters" / "antigravity"
        self.bridge.mkdir(parents=True)
        (self.bridge / "reviewed_exec.cmd").write_text("@echo off\n")
        (self.bridge / "launch_mcp.py").write_text("# fixture\n")
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.state = self.root / "private-state"
        self.settings = self.root / "user-settings.json"
        self.output = self.bridge / ".local"
        self.settings.write_text(json.dumps({"unrelated": 123, "permissions": {"allow": ["read_file(existing)"]}}))

    def tearDown(self):
        self.temp.cleanup()

    def run_setup(self, **kwargs):
        return prepare(self.bridge, self.workspace, self.state, self.settings, self.output, **kwargs)

    def test_dry_run_does_not_create_state_or_change_settings(self):
        before = self.settings.read_bytes()
        result = self.run_setup()
        self.assertFalse(result["written"])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.output.exists())
        self.assertEqual(before, self.settings.read_bytes())

    def test_fresh_state_works_with_archived_launcher_contract_and_is_reused(self):
        self.run_setup(write=True, configure_agy=True)
        first = {p.name: p.read_bytes() for p in self.state.iterdir() if p.is_file()}
        self.assertEqual(len(first["broker-secret.bin"]), 32)
        self.assertRegex(first["runtime-secret.txt"].decode().strip(), r"^[0-9a-f]{64}$")
        tokens = json.loads(first["broker-tokens.json"])
        self.assertEqual(len(tokens), 8)
        self.assertEqual(len(set(tokens)), 8)
        self.run_setup(write=True, configure_agy=True)
        self.assertEqual(first, {p.name: p.read_bytes() for p in self.state.iterdir() if p.is_file()})
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["unrelated"], 123)
        self.assertIn("read_file(existing)", settings["permissions"]["allow"])
        broker_rules = [x for x in settings["permissions"]["allow"] if "reviewed_exec.cmd" in x]
        self.assertEqual(len(broker_rules), 8)
        self.assertFalse(settings["useG1Credits"])
        config = json.loads((self.output / "mcp.json").read_text())
        env = config["mcpServers"]["antigravity_agents"]["env"]
        self.assertEqual(Path(env["ANTIGRAVITY_BRIDGE_RUNTIME_SECRET_FILE"]), self.state / "runtime-secret.txt")
        self.assertNotIn(first["runtime-secret.txt"].decode().strip(), json.dumps(config))

    def test_settings_are_untouched_without_configure_flag(self):
        before = self.settings.read_bytes()
        self.run_setup(write=True)
        self.assertEqual(before, self.settings.read_bytes())
        self.assertTrue((self.output / "agy-settings.patch.json").exists())

    def test_invalid_existing_state_is_not_overwritten(self):
        self.state.mkdir()
        path = self.state / "runtime-secret.txt"
        path.write_text("invalid")
        with self.assertRaises(ValueError):
            self.run_setup(write=True, configure_agy=True)
        self.assertEqual(path.read_text(), "invalid")
        self.assertFalse((self.state / "broker-secret.bin").exists())

    def test_private_state_cannot_be_written_inside_workspace(self):
        with self.assertRaises(ValueError):
            prepare(self.bridge, self.workspace, self.workspace / "secrets", self.settings, self.output, write=True)
        self.assertFalse((self.workspace / "secrets").exists())

    def test_malformed_settings_cause_no_partial_setup(self):
        self.settings.write_text('{"permissions":{"allow":"invalid"}}')
        with self.assertRaises(ValueError):
            self.run_setup(write=True, configure_agy=True)
        self.assertFalse(self.state.exists())

    def test_hook_paths_with_spaces_fail_before_writing(self):
        bad_bridge = self.root / "repo with spaces" / "adapters" / "antigravity"
        bad_bridge.mkdir(parents=True)
        (bad_bridge / "reviewed_exec.cmd").write_text("fixture")
        (bad_bridge / "launch_mcp.py").write_text("fixture")
        with self.assertRaises(ValueError):
            prepare(bad_bridge, self.workspace, self.state, self.settings, self.output, write=True)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
