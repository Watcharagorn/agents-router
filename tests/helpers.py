import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")


class TempHome(unittest.TestCase):
    """Isolated CLI_SUBAGENTS_HOME, primary setup and fake claude binary."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.home = self.root / "csa"
        self.primary = self.root / "primary"
        self.project = self.root / "project"
        for folder in (self.home, self.primary, self.project):
            folder.mkdir()
        self.record = self.root / "record.jsonl"
        self._env = patch.dict(
            os.environ,
            {
                "CLI_SUBAGENTS_HOME": str(self.home),
                "CLI_SUBAGENTS_CLAUDE_COMMAND": json.dumps([sys.executable, str(FAKE_CLAUDE)]),
                "FAKE_CLAUDE_RECORD": str(self.record),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
        self._env.start()
        for key in ("CLI_SUBAGENTS_DEPTH", "FAKE_CLAUDE_LIMITED", "FAKE_CLAUDE_SCENARIO"):
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()
        self._temp.cleanup()

    def write_config(self, accounts=("a1", "a2"), failover="on-limit", extra=""):
        lines = [
            "[claude]",
            f"primary = {json.dumps(str(self.primary))}",
            f'failover = "{failover}"',
            extra,
        ]
        for name in accounts:
            lines += ["[[claude.accounts]]", f'name = "{name}"', f"home = {json.dumps(str(self.root / 'homes' / name))}"]
        (self.home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def records(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines() if line.strip()]
