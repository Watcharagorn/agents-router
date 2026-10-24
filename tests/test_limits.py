"""Admission control: caps, plan presets, usage ceiling, effort and model limits."""

import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import accounts, jobs, limits, plans, tools, usage_view
from agents_router.config import dump_config, load_config, save_config


class LimitTests(TempHome):
    def write(self, text: str) -> None:
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def codex_config(self, primary="", accounts_text="", top="", claude=""):
        self.write(
            f'{top}\n[codex]\nprimary = {json.dumps(str(self.root / "codex"))}\n{primary}\n{accounts_text}\n'
            f'[claude]\nprimary = {json.dumps(str(self.primary))}\n{claude}\n'
        )

    def active(self, runtime="codex", account="primary", state="running"):
        """A job record that counts as running: its runner is this process."""

        job_id = jobs.new_job_id() + f"-{len(list(jobs.jobs_dir().glob('*'))) if jobs.jobs_dir().exists() else 0}"
        folder = jobs.jobs_dir() / job_id
        folder.mkdir(parents=True)
        now = time.time()
        (folder / "status.json").write_text(
            json.dumps({"job_id": job_id, "runtime": runtime, "state": state, "account": account, "created_at": now, "updated_at": now}),
            encoding="utf-8",
        )
        (folder / "spec.json").write_text(json.dumps({"account": None}), encoding="utf-8")
        (folder / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")
        return job_id

    def admit(self, runtime="codex", **kwargs):
        return limits.admit(load_config(), runtime, **kwargs)

    def test_plus_cap_refuses_with_source_and_alternative(self):
        self.codex_config(primary='plan = "plus"')
        for _ in range(3):
            self.active()
        self.assertEqual(self.admit().account, "primary")
        self.active()
        with patch.object(limits, "_available_runtime", side_effect=lambda name: name == "claude"):
            with self.assertRaises(limits.LimitError) as raised:
                self.admit()
        message = str(raised.exception)
        self.assertEqual(raised.exception.outcome, "limit")
        self.assertIn("Codex is at its limit: 4 of 4 subagents running", message)
        self.assertIn("plan preset plus", message)
        self.assertIn("change with max_jobs", message)
        self.assertIn("Start this with runtime claude, or wait for one to finish.", message)

    def test_no_plan_means_no_new_cap(self):
        self.codex_config()
        for _ in range(6):
            self.active()
        self.assertEqual(self.admit().account, "primary")  # max_jobs_per_account only shapes the runner's queue

    def test_wait_hint_when_no_other_runtime_has_room(self):
        self.codex_config(primary='plan = "free"')
        self.active()
        with patch.object(limits, "_available_runtime", return_value=False):
            with self.assertRaises(limits.LimitError) as raised:
                self.admit()
        self.assertIn("Wait for one to finish, then start this again.", str(raised.exception))
        self.assertNotIn("Start this with runtime", str(raised.exception))

    def test_explicit_max_jobs_beats_the_preset(self):
        self.codex_config(primary='plan = "plus"\nprimary_max_jobs = 2')
        self.active()
        self.active()
        with self.assertRaises(limits.LimitError) as raised:
            self.admit()
        self.assertIn("2 of 2", str(raised.exception))
        self.assertIn("max_jobs = 2", str(raised.exception))

    def test_runtime_and_global_caps(self):
        self.codex_config(primary="max_jobs = 2", top="max_jobs = 3")
        self.active()
        self.active()
        with self.assertRaises(limits.LimitError) as raised:
            self.admit()
        self.assertIn("2 of 2 subagents running (max_jobs under [codex])", str(raised.exception))
        self.active(runtime="claude", account="primary")
        with self.assertRaises(limits.LimitError) as raised:
            self.admit("claude")
        self.assertIn("global limit: 3 of 3", str(raised.exception))

    def test_free_plan_lowers_effort_and_filters_models(self):
        self.codex_config(
            primary='include_primary = false\n',
            accounts_text='[[codex.accounts]]\nname = "free"\nhome = ' + json.dumps(str(self.root / "free"))
            + '\nplan = "free"\nfallback = true\nmodels = ["gpt-6-luna"]\n',
        )
        admission = self.admit(effort="xhigh", model="gpt-6-luna")
        self.assertEqual((admission.account, admission.effort), ("free", "high"))
        self.assertIn("effort lowered from xhigh to high", admission.warnings[0])
        self.assertEqual(self.admit(effort="medium", model="gpt-6-luna").warnings, [])
        with self.assertRaises(limits.LimitError) as raised:
            self.admit(model="gpt-6-sol")
        self.assertIn("No codex account allows model 'gpt-6-sol' (allowed: gpt-6-luna", str(raised.exception))
        self.active(account="free")
        with self.assertRaises(limits.LimitError) as raised:
            self.admit(model="gpt-6-luna")
        self.assertIn("1 of 1", str(raised.exception))

    def test_fallback_account_is_used_last(self):
        self.codex_config(
            primary='plan = "plus"\ninclude_primary = true',
            accounts_text='[[codex.accounts]]\nname = "free"\nhome = ' + json.dumps(str(self.root / "free"))
            + '\nplan = "free"\nfallback = true\n',
        )
        self.assertEqual(self.admit().account, "primary")
        for _ in range(4):
            self.active()
        self.assertEqual(self.admit().account, "free")
        self.active(account="free")
        with self.assertRaises(limits.LimitError) as raised:
            self.admit()
        self.assertIn("5 of 5", str(raised.exception))

    def test_pinned_account_cap(self):
        self.codex_config(primary='plan = "free"')
        self.active()
        with self.assertRaises(limits.LimitError) as raised:
            self.admit(account="primary")
        self.assertIn("Codex account 'primary' is at its limit", str(raised.exception))
        self.assertIn("Omit account", str(raised.exception))

    def test_usage_ceiling(self):
        self.codex_config(claude='usage_ceiling = 0.9')
        reset = time.time() + 3600
        accounts.record_rate_limit_event("claude", "primary", {"rateLimitType": "five_hour", "utilization": 0.95, "resetsAt": reset})
        with self.assertRaises(limits.LimitError) as raised:
            self.admit("claude")
        self.assertIn("95% of its usage window (usage_ceiling 90%)", str(raised.exception))
        self.assertIn("Room: Claude primary 5% left (5h, resets", str(raised.exception))
        # A window that has reset since the record was written does not block.
        accounts.record_rate_limit_event("claude", "primary", {"rateLimitType": "five_hour", "utilization": 0.99, "resetsAt": time.time() - 5})
        self.assertEqual(self.admit("claude").account, "primary")
        # Without usage data nothing is guessed.
        (self.home / "state" / "claude-usage.json").unlink()
        self.assertEqual(self.admit("claude").account, "primary")

    def test_cooldown_message(self):
        self.codex_config()
        accounts.set_cooldown("codex", "primary", time.time() + 7200, "limit")
        with self.assertRaises(limits.LimitError) as raised:
            self.admit()
        self.assertIn("Codex is at its usage limit until", str(raised.exception))

    def test_queue_mode_never_refuses_for_capacity(self):
        self.codex_config(primary='plan = "free"\nwhen_full = "queue"')
        self.active()
        self.assertIsNone(self.admit().account)

    def test_create_job_refuses_before_writing_a_record(self):
        self.codex_config(primary='plan = "free"')
        self.active()
        before = set(jobs.jobs_dir().iterdir())
        with self.assertRaises(limits.LimitError):
            jobs.create_job(runtime="codex", prompt="x", cwd=str(self.project))
        self.assertEqual(set(jobs.jobs_dir().iterdir()), before)
        with self.assertRaises(tools.ToolError) as raised:
            tools.call("subagent_start", {"runtime": "codex", "prompt": "x", "cwd": str(self.project)})
        self.assertIn("Codex is at its limit", str(raised.exception))

    def test_jobs_without_a_runner_and_dead_runners_do_not_count(self):
        self.codex_config(primary='plan = "free"')
        job = self.active()
        (jobs.jobs_dir() / job / "runner.pid").unlink()
        self.assertEqual(self.admit().account, "primary")
        (jobs.jobs_dir() / job / "runner.pid").write_text("99999999")
        status_path = jobs.jobs_dir() / job / "status.json"
        data = json.loads(status_path.read_text())
        data["updated_at"] = time.time() - 120
        status_path.write_text(json.dumps(data))
        self.assertEqual(self.admit().account, "primary")

    def test_effort_note_reaches_the_summary(self):
        self.codex_config(primary='plan = "free"')
        status = jobs.create_job(runtime="codex", prompt="x", cwd=str(self.project), effort="max", start=False)
        self.assertEqual(status["limit_notes"], [])  # start=False skips admission
        data = {"job_id": "j", "limit_notes": ["effort lowered from max to high (primary: plan preset free)"], "warnings": ["w"]}
        self.assertEqual(len(tools.summarize(data)["warnings"]), 2)

    def test_config_round_trip(self):
        self.codex_config(
            primary='plan = "plus"\nprimary_max_jobs = 3\nusage_ceiling = 0.9\nwhen_full = "queue"\nmax_jobs = 6',
            accounts_text='[[codex.accounts]]\nname = "free"\nhome = ' + json.dumps(str(self.root / "free"))
            + '\nplan = "free"\nfallback = true\nmodels = ["gpt-6-luna"]\nmax_effort = "medium"\n',
            top="max_jobs = 9",
        )
        save_config(load_config())
        again = load_config()
        codex = again.runtime("codex")
        self.assertEqual((again.max_jobs, codex.plan, codex.primary_max_jobs, codex.max_jobs), (9, "plus", 3, 6))
        self.assertEqual((codex.usage_ceiling, codex.when_full), (0.9, "queue"))
        free = codex.accounts[0]
        self.assertEqual((free.plan, free.models, free.max_effort, free.fallback), ("free", ["gpt-6-luna"], "medium", True))

    def test_accounts_report_shows_limits_and_sources(self):
        self.codex_config(primary='plan = "plus"')
        self.active()
        with patch.dict(os.environ, {"AGT_CODEX_COMMAND": json.dumps([sys.executable, "-c", "pass"])}):
            entry = tools.account_report("codex")["accounts"][0]
        self.assertEqual(entry["running_jobs"], 1)
        self.assertEqual(entry["limits"]["max_jobs"], 4)
        self.assertEqual(entry["limits"]["max_jobs_from"], "plan preset plus")
        self.assertEqual(entry["limits"]["plan"], "plus")

    def test_claude_plan_detection_reads_two_fields_and_only_applies_when_asked(self):
        (self.primary / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"userRateLimitTier": "default_claude_max_5x", "accessToken": "secret"}}), encoding="utf-8"
        )
        self.assertEqual(plans.detect_claude_plan(self.primary), "max-5x")
        self.codex_config()
        entry = tools.account_report("claude")["accounts"][0]
        self.assertIn("max-5x", entry["limits"]["suggested_plan"])
        self.assertNotIn("plan", entry["limits"])
        self.assertEqual(entry["limits"]["max_jobs_from"], "max_jobs_per_account")
        self.codex_config(claude='plan = "auto"')
        entry = tools.account_report("claude")["accounts"][0]
        self.assertEqual(entry["limits"]["max_jobs"], 4)
        self.assertIn("auto-detected", entry["limits"]["max_jobs_from"])


