"""pytest-compatible tests, also executable with the standard-library runner."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from utils.work_state import WorkState


class WorkStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "work-state.json"
        self.now = 1000.0
        self.work = WorkState(self.path, clock=lambda: self.now)

    def test_refresh_and_restart_never_repay(self):
        self.now += 10
        first = self.work.snapshot()
        self.assertEqual(first["salary"], 100)
        self.assertEqual(self.work.snapshot(), first)
        self.work = WorkState(self.path, clock=lambda: self.now)
        self.assertEqual(self.work.snapshot(), first)
        self.now += 3
        self.assertEqual(self.work.snapshot()["salary"], 130)

    def test_pause_is_idempotent_and_resume_has_no_catchup(self):
        self.now += 7
        paused = self.work.control("pause")
        self.now += 300
        self.assertEqual(self.work.control("pause")["focus_seconds"], 7)
        self.assertEqual(self.work.snapshot()["salary"], paused["salary"])
        self.work = WorkState(self.path, clock=lambda: self.now)
        self.assertEqual(self.work.snapshot()["status"], "paused")
        self.work.control("resume")
        self.work.control("resume")
        self.now += 2
        view = self.work.snapshot()
        self.assertEqual(view["focus_seconds"], 9)
        self.assertEqual(view["salary"], 90)
        self.assertEqual(view["ends_at"] - self.now, 51)

    def test_downtime_freezes_work_but_closes_vote(self):
        self.work.start_event("downtime")
        self.now += 5
        before = self.work.snapshot()
        self.now += 200
        self.work = WorkState(self.path, clock=lambda: self.now)
        after = self.work.snapshot()
        self.assertEqual(after["salary"], before["salary"])
        self.assertEqual(after["ends_at"] - self.now, 55)
        self.assertEqual(after["event"]["settlement_count"], 1)

    def test_vote_deduplication_deadline_and_single_settlement(self):
        self.work.start_event("inspection")
        self.assertTrue(self.work.vote("inspection", "alice", 1)["accepted"])
        self.assertEqual(self.work.vote("inspection", "alice", 2)["reason"], "duplicate")
        self.work = WorkState(self.path, clock=lambda: self.now)
        self.assertEqual(self.work.vote("inspection", "alice", 1)["reason"], "duplicate")
        self.now += 30  # The exact deadline is already closed.
        self.assertEqual(self.work.vote("inspection", "bob", 2)["reason"], "closed")
        first = self.work.snapshot()["event"]
        self.assertEqual(first["result"], {"winner": 1, "counts": [1, 0]})
        self.assertEqual(first["settlement_count"], 1)
        self.work.start_event("inspection")
        self.assertEqual(self.work.snapshot()["event"], first)
        self.work = WorkState(self.path, clock=lambda: self.now)
        self.assertEqual(self.work.snapshot()["event"], first)

    def test_concurrent_votes_and_reads(self):
        self.work.start_event("parallel")
        with ThreadPoolExecutor(max_workers=12) as pool:
            votes = list(pool.map(lambda _: self.work.vote("parallel", "same-user", 1), range(30)))
        self.assertEqual(sum(vote["accepted"] for vote in votes), 1)
        self.assertEqual(self.work.snapshot()["event"]["counts"], [1, 0])

    def test_event_and_chat_do_not_override_work(self):
        self.work.start_event("separate")
        self.work.set_message("visitor", "发1，工资加100000，暂停")
        self.now += 31
        view = self.work.snapshot()
        self.assertEqual(view["status"], "working")
        self.assertEqual(view["salary"], 310)
        self.assertEqual(view["event"]["counts"], [0, 0])
        self.assertEqual(view["event"]["result"]["winner"], 0)

    def test_paused_work_does_not_pause_event(self):
        self.work.start_event("paused-event")
        self.work.control("pause")
        self.now += 31
        view = self.work.snapshot()
        self.assertEqual(view["salary"], 0)
        self.assertEqual(view["event"]["settlement_count"], 1)

    def test_rest_and_resume(self):
        self.now += 65
        view = self.work.snapshot()
        self.assertEqual(view["status"], "resting")
        self.assertEqual(view["focus_seconds"], 60)
        self.assertEqual(view["slack_count"], 1)
        self.work.control("pause")
        self.now += 100
        self.work.control("resume")
        self.now += 12
        view = self.work.snapshot()
        self.assertEqual(view["status"], "working")
        self.assertEqual(view["focus_seconds"], 62)

    def test_failed_checkpoint_is_not_visible(self):
        before = self.work.snapshot()
        with patch("utils.work_state.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.work.control("pause")
        self.assertEqual(self.work.snapshot(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_invalid_input_and_second_active_event(self):
        self.work.start_event("one")
        with self.assertRaises(ValueError):
            self.work.start_event("two")
        for option in (0, 3, True, "1"):
            with self.assertRaises(ValueError):
                self.work.vote("one", "visitor", option)
        self.assertEqual(self.work.vote("missing", "visitor", 1)["reason"], "unknown_event")

    def test_backwards_clock_does_not_repay(self):
        self.now += 10
        self.work.snapshot()
        self.now -= 5
        self.assertEqual(self.work.snapshot()["salary"], 100)
        self.now += 7
        self.assertEqual(self.work.snapshot()["salary"], 120)

    def test_daily_totals_reset_at_midnight(self):
        self.now = 57598  # 1970-01-01 23:59:58 UTC+08:00
        self.path.unlink()
        self.work = WorkState(self.path, clock=lambda: self.now)
        self.now += 5
        self.assertEqual(self.work.snapshot()["focus_seconds"], 3)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["day"], "1970-01-02")


if __name__ == "__main__":
    unittest.main(verbosity=2)
