from pathlib import Path
import json
import tempfile
import unittest

from review_gate import (
    _is_exact_request_write,
    action_digest,
    deterministic_decision,
    parse_hook_input,
)


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, name, args=None):
        return {"toolCall": {"name": name, "args": args or {}}}

    def test_rejects_oversized_input(self):
        with self.assertRaises(ValueError):
            parse_hook_input(b"x" * (256 * 1024 + 1))

    def test_digest_is_stable_across_key_order(self):
        left = {"name": "run_command", "args": {"b": 2, "a": 1}}
        right = {"args": {"a": 1, "b": 2}, "name": "run_command"}
        self.assertEqual(action_digest(left), action_digest(right))

    def test_allows_workspace_read(self):
        path = str(self.root / "README.md")
        self.assertEqual(
            deterministic_decision(self.payload("read_file", {"path": path}), self.root)[0],
            "allow",
        )

    def test_denies_outside_workspace_read(self):
        outside = str(self.root.parent / "secret.txt")
        self.assertEqual(
            deterministic_decision(self.payload("read_file", {"path": outside}), self.root)[0],
            "deny",
        )

    def test_denies_relative_traversal_write(self):
        result = deterministic_decision(
            self.payload("write_to_file", {"TargetFile": "..\\outside.txt", "CodeContent": "x"}),
            self.root,
        )
        self.assertEqual(result[0], "deny")

    def test_does_not_treat_file_content_as_a_path(self):
        target = str(self.root / "src" / "example.py")
        result = deterministic_decision(
            self.payload(
                "write_to_file",
                {"TargetFile": target, "CodeContent": "text mentioning C:\\outside\\file"},
            ),
            self.root,
        )
        self.assertEqual(result[0], "allow")

    def test_denies_agent_configuration_target(self):
        target = str(self.root / ".agents" / "hooks.json")
        self.assertEqual(
            deterministic_decision(self.payload("write_file", {"path": target}), self.root)[0],
            "deny",
        )

    def test_only_exact_leased_request_file_gets_write_exception(self):
        request = self.root / ".codex-agy-executor-test" / "command-requests" / (
            "a" * 32 + ".json"
        )
        request.parent.mkdir(parents=True)
        exact = self.payload("write_to_file", {"TargetFile": str(request)})["toolCall"]
        neighbor = self.payload(
            "write_to_file", {"TargetFile": str(request.with_name("other.json"))}
        )["toolCall"]
        self.assertTrue(
            _is_exact_request_write(
                exact, request_path=request, project_root=self.root
            )
        )
        self.assertFalse(
            _is_exact_request_write(
                neighbor, request_path=request, project_root=self.root
            )
        )

    def test_denies_resolved_symlink_alias_to_agent_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            protected = root / ".git"
            protected.mkdir()
            alias = root / "ordinary-looking-directory"
            try:
                alias.symlink_to(protected, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            target = alias / "config"
            result = deterministic_decision(
                self.payload("write_file", {"path": str(target)}), root
            )
            self.assertEqual(result[0], "deny")

    def test_command_requires_model_review(self):
        self.assertIsNone(
            deterministic_decision(
                self.payload("run_command", {"CommandLine": "npm test"}), self.root
            )
        )

    def test_denies_encoded_command(self):
        result = deterministic_decision(
            self.payload("run_command", {"CommandLine": "powershell -EncodedCommand AAA"}),
            self.root,
        )
        self.assertEqual(result[0], "deny")

    def test_denies_credential_store(self):
        target = r"C:\Users\Example\.ssh\id_ed25519"
        result = deterministic_decision(self.payload("read_file", {"path": target}), self.root)
        self.assertEqual(result[0], "deny")

    def test_allows_ordinary_workspace_write(self):
        target = str(self.root / "src" / "example.py")
        result = deterministic_decision(self.payload("write_file", {"path": target}), self.root)
        self.assertEqual(result[0], "allow")


if __name__ == "__main__":
    unittest.main()
