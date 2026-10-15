import json
import os
import sys
import unittest
from unittest.mock import patch

from helpers import TempHome

from cli_subagents.config import load_config
from cli_subagents.runtimes import codex
from cli_subagents.runtimes.base import JobSpec
from cli_subagents.runtimes.codex import CodexRuntime

OVERRIDE = 'windows.sandbox="unelevated"'


class WindowsSandboxTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["CLI_SUBAGENTS_CODEX_COMMAND"] = json.dumps([sys.executable, "fake"])
        self.windows = patch.object(codex, "_is_windows", return_value=True)
        self.windows.start()

    def tearDown(self):
        self.windows.stop()
        os.environ.pop("CLI_SUBAGENTS_CODEX_COMMAND", None)
        super().tearDown()

    def argv(self, access="read", resume=None, config_text="", extra=""):
        (self.home / "config.toml").write_text(f"[codex]\nprimary = {json.dumps(str(self.primary))}\n{extra}\n", encoding="utf-8")
        (self.primary / "config.toml").write_text(config_text, encoding="utf-8")
        config = load_config().runtime("codex")
        spec = JobSpec(job_id="t", runtime="codex", prompt="p", cwd=str(self.project), access=access, resume_session=resume)
        return CodexRuntime().launch(config, config.primary_account(), spec, "p", {}).argv

    def test_read_and_edit_get_the_override(self):
        for access in ("read", "edit"):
            argv = self.argv(access)
            self.assertIn(OVERRIDE, argv)
            self.assertEqual(argv[argv.index(OVERRIDE) - 1], "-c")

    def test_resume_gets_the_override(self):
        for access in ("read", "edit"):
            self.assertIn(OVERRIDE, self.argv(access, resume="thread-1"))

    def test_full_gets_nothing(self):
        self.assertFalse([item for item in self.argv("full") if "windows.sandbox" in item])

    def test_not_on_other_systems(self):
        with patch.object(codex, "_is_windows", return_value=False):
            self.assertNotIn(OVERRIDE, self.argv("read"))

    def test_user_setting_wins(self):
        for access in ("read", "edit"):
            argv = self.argv(access, config_text='[windows]\nsandbox = "elevated"\n')
            self.assertFalse([item for item in argv if "windows.sandbox" in item])

    def test_off_adds_nothing(self):
        self.assertFalse([item for item in self.argv("read", extra='windows_sandbox = "off"') if "windows.sandbox" in item])

    def test_elevated_passes_through(self):
        self.assertIn('windows.sandbox="elevated"', self.argv("edit", extra='windows_sandbox = "elevated"'))

    def test_bad_value_is_refused(self):
        (self.home / "config.toml").write_text('[codex]\nwindows_sandbox = "maybe"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config()

    def test_doctor_summary(self):
        (self.home / "config.toml").write_text(f"[codex]\nprimary = {json.dumps(str(self.primary))}\n", encoding="utf-8")
        config = load_config().runtime("codex")
        self.assertIn('"unelevated"', codex.windows_sandbox_summary(config))
        (self.primary / "config.toml").write_text('[windows]\nsandbox = "elevated"\n', encoding="utf-8")
        self.assertIn('"elevated"', codex.windows_sandbox_summary(config))


if __name__ == "__main__":
    unittest.main()
