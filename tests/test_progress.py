"""Slow work while it runs: the display that redraws in place, and `agt run` / `agt wait` for a person.

Agents, pipes and tests never get the display, and run and wait print today's JSON for them.
"""

import contextlib
import io
import json
import os
import re
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import cli, jobs, jobs_view, messages, ui


class Terminal(io.StringIO):
    def isatty(self):
        return True


class LiveTests(unittest.TestCase):
    def test_only_a_person_at_a_terminal_gets_it(self):
        with patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            self.assertTrue(ui.live_on(Terminal()))
            self.assertFalse(ui.live_on(io.StringIO()))  # a pipe
        for env in ({"TERM": "dumb"}, {"TERM": "xterm", "CLAUDECODE": "1"}, {"TERM": "xterm", "CODEX_THREAD_ID": "x"},
                    {"TERM": "xterm", "AGT_JOB_ID": "x"}):
            with patch.dict(os.environ, env, clear=True):
                self.assertFalse(ui.live_on(Terminal()), env)
        with patch.dict(os.environ, {"TERM": "xterm", "AGT_COLOR": "never"}, clear=True):
            self.assertTrue(ui.live_on(Terminal()))  # --color only decides its colors

    def test_it_draws_nothing_into_a_pipe(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), ui.Steps("installing") as display:
            display.start("plugins/deck@tools", "codex")
            display.finish(True)
            self.assertFalse(display.drawing)
        self.assertEqual(err.getvalue(), "")

    def test_steps_show_each_finished_one_and_the_running_one(self):
        display = ui.Steps([ui.bold("agt share kinds add plugins"), "  ", ui.muted("installing 2 plugins")])
        display.start("plugins/deck@tools", ui.muted("claude-lab"))
        display.finish(True)
        display.start("plugins/formatter@tools", ui.muted("codex"))
        display.now("codex plugin add")
        lines = [ui.plain(line) for line in display.draw("⠼", display.running[2] + 12)]
        self.assertEqual(lines[:3], ["agt share kinds add plugins  installing 2 plugins", "",
                                     "  + plugins/deck@tools        claude-lab   0s"])
        self.assertEqual(lines[3], "  ⠼ plugins/formatter@tools   codex        running `codex plugin add`  0:12")

    def test_times(self):
        self.assertEqual([ui.clock(12), ui.clock(72), ui.clock(3723)], ["0:12", "1:12", "1:02:03"])
        self.assertEqual([ui.took(4), ui.took(72), ui.took(3725)], ["4s", "1m 12s", "1h 02m"])


JOB = "20261027-175223-e7b8a5"
DONE = {"job_id": JOB, "runtime": "claude", "state": "done", "outcome": "success", "account": "a1",
        "progress": {"tool_uses": 14, "last_tool": "Edit"}, "cost_usd": 0.0123, "worker": {"model": "claude-haiku-5-5"},
        "elapsed_seconds": 72.0}


class JobViewTests(unittest.TestCase):
    def lines(self, found):
        return [ui.plain(line) for line in found]

    def test_one_line_at_the_end(self):
        self.assertEqual(self.lines(jobs_view.end_lines(DONE)), [
            "✓ done in 1m 12s · claude on a1 · haiku · 14 tool uses · $0.01",
            f"  `agt result {JOB}` shows it again."])
        failed = {**DONE, "outcome": "error", "message": "claude exited 1", "cost_usd": 0, "warnings": ["a1 is near its limit"]}
        self.assertEqual(self.lines(jobs_view.end_lines(failed)), [
            "✗ error after 1m 12s · claude on a1 · haiku · 14 tool uses", "  claude exited 1", "  a1 is near its limit",
            f"  `agt result {JOB}` shows what it got."])  # no cost when there is none

    def test_what_the_job_said_is_shown_once_in_full(self):
        told = {**DONE, "messages": [{"id": "m1", "kind": "note", "text": "Found two more failing tests."},
                                     {"id": "q1", "kind": "question", "text": "Update the docs too?"}],
                "open_questions": [{"id": "q1", "text": "Update the docs too?"}]}
        self.assertEqual(self.lines(jobs_view.end_lines(told))[1:4], [
            '  It said: "Found two more failing tests."', '  It asks: "Update the docs too?"',
            f'  Answer   agt message {JOB} "<your answer>" --reply-to q1'])
        found = self.lines(jobs_view.wait_lines({"finished": [told], "updates": [], "still_running": []}))
        self.assertEqual(found[1:3], ['  It said: "Found two more failing tests."', '  It asks: "Update the docs too?"'])
        self.assertIn(f'  Answer           agt message {JOB} "<your answer>" --reply-to q1', found)

    def test_wait_gives_a_row_per_job_and_the_next_commands(self):
        other = "20261027-175301-9c2f11"
        with patch.object(jobs, "read_status", lambda job_id: {"job_id": job_id, "runtime": "codex", "account": "primary",
                                                              "progress": {"tool_uses": 3}}):
            found = self.lines(jobs_view.wait_lines({"finished": [{**DONE, "outcome": "error"}], "updates": [],
                                                     "still_running": [other]}))
        self.assertEqual(found, [f"✗ {JOB}   claude on a1       error after 1m 12s",
                                 f"… {other}   codex on primary   still running, 3 tool uses", "",
                                 f"  See why        agt result {JOB}", f"  Keep waiting   agt wait {other}"])


