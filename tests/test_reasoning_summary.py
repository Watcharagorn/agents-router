import json
import os
import sys
from unittest.mock import patch

from helpers import TempHome

from agents_router.config import load_config, save_config
from agents_router.runtimes.base import JobSpec
from agents_router.runtimes.codex import CodexRuntime


class ReasoningSummaryTests(TempHome):
    def argv(self, resume=None, setting=None, codex_config=""):
        text = f"[codex]\nprimary = {json.dumps(str(self.primary))}\n"
        if setting is not None:
            text += f"reasoning_summary = {json.dumps(setting)}\n"
        (self.home / "config.toml").write_text(text, encoding="utf-8")
        (self.primary / "config.toml").write_text(codex_config, encoding="utf-8")
        config = load_config().runtime("codex")
        spec = JobSpec(job_id="t", runtime="codex", prompt="p", cwd=str(self.project), resume_session=resume)
        with patch.dict(os.environ, {"AGT_CODEX_COMMAND": json.dumps([sys.executable, "fake"])}):
            return CodexRuntime().launch(config, config.primary_account(), spec, "p", {}).argv

    def test_default_on_new_runs_and_resumes(self):
        for resume in (None, "thread-1"):
            with self.subTest(resume=resume):
                argv = self.argv(resume=resume)
                override = 'model_reasoning_summary="detailed"'
                self.assertIn(override, argv)
                self.assertEqual(argv[argv.index(override) - 1], "-c")

    def test_user_setting_is_preserved_on_runs_and_resumes(self):
        for resume in (None, "thread-1"):
            for value in ("auto", "concise", "none", "detailed"):
                with self.subTest(resume=resume, value=value):
                    argv = self.argv(resume=resume, codex_config=f'model_reasoning_summary="{value}"\n')
                    self.assertFalse(any("model_reasoning_summary=" in arg for arg in argv))

    def test_project_setting_and_active_profile_are_preserved(self):
        folder = self.project / ".codex"
        folder.mkdir()
        project_config = folder / "config.toml"
        project_config.write_text('model_reasoning_summary="concise"\n', encoding="utf-8")
        self.assertFalse(any("model_reasoning_summary=" in arg for arg in self.argv()))
        project_config.unlink()
        argv = self.argv(codex_config='profile="quiet"\n[profiles.quiet]\nmodel_reasoning_summary="none"\n')
        self.assertFalse(any("model_reasoning_summary=" in arg for arg in argv))
        argv = self.argv(codex_config='[profiles.unused]\nmodel_reasoning_summary="none"\n')
        self.assertIn('model_reasoning_summary="detailed"', argv)

    def test_off_adds_nothing_and_other_values_pass_through(self):
        for resume in (None, "thread-1"):
            self.assertFalse(any("model_reasoning_summary=" in arg for arg in self.argv(resume, "off")))
            for value in ("concise", "auto", "none"):
                self.assertIn(f'model_reasoning_summary="{value}"', self.argv(resume, value))

    def test_values_survive_saving(self):
        for value in ("detailed", "concise", "auto", "none", "off"):
            self.argv(setting=value)
            save_config(load_config())
            self.assertEqual(load_config().runtime("codex").reasoning_summary, value)

    def test_invalid_values_and_other_runtimes_are_refused(self):
        for text in ('[codex]\nreasoning_summary="verbose"\n',
                     '[claude]\nreasoning_summary="detailed"\n'):
            (self.home / "config.toml").write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config()