class UsageViewTests(TempHome):
    def rollout(self, home: Path, used: float, day="16"):
        folder = home / "sessions" / "2026" / "10" / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-10-{day}T01-19-57-thread{day}.jsonl"
        snapshot = {"primary": {"used_percent": used, "window_minutes": 300, "resets_at": time.time() + 3600}}
        path.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "private"}}) + "\n"
            + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": snapshot}}) + "\n",
            encoding="utf-8",
        )
        return path

    def test_codex_usage_is_read_from_the_newest_rollout_and_shown_in_one_line(self):
        codex_home = self.root / "codex"
        (self.home / "config.toml").write_text(f'[codex]\nprimary = {json.dumps(str(codex_home))}\n', encoding="utf-8")
        self.rollout(codex_home, 20.0, day="15")
        self.rollout(codex_home, 92.0, day="16")
        config = load_config()
        usage_view.refresh_codex(config)
        self.assertAlmostEqual(accounts.load_usage("codex")["primary"]["windows"]["five_hour"]["utilization"], 0.92)
        line = usage_view.room_line(config, ["codex"])
        self.assertTrue(line.startswith("Room: Codex primary 8% left (5h, resets "), line)
        self.assertNotIn("private", line)

    def test_accounts_list_shows_when_each_window_resets(self):
        codex_home = self.root / "codex"
        (self.home / "config.toml").write_text(f'[codex]\nprimary = {json.dumps(str(codex_home))}\n', encoding="utf-8")
        self.rollout(codex_home, 85.0)
        with patch.dict(os.environ, {"AGT_CODEX_COMMAND": json.dumps([sys.executable, "-c", "pass"])}):
            entry = tools.account_report("codex")["accounts"][0]
        self.assertEqual(entry["usage"], {"five_hour": 85})
        self.assertRegex(entry["usage_resets"]["five_hour"], r"^\d{4}-\d\d-\d\d \d\d:\d\d \(in (1 h 0 min|59 min)\)$")

    def test_reset_text_counts_down_and_marks_passed_windows(self):
        now = 1_000_000.0
        self.assertTrue(usage_view.reset_text(now + 4 * 3600 + 15 * 60, now).endswith("(in 4 h 15 min)"))
        self.assertTrue(usage_view.reset_text(now + 4 * 86400 + 18 * 3600, now).endswith("(in 4 d 18 h)"))
        self.assertTrue(usage_view.reset_text(now + 20 * 60, now).endswith("(in 20 min)"))
        self.assertIn("passed", usage_view.reset_text(now - 60, now))

    def test_stale_data_shows_its_age_and_unknown_accounts_are_left_out(self):
        (self.home / "config.toml").write_text(
            f'[claude]\nprimary = {json.dumps(str(self.primary))}\n[[claude.accounts]]\nname = "a2"\nhome = {json.dumps(str(self.root / "a2"))}\n'
            f'[[claude.accounts]]\nname = "spare"\nhome = {json.dumps(str(self.root / "spare"))}\nfallback = true\n',
            encoding="utf-8",
        )
        accounts.record_rate_limit_event("claude", "a2", {"rateLimitType": "five_hour", "utilization": 0.4, "resetsAt": time.time() + 600})
        data = accounts.load_usage("claude")
        data["a2"]["updated"] = time.time() - 7200
        (self.home / "state" / "claude-usage.json").write_text(json.dumps(data), encoding="utf-8")
        line = usage_view.room_line(load_config(), ["claude"])
        self.assertIn("a2 60% left", line)
        self.assertIn("as of 2 h ago", line)
        self.assertIn("spare fallback", line)
        self.assertNotIn("primary", line)

    def test_runtime_without_usage_says_so(self):
        (self.home / "config.toml").write_text(f'[claude]\nprimary = {json.dumps(str(self.primary))}\n', encoding="utf-8")
        self.assertEqual(usage_view.room_line(load_config(), ["claude"]), "Room: Claude 1 account (no usage data)")

    def test_start_result_carries_the_room_line(self):
        (self.home / "config.toml").write_text(f'[claude]\nprimary = {json.dumps(str(self.primary))}\n', encoding="utf-8")
        result = tools.call("subagent_start", {"runtime": "claude", "prompt": "x", "cwd": str(self.project)})
        self.assertIn("Room: Claude", result["room"])
        jobs.wait_for(result["job_id"], 60)


if __name__ == "__main__":
    unittest.main()
