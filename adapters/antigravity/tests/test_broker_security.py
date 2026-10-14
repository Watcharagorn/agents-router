import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import broker_security
from broker_security import (
    BrokerTokenLease,
    consume_approval,
    issue_approval,
    parse_request,
    read_request_bytes,
    request_digest,
)


class BrokerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.requests = self.root / "requests"
        self.approvals = self.root / "approvals"
        self.requests.mkdir()
        self.secret = self.root / "secret.bin"
        self.secret.write_bytes(b"s" * 32)
        self.approval_id = "a" * 32
        self.runtime_patch = patch.dict(
            "os.environ", {"ANTIGRAVITY_BRIDGE_RUNTIME_SECRET": "b" * 64}
        )
        self.runtime_patch.start()

    def tearDown(self):
        self.runtime_patch.stop()
        self.temp.cleanup()

    def request(self, command="Write-Output OK"):
        path = self.requests / "request.json"
        path.write_text(
            json.dumps(
                {
                    "cwd": str(self.root),
                    "command": command,
                    "timeout_seconds": 30,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_one_use_approval_round_trip(self):
        path = self.request()
        raw = read_request_bytes(path)
        parsed = parse_request(raw, project_root=self.root, request_dir=self.requests)
        issue_approval(
            approval_id=self.approval_id,
            raw_request=raw,
            parsed_request=parsed,
            secret_path=self.secret,
            approval_dir=self.approvals,
        )
        record = consume_approval(
            approval_id=self.approval_id,
            secret_path=self.secret,
            approval_dir=self.approvals,
        )
        self.assertEqual(record["project_root"], str(self.root))
        with self.assertRaises(FileNotFoundError):
            consume_approval(
                approval_id=self.approval_id,
                secret_path=self.secret,
                approval_dir=self.approvals,
            )

    def test_modified_request_has_no_approval(self):
        path = self.request()
        raw = read_request_bytes(path)
        parsed = parse_request(raw, project_root=self.root, request_dir=self.requests)
        issue_approval(
            approval_id=self.approval_id,
            raw_request=raw,
            parsed_request=parsed,
            secret_path=self.secret,
            approval_dir=self.approvals,
        )
        modified = raw.replace(b"OK", b"NOT_OK")
        record = consume_approval(
            approval_id=self.approval_id,
            secret_path=self.secret,
            approval_dir=self.approvals,
        )
        self.assertNotEqual(record["request_sha256"], request_digest(modified))

    def test_wrong_runtime_cannot_consume_approval(self):
        path = self.request()
        raw = read_request_bytes(path)
        parsed = parse_request(raw, project_root=self.root, request_dir=self.requests)
        issue_approval(
            approval_id=self.approval_id,
            raw_request=raw,
            parsed_request=parsed,
            secret_path=self.secret,
            approval_dir=self.approvals,
        )
        with patch.dict(
            "os.environ", {"ANTIGRAVITY_BRIDGE_RUNTIME_SECRET": "c" * 64}
        ):
            with self.assertRaises(ValueError):
                consume_approval(
                    approval_id=self.approval_id,
                    secret_path=self.secret,
                    approval_dir=self.approvals,
                )
        record = consume_approval(
            approval_id=self.approval_id,
            secret_path=self.secret,
            approval_dir=self.approvals,
        )
        self.assertEqual(record["approval_id"], self.approval_id)

    def test_rejects_cwd_outside_project(self):
        path = self.request()
        value = json.loads(path.read_text())
        value["cwd"] = str(self.root.parent)
        raw = json.dumps(value).encode()
        with self.assertRaises(ValueError):
            parse_request(raw, project_root=self.root, request_dir=self.requests)

    def test_rejects_extra_request_fields(self):
        path = self.request()
        value = json.loads(path.read_text())
        value["extra"] = True
        with self.assertRaises(ValueError):
            parse_request(
                json.dumps(value).encode(),
                project_root=self.root,
                request_dir=self.requests,
            )

    def test_token_lease_is_exclusive_across_processes(self):
        local_app_data = self.root / "local"
        state = local_app_data / "Codex" / "antigravity-bridge"
        state.mkdir(parents=True)
        token = "d" * 32
        (state / "broker-tokens.json").write_text(
            json.dumps([token]), encoding="utf-8"
        )
        child_code = (
            "import sys\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            "from broker_security import BrokerTokenLease\n"
            "with BrokerTokenLease(timeout_seconds=2):\n"
            " print('LOCKED', flush=True)\n"
            " sys.stdin.read(1)\n"
        )
        env = dict(os.environ)
        env["LOCALAPPDATA"] = str(local_app_data)
        process = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "LOCKED")
            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app_data)}):
                broker_security._AVAILABLE_TOKENS = None
                with self.assertRaises(TimeoutError):
                    with BrokerTokenLease(timeout_seconds=0.2):
                        pass
        finally:
            process.stdin.write("x")
            process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app_data)}):
            broker_security._AVAILABLE_TOKENS = None
            with BrokerTokenLease(timeout_seconds=1) as leased:
                self.assertEqual(leased, token)


if __name__ == "__main__":
    unittest.main()
