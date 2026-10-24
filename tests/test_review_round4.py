"""Regression tests for the fourth review (reply marking, wake hook re-arming, leases by folder)."""

import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

from helpers import TempHome

from agents_router import accounts, hooks, jobs, messages, tools
from agents_router.config import jobs_dir, load_config
from agents_router.fsutil import try_lock, unlock

JOB = "20260101-000000-abcdef"


def tool_response(result):
    """A tool result the way an MCP host hands it to a PostToolUse hook."""

    return [{"type": "text", "text": json.dumps(result, indent=2)}]


class ReplyMarkingTests(TempHome):
    """Item 1: finding the answer to a question marks only that answer read."""

    def setUp(self):
        super().setUp()
        (jobs_dir() / JOB).mkdir(parents=True)

    def test_follow_up_queued_with_the_answer_is_still_delivered(self):
        question = messages.post_from_worker(JOB, "Which database?", kind="question")
        follow_up = messages.post_to_worker(JOB, "also add tests")
        answer = messages.post_to_worker(JOB, "Postgres.", reply_to=question["id"])

        reply = messages.wait_for_reply(JOB, question["id"], 5)
        self.assertEqual(reply["id"], answer["id"])
        # The worker's inbox hook still shows the follow-up, but not the answer it already has.
        self.assertEqual([item["id"] for item in messages.take_for_hook(JOB)], [follow_up["id"]])
        # check_parent_messages and the runner's follow-up turn still get it, once.
        self.assertEqual([item["id"] for item in messages.take_for_worker(JOB)], [follow_up["id"]])
        self.assertEqual(messages.take_for_worker(JOB), [])

    def test_ask_parent_leaves_the_follow_up_for_check_parent_messages(self):
        answers = []
        worker = threading.Thread(
            target=lambda: answers.append(tools.call("ask_parent", {"question": "Tabs or spaces?", "timeout_seconds": 20}, worker_job=JOB))
        )
        worker.start()
        deadline = time.time() + 10
        while not messages.open_questions(JOB) and time.time() < deadline:
            time.sleep(0.05)
        question = messages.open_questions(JOB)[0]
        messages.post_to_worker(JOB, "rename the module too")
        messages.post_to_worker(JOB, "Spaces.", reply_to=question["id"])
        worker.join(15)
        self.assertEqual(answers[0]["answer"], "Spaces.")
        later = tools.call("check_parent_messages", {}, worker_job=JOB)
        self.assertEqual(later["messages"], [{"kind": "follow_up", "text": "rename the module too"}])