class RunTests(TempHome):
    def agt(self, *argv, person=False):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(ui, "live_on", lambda stream=None: person), patch.object(ui.Live, "delay", 60):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_agents_and_pipes_get_todays_output(self):
        self.write_config(accounts=("a1",))
        code, out, err = self.agt("run", "--cwd", str(self.project), "do the task")
        self.assertEqual((code, out), (0, "done on a1\n"))
        started, _, rest = err.partition("\n")
        self.assertRegex(started, r"^Started \S+$")
        summary = json.loads(rest)
        self.assertEqual((summary["outcome"], summary["account"]), ("success", "a1"))
        self.assertEqual(rest, json.dumps(summary, indent=2, default=str) + "\n")
        job_id = started.split()[1]
        code, out, _ = self.agt("wait", job_id)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["still_running"], [])  # the JSON report, as before

    def test_a_person_gets_the_result_and_one_line(self):
        self.write_config(accounts=("a1",))
        code, out, err = self.agt("run", "--cwd", str(self.project), "do the task", person=True)
        self.assertEqual((code, out), (0, "done on a1\n"))  # the result stays on stdout
        self.assertRegex(err, r"^✓ done in \d+s · claude on a1 · haiku · 1 tool use · \$0\.01\n  `agt result \S+` shows it again\.\n$")
        job_id = re.search(r"`agt result (\S+)`", err).group(1)
        code, out, err = self.agt("wait", job_id, person=True)  # one job: its result, as run ends
        self.assertEqual((code, out), (0, "done on a1\n"))
        self.assertIn("✓ done in ", err)
        long = "x" * 25000
        (jobs.job_dir(job_id) / "result.md").write_text(long, encoding="utf-8")
        self.assertEqual(self.agt("wait", job_id, person=True)[1], long + "\n")  # all of it, not the JSON's 20,000
        code, _, err = self.agt("run", "--json", "--cwd", str(self.project), "again", person=True)
        self.assertTrue(err.startswith("Started "), err)  # --json: today's output

    def test_result_shows_a_person_everything_the_job_said_and_takes_none_of_it(self):
        self.write_config(accounts=("a1",))
        _, _, err = self.agt("run", "--cwd", str(self.project), "do the task", person=True)
        job_id = re.search(r"`agt result (\S+)`", err).group(1)
        messages.post_from_worker(job_id, "Found two more failing tests.")
        asked = messages.post_from_worker(job_id, "Update the docs too?", kind="question")
        still = messages.post_from_worker(job_id, "Bump the version?", kind="question")
        messages.post_to_worker(job_id, "Yes, the README only.", reply_to=asked["id"])
        code, out, err = self.agt("result", job_id, person=True)
        self.assertEqual((code, out), (0, "done on a1\n"))
        self.assertEqual(err.splitlines()[1:], [
            '  It said: "Found two more failing tests."', '  It asked: "Update the docs too?"',
            '    Answered: "Yes, the README only."', '  It asks: "Bump the version?"',
            f'  Answer   agt message {job_id} "<your answer>" --reply-to {still["id"]}'])  # no "shows it again" here
        self.assertEqual(self.agt("result", job_id, person=True)[2], err)  # it took nothing: the same again
        summary = json.loads(self.agt("result", job_id)[1])  # a pipe: today's JSON, and the agent still gets them
        self.assertEqual([item["text"] for item in summary["messages"]],
                         ["Found two more failing tests.", "Update the docs too?", "Bump the version?"])
        self.assertNotIn("messages", json.loads(self.agt("result", job_id, "--json", person=True)[1]))  # given once

    def test_ctrl_c_only_stops_watching(self):
        self.write_config(accounts=("a1",))
        with patch.object(jobs, "wait_for", side_effect=KeyboardInterrupt):
            code, _, err = self.agt("run", "--cwd", str(self.project), "do the task", person=True)
        self.assertEqual(code, 130)
        self.assertRegex(err, r"^Stopped watching; the job keeps running\.\n  Look again   agt status \S+\n$")


if __name__ == "__main__":
    unittest.main()
