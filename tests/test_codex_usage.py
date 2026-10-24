import json
from pathlib import Path
import time
import unittest

from helpers import TempHome

from agents_router import accounts
from agents_router.runtimes.codex import CodexParser, find_rollout, last_rate_limits, usage_windows

THREAD = "01a141dd-7dbd-73d2-bc93-bd302ca72586"


def limits(used5, used7, reset5=1792106822):
    return {
        "limit_id": "codex",
        "primary": {"used_percent": used5, "window_minutes": 300, "resets_at": reset5},
        "secondary": {"used_percent": used7, "window_minutes": 10080, "resets_at": 1792675611},
    }


class RolloutTests(TempHome):
    def rollout(self, *snapshots, thread=THREAD):
        folder = self.primary / "sessions" / "2026" / "10" / "16"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-10-16T01-19-57-{thread}.jsonl"
        lines = [json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "private text"}})]
        for snapshot in snapshots:
            lines.append(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": snapshot}}))
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "x"}}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_last_snapshot_and_windows(self):
        path = self.rollout(limits(10.0, 5.0), limits(92.0, 27.0))
        self.assertEqual(find_rollout(self.primary, THREAD), path)
        self.assertIsNone(find_rollout(self.primary, "other"))
        found = last_rate_limits(path)
        windows = usage_windows(found)
        self.assertEqual(windows["five_hour"], {"utilization": 0.92, "resets_at": 1792106822.0})
        self.assertAlmostEqual(windows["seven_day"]["utilization"], 0.27)

    def test_parser_records_usage_for_ranking(self):
        self.rollout(limits(92.0, 27.0))
        parser = CodexParser("c1", self.primary)
        parser.feed({"type": "thread.started", "thread_id": THREAD})
        parser.feed({"type": "turn.completed", "usage": {}})
        self.assertEqual(parser.finish(0, "").outcome, "success")
        record = accounts.load_usage("codex")["c1"]
        self.assertEqual(record["windows"]["five_hour"]["utilization"], 0.92)
        self.assertEqual(accounts.max_utilization(record), 0.92)

    def test_limit_uses_snapshot_reset_time(self):
        reset = time.time() + 3600
        self.rollout(limits(100.0, 40.0, reset5=reset))
        parser = CodexParser("c1", self.primary)
        parser.feed({"type": "thread.started", "thread_id": THREAD})
        parser.feed({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
        result = parser.finish(1, "")
        self.assertEqual(result.outcome, "usage_limit")
        self.assertEqual(result.resets_at, reset)

    def test_missing_rollout_is_harmless(self):
        parser = CodexParser("c1", self.primary)
        parser.feed({"type": "thread.started", "thread_id": THREAD})
        parser.feed({"type": "turn.completed", "usage": {}})
        self.assertEqual(parser.finish(0, "").outcome, "success")
        self.assertNotIn("c1", accounts.load_usage("codex"))


if __name__ == "__main__":
    unittest.main()