class RearmedWakeHookTests(TempHome):
    """Item 2: subagent_message starts a fresh watcher; each result still wakes the session once."""

    def setUp(self):
        super().setUp()
        os.environ.pop("FAKE_CLAUDE_DELAY", None)
        self.write_config(accounts=("a1",))
        self._poll = patch.object(hooks, "POLL_SECONDS", 0.2)
        self._poll.start()

    def tearDown(self):
        self._poll.stop()
        os.environ.pop("FAKE_CLAUDE_DELAY", None)
        super().tearDown()

    def start(self, prompt="task"):
        return jobs.create_job(runtime="claude", prompt=prompt, cwd=str(self.project))["job_id"]

    def test_plugin_hook_matches_subagent_message(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        entries = data["hooks"]["PostToolUse"]
        names = {name for entry in entries for name in entry["matcher"].split("|")}
        self.assertLessEqual(
            {"mcp__plugin_agents-router_agents-router__subagent_message", "mcp__agents-router__subagent_message"}, names
        )
        for entry in entries:
            handler = entry["hooks"][0]
            self.assertEqual((handler["command"], handler["async"], handler["asyncRewake"]), ("agt hook await", True, True))

    def test_finds_the_new_job_in_a_message_reply(self):
        reply = {"job_id": "old-job", "delivered": "continued", "new_job": {"job_id": "new-job", "state": "queued"}}
        self.assertEqual(hooks.find_job_id(tool_response(reply)), "new-job")
        self.assertEqual(hooks.find_job_id({"structuredContent": reply}), "new-job")
        cut = json.dumps(reply)[:-2]  # not valid JSON any more
        self.assertEqual(hooks.find_job_id(cut), "new-job")
        queued = {"job_id": "running-job", "delivered": "queued for the running subagent", "message_id": "ab12"}
        self.assertEqual(hooks.find_job_id(tool_response(queued)), "running-job")

    def test_answering_a_question_rearms_the_watcher_for_completion(self):
        os.environ["FAKE_CLAUDE_DELAY"] = "3"
        job = self.start()
        question = messages.post_from_worker(job, "Tabs or spaces?", kind="question")
        code, text = hooks.await_job({"tool_response": tool_response({"job_id": job, "state": "queued"})})
        self.assertEqual(code, 2)
        self.assertIn("Tabs or spaces?", text)

        reply = tools.call("subagent_message", {"job_id": job, "text": "Spaces.", "reply_to": question["id"]})
        code, text = hooks.await_job({"tool_response": tool_response(reply)})
        self.assertEqual(code, 2)
        self.assertIn(f"{job} finished: success", text)
        self.assertIn("done on a1", text)
        self.assertNotIn("Tabs or spaces?", text)
        # Delivered once: another watcher for the same job stays silent.
        self.assertEqual(hooks.await_job({"tool_response": tool_response(reply)}), (0, ""))

    def test_message_to_a_finished_job_watches_the_new_job(self):
        job = self.start()
        jobs.wait_for(job, 60)
        self.assertTrue(jobs.claim_delivery(job))  # the model already collected the first result
        reply = tools.call("subagent_message", {"job_id": job, "text": "one more thing"})
        new_job = reply["new_job"]["job_id"]
        code, text = hooks.await_job({"tool_response": tool_response(reply)})
        self.assertEqual(code, 2)
        self.assertIn(f"{new_job} finished: success", text)
        self.assertFalse(jobs.claim_delivery(new_job))

    def test_second_watcher_for_the_same_job_leaves(self):
        os.environ["FAKE_CLAUDE_DELAY"] = "2"
        job = self.start()
        handle = try_lock(jobs.job_dir(job) / "watch.lock")
        self.assertIsNotNone(handle)
        try:
            with patch.object(hooks, "WATCH_LOCK_SECONDS", 0.2):
                self.assertEqual(hooks.await_job({"tool_response": tool_response({"job_id": job})}), (0, ""))
        finally:
            unlock(handle)
        code, text = hooks.await_job({"tool_response": tool_response({"job_id": job})})
        self.assertEqual(code, 2)
        self.assertIn("done on a1", text)

    def test_question_already_shown_does_not_wake_again(self):
        os.environ["FAKE_CLAUDE_DELAY"] = "4"
        job = self.start()
        messages.post_from_worker(job, "Tabs or spaces?", kind="question")
        self.assertEqual(len(messages.take_for_host(job)), 1)  # e.g. subagent_wait showed it
        code, text = hooks.await_job({"tool_response": tool_response({"job_id": job})}, deadline_seconds=0.5)
        self.assertEqual(code, 2)
        self.assertIn("still running", text)
        self.assertNotIn("Tabs or spaces?", text)
        jobs.wait_for(job, 60)


class LeaseByFolderTests(TempHome):
    """Item 3: job slots belong to the account folder, not to the account name."""

    def write_shared_config(self, home_a1, home_a2):
        lines = [
            "[claude]",
            f"primary = {json.dumps(str(self.primary))}",
            "max_jobs_per_account = 1",
            "[[claude.accounts]]",
            'name = "a1"',
            f"home = {json.dumps(str(home_a1))}",
            "[[claude.accounts]]",
            'name = "a2"',
            f"home = {json.dumps(str(home_a2))}",
        ]
        (self.home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        config = load_config().runtime("claude")
        return config, config.find("a1"), config.find("a2")

    def test_two_names_for_one_folder_share_the_slot(self):
        folder = self.root / "homes" / "shared"
        (folder / "sub").mkdir(parents=True)
        config, a1, a2 = self.write_shared_config(folder, folder / "sub" / "..")
        lease = accounts.try_lease(config, a1)
        self.assertIsNotNone(lease)
        try:
            self.assertIsNone(accounts.try_lease(config, a2))
            self.assertEqual(accounts.busy_slots(config, a2), 1)
            self.assertEqual(accounts.acquire(config), (None, None, True))
        finally:
            lease.release()
        second = accounts.try_lease(config, a2)
        self.assertIsNotNone(second)
        second.release()

    def test_live_owner_record_blocks_every_name_for_the_folder(self):
        folder = self.root / "homes" / "shared"
        folder.mkdir(parents=True)
        config, a1, a2 = self.write_shared_config(folder, folder)
        lease = accounts.try_lease(config, a1)
        lease.set_owner(os.getpid(), None, "job")
        unlock(lease.handle)  # the runner died, its worker (this process) still runs
        self.assertIsNone(accounts.try_lease(config, a2))
        self.assertEqual(accounts.busy_slots(config, a2), 1)

    def test_different_folders_have_their_own_slots(self):
        config, a1, a2 = self.write_shared_config(self.root / "homes" / "one", self.root / "homes" / "two")
        first = accounts.try_lease(config, a1)
        second = accounts.try_lease(config, a2)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        first.release()
        second.release()
