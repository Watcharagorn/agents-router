"""`agt run --role`: read-role fan-out, edit-role selection, and group ids in follow, cancel, message and jobs."""

import contextlib
import io
import json
import os
import re
import sys
import time
from unittest.mock import patch

from helpers import TempHome

from agents_router import accounts, cli, jobs, messages, profiles, tools, ui

FRAME_RE = re.compile(r" · job ([a-z0-9-]+) · token ([0-9a-f]{32})$")
GROUP_FOLLOW_RE = re.compile(r"^agt follow (g-[0-9a-f]+) --from (\d+)((?: --window \d+)?) # agt-token ([0-9a-f]{32})$", re.MULTILINE)


def result_body(text):
    lines = text.split("\n")
    start, end = lines.index("===== RESULT ====="), lines.index("===== END RESULT =====")
    assert all(line.startswith("| ") or line == "|" for line in lines[start + 1:end]), lines[start + 1:end]
    return "\n".join(line[2:] for line in lines[start + 1:end])


class RoleRunTests(TempHome):
    def setUp(self):
        super().setUp()
        for key in ("FAKE_CLAUDE_DELAY",):
            os.environ.pop(key, None)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_DELAY", None)

    def roles(self, text, extra=""):
        self.write_config(accounts=("a1", "a2"), extra=extra)
        for name in ("a1", "a2"):  # fake_claude's `auth status` reads a folder with this file as logged in
            (self.root / "homes" / name).mkdir(parents=True, exist_ok=True)
            (self.root / "homes" / name / "logged_in").touch()
        with open(self.home / "settings.toml", "a", encoding="utf-8") as handle:
            handle.write("\n" + text.strip() + "\n")

    def agt(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("sys.stdin", io.StringIO(stdin or "")):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_role(self, role, *extra, task="review the change"):
        return self.agt("run", "--follow", "--role", role, "--cwd", str(self.project), *extra, task)

    def frame(self, text):
        lines = text.splitlines()
        group, token = FRAME_RE.search(lines[0]).groups()
        self.assertTrue(group.startswith("g-"), lines[0])
        self.assertEqual(FRAME_RE.search(lines[-1]).groups(), (group, token), lines[-1])
        return group, token, lines

    def job_ids(self):
        folder = self.home / "jobs"
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []

    # S2: a read role fans out ---------------------------------------------------------

    def test_S2_read_role_one_section_per_provider(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]')
        code, text, err = self.run_role("reviewer")
        self.assertEqual(code, 0, text + err)
        group, token, lines = self.frame(text)
        self.assertRegex(lines[0], r"^\[agt\] Agents Router · role reviewer · read · 2 jobs \(group g-[0-9a-f]+\) · job ")
        self.assertTrue(any(line.startswith("[agt] role reviewer · read · 2 jobs in parallel") for line in lines))
        self.assertRegex(lines[-1], r"^\[agt\] DONE · 2 of 2 succeeded · \d+s · job g-")
        self.assertEqual(result_body(text), "## claude:a1\ndone on a1\n\n## claude:a2\ndone on a2")
        # Worker lines carry the member's number; each member ends with its own line.
        self.assertIn("| [1] says: done on a1", lines)
        self.assertIn("| [2] says: done on a2", lines)
        self.assertTrue(any(line.startswith("[agt] [1] DONE · claude:a1") for line in lines))
        # Two ordinary jobs, one per provider, each with the group id on its record.
        members = jobs.group_members(group)
        self.assertEqual(sorted(members), self.job_ids())
        self.assertEqual([jobs.read_status(job)["group"] for job in members], [group, group])
        self.assertEqual([jobs.read_spec(job).account for job in members], ["a1", "a2"])
        self.assertEqual({jobs.read_spec(job).access for job in members}, {"read"})
        # The follower delivered the results.
        self.assertFalse(any(jobs.is_pending(job) for job in members))

    def test_S2_partial_failure_reads_failed_and_still_done(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]')
        os.environ["FAKE_CLAUDE_LIMITED"] = "a2"
        code, text, err = self.run_role("reviewer")
        self.assertEqual(code, 0, text + err)
        _, _, lines = self.frame(text)
        body = result_body(text)
        self.assertTrue(body.startswith("## claude:a1\ndone on a1\n\n## claude:a2 · FAILED · "), body)
        self.assertRegex(lines[-1], r"^\[agt\] DONE · 1 of 2 succeeded · ")

    def test_S2_every_provider_failed_is_failed(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]')
        os.environ["FAKE_CLAUDE_SCENARIO"] = "auth"
        code, text, err = self.run_role("reviewer")
        self.assertEqual(code, 1, text + err)
        _, _, lines = self.frame(text)
        self.assertRegex(lines[-1], r"^\[agt\] FAILED · every reviewer job failed: \[1\] claude:a1 \S+, \[2\] claude:a2 ")

    def test_S2_mixed_runtimes_claude_and_antigravity(self):
        fake = os.path.join(os.path.dirname(__file__), "fake_agy.py")
        os.environ["AGT_AGY_COMMAND"] = json.dumps([sys.executable, fake])
        self.addCleanup(os.environ.pop, "AGT_AGY_COMMAND", None)
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "antigravity"]')
        code, text, err = self.run_role("reviewer")
        self.assertEqual(code, 0, text + err)
        _, _, lines = self.frame(text)
        self.assertIn("| [2] says: gemini done", lines)
        self.assertEqual(result_body(text), "## claude:a1\ndone on a1\n\n## antigravity\ngemini done")

    def test_S2a_conflict_returns_both_raw(self):
        self.roles('[roles.decision-maker]\nproviders = ["claude:a1/opus", "claude:a2:high"]')
        code, text, err = self.run_role("decision-maker", task="settle login.py:42")
        self.assertEqual(code, 0, text + err)
        self.assertEqual(result_body(text), "## claude:a1/opus\ndone on a1\n\n## claude:a2:high\ndone on a2")
        group = self.frame(text)[0]
        specs = [jobs.read_spec(job) for job in jobs.group_members(group)]
        self.assertEqual([(spec.model, spec.effort) for spec in specs], [("opus", None), (None, "high")])

    def test_S2b_cooling_provider_skipped(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]')
        until = time.time() + 3600
        accounts.update_usage("claude", "a2", cooldown_until=until)
        code, text, err = self.run_role("reviewer")
        self.assertEqual(code, 0, text + err)
        group, _, lines = self.frame(text)
        clock = time.strftime("%H:%M", time.localtime(until))
        self.assertTrue(any(line.startswith("[agt] role reviewer · read · 1 of 2 providers available") for line in lines))
        self.assertTrue(any(line.startswith(f"[agt] skipped claude:a2 — a2 at its limit until {clock} · job {group}")
                            for line in lines), lines)
        self.assertTrue(any('"rebalance my roles" (orchestrator-rebalance)' in line for line in lines))
        self.assertEqual(result_body(text), f"## claude:a1\ndone on a1\n\n## claude:a2 · SKIPPED · a2 at its limit until {clock}")
        self.assertEqual(len(self.job_ids()), 1)

    def test_S2_cap_skip_reason(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]', extra="max_jobs = 1")
        os.environ["FAKE_CLAUDE_DELAY"] = "20"
        code, out, err = self.agt("start", "--role", "reviewer", "--cwd", str(self.project), "review")
        self.assertEqual(code, 0, out + err)
        started = json.loads(out)
        self.assertEqual([item["provider"] for item in started["jobs"]], ["claude:a1"])
        self.assertEqual(started["skipped"], [{"provider": "claude:a2", "position": 2, "quota": False, "reason":
                                               "Claude is at its limit: 1 of 1 subagents running (max_jobs under [claude])."}])
        self.assertEqual(len(self.job_ids()), 1)
        code, out, err = self.agt("cancel", started["group"])
        self.assertEqual(code, 0, out + err)

    def test_S2c_cancel_group(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]')
        os.environ["FAKE_CLAUDE_DELAY"] = "20"
        code, out, err = self.agt("start", "--role", "reviewer", "--cwd", str(self.project), "review")
        self.assertEqual(code, 0, out + err)
        started = json.loads(out)
        group = started["group"]
        self.assertEqual([item["provider"] for item in started["jobs"]], ["claude:a1", "claude:a2"])
        members = jobs.group_members(group)
        code, out, err = self.agt("cancel", group)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.strip(), f"Cancelled 2 jobs in group {group} (role reviewer): "
                                      f"{members[0]} claude:a1, {members[1]} claude:a2")
        self.assertEqual([jobs.read_status(job)["outcome"] for job in members], ["cancelled", "cancelled"])
        # The relay following the group ends with FAILED naming the cancelled jobs.
        code, text, err = self.agt("follow", group)
        self.assertEqual(code, 1, text + err)
        _, _, lines = self.frame(text)
        self.assertEqual(lines[-1].split(" · job ")[0],
                         "[agt] FAILED · every reviewer job failed: [1] claude:a1 cancelled, [2] claude:a2 cancelled")
        # Each member's outcome reads "cancelled" once, on its line and in its section.
        self.assertNotIn("cancelled · cancelled", text)
        self.assertTrue(any(line.startswith("[agt] [1] FAILED · claude:a1 · cancelled · job ") for line in lines), lines)
        self.assertIn("## claude:a1 · FAILED · cancelled\n", text.replace("| ", ""))

    def test_S2d_question_names_one_job(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]')
        os.environ["FAKE_CLAUDE_DELAY"] = "3"
        code, out, err = self.agt("start", "--role", "reviewer", "--cwd", str(self.project), "review")
        self.assertEqual(code, 0, out + err)
        group = json.loads(out)["group"]
        asking = jobs.group_members(group)[1]
        question = messages.post_from_worker(asking, "Review the migrations too?", kind="question")
        code, text, err = self.agt("follow", group, "--window", "60")
        self.assertEqual(code, 0, text + err)
        _, token, lines = self.frame(text)
        marker = lines.index("===== QUESTION =====")
        self.assertEqual(lines[marker + 1: marker + 3], ["| Review the migrations too?", "===== END QUESTION ====="])
        self.assertIn(f"agt message {group} --reply-to {question['id']} - # agt-token {token}", lines)
        self.assertTrue(lines[-1].startswith(
            f"[agt] QUESTION from job {asking} ([2] claude:a2) (id {question['id']}) · job {group} · token {token}"))
        found = GROUP_FOLLOW_RE.search(text)
        self.assertEqual((found.group(1), found.group(3), found.group(4)), (group, " --window 60", token))
        # The group id routes an answer to the job that asked.
        code, out, err = self.agt("message", group, "--reply-to", question["id"], "-", stdin="Yes, those too.")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(messages.answers(asking)[question["id"]], "Yes, those too.")
        messages.take_for_worker(asking)
        code, text, err = self.agt("follow", group, "--from", found.group(2))
        self.assertEqual(code, 0, text + err)
        self.assertNotIn("===== QUESTION =====", text)
        self.assertRegex(text.splitlines()[-1], r"^\[agt\] DONE · 2 of 2 succeeded")
        # A follow-up without a question needs one job: the group has two.
        code, out, err = self.agt("message", group, "more please")
        self.assertEqual(code, 2)
        self.assertIn(f"group {group} has 2 jobs; send the message to one of them", err)

    def test_group_still_running_continues_at_its_cursor(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]')
        os.environ["FAKE_CLAUDE_DELAY"] = "2"
        code, text, err = self.agt("run", "--follow", "--max-wait", "0.3", "--role", "reviewer",
                                   "--cwd", str(self.project), "review")
        self.assertEqual(code, 0, text + err)
        group, token, lines = self.frame(text)
        self.assertRegex(lines[-1], r"^\[agt\] STILL RUNNING after \d+s · \[1\] \w+, \[2\] \w+ · job g-")
        found = GROUP_FOLLOW_RE.search(text)
        self.assertEqual((found.group(1), found.group(4)), (group, token))
        first_started = [line for line in lines if "started: Claude Code" in line]
        code, more, err = self.agt("follow", group, "--from", found.group(2))
        self.assertEqual(code, 0, more + err)
        again = [line for line in more.splitlines() if "started: Claude Code" in line]
        self.assertEqual(len(first_started) + len(again), 2)  # each worker's start printed once in all
        self.assertFalse(any(line.startswith("[agt] [1]") and "is job" in line for line in more.splitlines()))
        self.assertRegex(more.splitlines()[-1], r"^\[agt\] DONE · 2 of 2 succeeded")

    def test_jobs_lists_the_role_and_group(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]')
        code, text, _ = self.run_role("reviewer")
        group = self.frame(text)[0]
        with patch("agents_router.levels.find_workspace", return_value=None):
            code, out, err = self.agt("jobs", "--all")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count(f"role reviewer · {group}"), 2, out)

    # S3: an edit role picks one provider ------------------------------------------------

    def test_S3_edit_role_first_admitted(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1", "claude:a2"]')
        code, text, err = self.run_role("builder", task="fix the test")
        self.assertEqual(code, 0, text + err)
        group, _, lines = self.frame(text)
        self.assertTrue(any(line.startswith("[agt] role builder · edit · provider 1 of 2: claude:a1") for line in lines))
        self.assertEqual(result_body(text), "done on a1")
        self.assertRegex(lines[-1], r"^\[agt\] DONE · success · \d+s · 1 tool call · account a1 · job g-")
        self.assertEqual(len(self.job_ids()), 1)
        self.assertEqual(jobs.read_spec(jobs.group_members(group)[0]).access, "edit")

    def test_S3a_near_limit_moves_to_next(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1", "claude:a2"]')
        accounts.update_usage("claude", "a1", windows={"five_hour": {"utilization": 0.96, "resets_at": time.time() + 3600}})
        code, text, err = self.run_role("builder", task="fix the test")
        self.assertEqual(code, 0, text + err)
        _, _, lines = self.frame(text)
        self.assertTrue(any(line.startswith("[agt] role builder · skipped provider 1 claude:a1 — "
                                            "a1 4% left · above the 95% usage ceiling") for line in lines), lines)
        self.assertTrue(any(line.startswith("[agt] role builder · edit · provider 2 of 2: claude:a2") for line in lines))
        self.assertTrue(any('"rebalance my roles"' in line for line in lines))
        self.assertEqual(result_body(text), "done on a2")

    def test_S3b_all_exhausted_exit_1(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1", "claude:a2"]')
        first, second = time.time() + 1800, time.time() + 7200
        accounts.update_usage("claude", "a1", cooldown_until=second)
        accounts.update_usage("claude", "a2", cooldown_until=first)
        code, text, err = self.run_role("builder", task="fix the test")
        self.assertEqual(code, 1, text + err)
        _, _, lines = self.frame(text)
        clock = time.strftime("%H:%M", time.localtime(first))
        self.assertTrue(lines[-1].startswith(
            f"[agt] FAILED · every builder provider is at its usage limit; the earliest resets around {clock} ("), lines[-1])
        self.assertEqual(sum("role builder · skipped provider" in line for line in lines), 2)
        self.assertEqual(self.job_ids(), [])
        # Without --follow: the same reasons on stderr, exit 1.
        code, out, err = self.agt("run", "--role", "builder", "--cwd", str(self.project), "fix the test")
        self.assertEqual(code, 1)
        self.assertIn(f"FAILED · every builder provider is at its usage limit; the earliest resets around {clock}", err)

    def test_S3c_unknown_account_skipped(self):
        self.roles('[roles.builder]\nproviders = ["claude:gone", "claude:a2"]')
        code, text, err = self.run_role("builder", task="fix the test")
        self.assertEqual(code, 0, text + err)
        _, _, lines = self.frame(text)
        self.assertTrue(any(line.startswith("[agt] role builder · skipped provider 1 claude:gone — no claude account "
                                            "named gone (removed, renamed or logged out)") for line in lines), lines)
        self.assertEqual(result_body(text), "done on a2")

    def test_S3c_no_usable_provider_names_one_reason_each(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:gone", "claude:a2"]')
        accounts.update_usage("claude", "a2", cooldown_until=time.time() + 600)
        code, out, err = self.agt("run", "--role", "reviewer", "--cwd", str(self.project), "review")
        self.assertEqual(code, 1, out + err)
        self.assertIn("FAILED · no reviewer provider can run: claude:gone — no claude account named gone", err)
        self.assertIn("; claude:a2 — a2 at its limit until ", err)
        # agt start says the same, also with exit 1.
        code, out, err = self.agt("start", "--role", "reviewer", "--cwd", str(self.project), "review")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("FAILED · no reviewer provider can run: claude:gone", err)
        self.assertEqual(self.job_ids(), [])

    def test_S3c_logged_out_account_skipped(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1", "claude:a2"]')
        (self.root / "homes" / "a1" / "logged_in").unlink()
        code, text, err = self.run_role("builder", task="fix the test")
        self.assertEqual(code, 0, text + err)
        _, _, lines = self.frame(text)
        self.assertTrue(any(line.startswith("[agt] role builder · skipped provider 1 claude:a1 — a1 is logged out · "
                                            "log in with agt accounts login a1") for line in lines), lines)
        self.assertTrue(any(line.startswith("[agt] role builder · edit · provider 2 of 2: claude:a2") for line in lines))
        self.assertFalse(any('"rebalance my roles"' in line for line in lines))  # a login, not quota
        self.assertEqual(result_body(text), "done on a2")
        self.assertEqual(len(self.job_ids()), 1)

    def test_S3c_roles_show_marks_a_logged_out_account(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1", "claude:a2"]')
        (self.root / "homes" / "a1" / "logged_in").unlink()
        code, out, err = self.agt("roles", "show", "--json")
        self.assertEqual(code, 0, out + err)
        builder = next(row for row in json.loads(out)["roles"] if row["name"] == "builder")
        usable = {p["provider"]: (p["usable"], p["reason"]) for p in builder["providers"]}
        self.assertEqual(usable["claude:a2"], (True, None))
        self.assertFalse(usable["claude:a1"][0])
        self.assertIn("a1 is logged out · log in with agt accounts login a1", usable["claude:a1"][1])

    def test_S3c_logged_out_read_role_runs_the_others(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1", "claude:a2"]')
        (self.root / "homes" / "a2" / "logged_in").unlink()
        code, text, err = self.run_role("reviewer")
        self.assertEqual(code, 0, text + err)
        self.assertEqual(result_body(text), "## claude:a1\ndone on a1\n\n"
                                            "## claude:a2 · SKIPPED · a2 is logged out · log in with agt accounts login a2")
        self.assertEqual(len(self.job_ids()), 1)
        # None logged in: exit 1, one reason per provider, nothing started.
        (self.root / "homes" / "a1" / "logged_in").unlink()
        profiles.forget(self.root / "homes" / "a1")  # a real logout rewrites .claude.json, which ends the cached check
        code, out, err = self.agt("run", "--role", "reviewer", "--cwd", str(self.project), "review")
        self.assertEqual(code, 1, out + err)
        self.assertIn("FAILED · no reviewer provider can run: claude:a1 — a1 is logged out · log in with agt accounts "
                      "login a1; claude:a2 — a2 is logged out · log in with agt accounts login a2", err)
        self.assertEqual(len(self.job_ids()), 1)

    def test_S3b_ceiling_and_cooling_together_are_at_the_usage_limit(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1", "claude:a2"]')
        resets, cooling = time.time() + 5400, time.time() + 1800
        accounts.update_usage("claude", "a1", windows={"five_hour": {"utilization": 0.97, "resets_at": resets}})
        accounts.update_usage("claude", "a2", cooldown_until=cooling)
        code, text, err = self.run_role("builder", task="fix the test")
        self.assertEqual(code, 1, text + err)
        clock = lambda when: time.strftime("%H:%M", time.localtime(when))
        self.assertEqual(
            self.frame(text)[2][-1].split(" · job g-")[0],
            f"[agt] FAILED · every builder provider is at its usage limit; the earliest resets around {clock(cooling)} "
            f"(claude:a1 — a1 3% left · above the 95% usage ceiling; claude:a2 — a2 at its limit until {clock(cooling)})")

    def test_short_provider_uses_any_account_with_room(self):
        self.roles('[roles.builder]\nproviders = ["claude"]')
        accounts.update_usage("claude", "a1", cooldown_until=time.time() + 3600)
        code, text, err = self.run_role("builder", task="fix it")
        self.assertEqual(code, 0, text + err)
        job = jobs.group_members(self.frame(text)[0])[0]
        self.assertIsNone(jobs.read_spec(job).account)  # agt's usual choice, not a pin
        self.assertEqual(jobs.read_status(job)["account"], "a2")
        self.assertEqual(result_body(text), "done on a2")

    def test_custom_role_prompt_file_goes_before_the_task(self):
        prompt = self.root / "security.md"
        prompt.write_text("Look for injection bugs.\n", encoding="utf-8")
        self.roles(f'[roles.security]\naccess = "read"\nprompt_file = {json.dumps(str(prompt))}\nproviders = ["claude:a1"]')
        code, text, err = self.run_role("security", task="check the login form")
        self.assertEqual(code, 0, text + err)
        spec = jobs.read_spec(jobs.group_members(self.frame(text)[0])[0])
        self.assertEqual(spec.prompt, "Look for injection bugs.\n\ncheck the login form")

    # Role rules from the calling CLI's own agt-<role> file (S2, S6, S13) ---------------

    def caller_file(self, folder, host="claude", rules="Never edit migrations.\n", name="builder"):
        from agents_router import role_agents
        from agents_router.config import load_config

        path = folder / f"agt-{name}{role_agents.SUFFIX[host]}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(role_agents.render(load_config().roles[name], host, rules=rules))
        return path

    def build(self, *extra, task="fix the test"):
        code, text, err = self.agt("run", "--follow", "--role", "builder", "--cwd", str(self.project), *extra, task)
        self.assertEqual(code, 0, text + err)
        return jobs.read_spec(jobs.group_members(self.frame(text)[0])[0]), text

    def test_S2_rules_go_before_the_task_after_the_prompt_file(self):
        prompt = self.root / "builder.md"
        prompt.write_text("Work in small steps.\n", encoding="utf-8")
        self.roles(f'[roles.builder]\nprompt_file = {json.dumps(str(prompt))}\nproviders = ["claude:a1"]')
        path = self.caller_file(self.primary / "agents", rules="Run the tests before you finish.\nNever edit migrations.\n")
        spec, text = self.build("--caller", "claude")
        self.assertEqual(spec.prompt, f"Work in small steps.\n\nRole rules for the builder (from {ui.tilde(str(path))}):\n"
                                      "Run the tests before you finish.\nNever edit migrations.\n\nfix the test")
        self.assertEqual(spec.rules, f"Role rules for the builder (from {ui.tilde(str(path))}):\n"
                                     "Run the tests before you finish.\nNever edit migrations.")
        self.assertIn("role rules: 56 characters from", text)
        group = jobs.read_group(self.frame(text)[0])
        self.assertEqual(group["rules"]["chars"], 56)

    def test_S11_a_fresh_session_follow_up_gets_the_rules_snapshot_first(self):
        import shutil

        self.roles('[roles.builder]\nproviders = ["claude"]')  # any account: the follow-up may land on another
        self.caller_file(self.primary / "agents", rules="Never edit migrations.\n")
        spec, _ = self.build("--caller", "claude")
        self.caller_file(self.primary / "agents", rules="An edit made later.\n")  # edits apply to new calls only
        owner = jobs.read_status(spec.job_id)["account"]
        accounts.set_cooldown("claude", owner, time.time() + 3600, "test")
        shutil.rmtree(self.root / "homes" / owner / "projects", ignore_errors=True)  # the transcript can't move
        result = tools.call("subagent_message", {"job_id": spec.job_id, "text": "and the docs"})
        follow_up = result["new_job"]["job_id"]
        self.assertEqual(jobs.read_spec(follow_up).rules, spec.rules)
        jobs.wait_for(follow_up, 60)
        status = jobs.read_status(follow_up)
        self.assertIn("could not move the session", " ".join(status.get("warnings") or []), status)
        sent = self.records()[-1]["stdin"]
        self.assertTrue(sent.startswith(spec.rules + "\n\n"), sent)
        self.assertIn("and the docs", sent)
        self.assertNotIn("An edit made later.", sent)

    def test_S12_long_rules_skip_antigravity_for_the_next_provider(self):
        gemini = self.root / "gemini"
        self.roles('[roles.builder]\nproviders = ["antigravity", "claude:a1"]\n\n[roles.reviewer]\n'
                   'providers = ["antigravity", "claude:a1"]\n\n[antigravity]\nprimary = '
                   + json.dumps(str(gemini / "antigravity-cli")))
        long = "Never edit migrations.\n" * 1100  # about 25,000 characters
        self.caller_file(self.primary / "agents", rules=long)
        spec, text = self.build("--caller", "claude")
        self.assertEqual(spec.runtime, "claude")
        self.assertIn(long.rstrip(), spec.prompt)  # never shortened
        self.assertIn("skipped provider 1 antigravity — the prompt with agt's notes is up to", text)
        self.assertIn("Antigravity takes at most 24000", text)
        self.caller_file(self.primary / "agents", rules=long, name="reviewer")
        code, text, err = self.run_role("reviewer", "--caller", "claude")
        self.assertEqual(code, 0, text + err)
        members = jobs.group_members(self.frame(text)[0])
        self.assertEqual([jobs.read_spec(job).runtime for job in members], ["claude"])
        self.assertIn("skipped antigravity — the prompt with agt's notes is up to", text)

    def test_S6_two_claude_profiles_send_their_own_rules(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1"]')
        self.caller_file(self.primary / "agents", rules="Rules of the primary.\n")
        other = self.root / "claude-ntb"
        self.caller_file(other / "agents", rules="Rules of claude-ntb.\n")
        spec, _ = self.build("--caller", "claude")
        self.assertIn("Rules of the primary.", spec.prompt)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other)}):
            spec, _ = self.build("--caller", "claude")
        self.assertIn("Rules of claude-ntb.", spec.prompt)
        self.assertNotIn("Rules of the primary.", spec.prompt)

    def test_S13_codex_reads_codex_home_not_the_worker_home(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1"]\n\n[codex]\nprimary = ' + json.dumps(str(self.root / "codex-w")))
        self.caller_file(self.root / "codex-w" / "agents", "codex", rules="From the worker home.\n")
        path = self.caller_file(self.root / "codex-home" / "agents", "codex", rules="From CODEX_HOME.\n")
        spec, _ = self.build("--caller", "codex")
        self.assertIn(f"(from {ui.tilde(str(path))}):\nFrom CODEX_HOME.", spec.prompt)
        self.assertNotIn("From the worker home.", spec.prompt)

    def test_S13_antigravity_reads_its_relay_plugin(self):
        gemini = self.root / "gemini"
        self.roles('[roles.builder]\nproviders = ["claude:a1"]\n\n[antigravity]\nprimary = '
                   + json.dumps(str(gemini / "antigravity-cli")))
        path = self.caller_file(gemini / "config" / "plugins" / "agents-router-relays" / "agents", "antigravity",
                                rules="From Antigravity.\n")
        spec, _ = self.build("--caller", "antigravity")
        self.assertIn(f"(from {ui.tilde(str(path))}):\nFrom Antigravity.", spec.prompt)

    def test_terminal_kiro_and_nested_workers_send_none_and_say_why(self):
        from agents_router import role_agents
        from agents_router.config import load_config

        self.roles('[roles.builder]\nproviders = ["claude:a1"]')
        self.caller_file(self.primary / "agents")
        spec, text = self.build()  # no --caller: a terminal, Kiro or another tool
        self.assertEqual(spec.prompt, "fix the test")
        self.assertIn("role rules: none (no calling agent", text)
        nested = role_agents.caller_rules("builder", "claude", load_config(), env={**os.environ, "AGT_DEPTH": "1"})
        self.assertEqual((nested.text, nested.source), ("", None))
        self.assertIn("a worker started by agt", nested.note)

    def test_a_file_without_the_management_line_still_sends_its_rules(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1"]')
        path = self.caller_file(self.primary / "agents", rules="Mine entirely.\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(line for line in lines if "Managed by agt roles" not in line) + "\n", encoding="utf-8")
        spec, _ = self.build("--caller", "claude")
        self.assertIn("Mine entirely.", spec.prompt)

    def test_a_linked_file_or_folder_exits_2_before_anything_starts(self):
        self.roles('[roles.builder]\nproviders = ["claude:a1"]')
        real = self.caller_file(self.root / "elsewhere" / "agents")
        (self.primary / "agents").mkdir()
        (self.primary / "agents" / "agt-builder.md").symlink_to(real)
        code, out, err = self.agt("run", "--follow", "--role", "builder", "--caller", "claude", "--cwd", str(self.project), "x")
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("is a link", " ".join(err.split()))
        (self.primary / "agents" / "agt-builder.md").unlink()
        (self.primary / "agents").rmdir()
        (self.primary / "agents").symlink_to(real.parent)
        code, out, err = self.agt("run", "--follow", "--role", "builder", "--caller", "claude", "--cwd", str(self.project), "x")
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("is a link", " ".join(err.split()))
        self.assertEqual(self.job_ids(), [])

    def test_S8_a_broken_file_exits_2_naming_the_line(self):
        from agents_router import role_agents

        self.roles('[roles.builder]\nproviders = ["claude:a1"]')
        path = self.caller_file(self.primary / "agents")
        text = path.read_text(encoding="utf-8")
        line = text.splitlines().index(role_agents.RULES_START) + 1
        path.write_text(text.replace(role_agents.RULES_END + "\n", ""), encoding="utf-8")
        code, out, err = self.agt("run", "--follow", "--role", "builder", "--caller", "claude", "--cwd", str(self.project), "x")
        self.assertEqual((code, out), (2, ""), err)  # no frame started, so none is left half-open
        self.assertIn(f"{path}:{line}:", " ".join(err.split()))
        code, out, err = self.agt("run", "--json", "--role", "builder", "--caller", "claude", "--cwd", str(self.project), "x")
        self.assertEqual(code, 2)
        report = json.loads(err)
        self.assertIn(f"{path}:{line}:", report["error"])
        self.assertEqual(self.job_ids(), [])

    # Refusals (exit 2, nothing starts) ---------------------------------------------------

    def test_S8a_role_off_exit_2(self):
        self.roles('[roles.builder]\nenabled = false\nproviders = ["claude:a1"]')
        code, out, err = self.agt("run", "--follow", "--role", "builder", "--cwd", str(self.project), "Fix the test")
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"role builder is off (enabled = false in {self.home / 'settings.toml'}); "
                      "`agt roles config --enable builder` turns it back on.", " ".join(err.split()))
        self.assertEqual(out, "")
        self.assertEqual(self.job_ids(), [])

    def test_role_with_runtime_or_account_exit_2(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1"]')
        for flags in (("--runtime", "codex"), ("--account", "a1"), ("--model", "opus")):
            code, out, err = self.agt("run", "--role", "reviewer", *flags, "--cwd", str(self.project), "x")
            self.assertEqual(code, 2, out + err)
            self.assertIn(f"leave out {flags[0]}", err)
        self.assertEqual(self.job_ids(), [])

    def test_unknown_role_exit_2_lists_roles(self):
        self.roles('[roles.reviewer]\nproviders = ["claude:a1"]')
        code, out, err = self.agt("run", "--role", "nope", "--cwd", str(self.project), "x")
        self.assertEqual(code, 2, out + err)
        self.assertIn("there's no role nope. Roles: reviewer", " ".join(err.split()))
