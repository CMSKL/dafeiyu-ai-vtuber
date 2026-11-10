"""pytest-compatible tests, also executable with the standard-library runner."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import unicodedata
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from utils.work_state import EVENT_HISTORY_LIMIT, MAX_SETTLEMENT_GAP, WorkState


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
        self.work.checkpoint()  # The service's one-second durable tick.
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
        self.work.checkpoint()
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
        first = self.work.start_event("one")
        self.assertEqual(self.work.start_event("two"), first)
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
        self.work.checkpoint()
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["day"], "1970-01-02")

    def test_restart_after_clock_rollback_matches_live_control(self):
        now = 3600.0
        restarted = WorkState(self.path, clock=lambda: now)
        control = WorkState(self.path.with_name("control.json"), clock=lambda: now)
        now = 3700.0
        self.assertEqual(restarted.checkpoint(), control.checkpoint())
        now = 3500.0
        restarted = WorkState(self.path, clock=lambda: now)
        pinned = restarted.snapshot()
        self.assertEqual(pinned, control.snapshot())
        self.assertEqual(restarted.state["settled_at"], 3700)
        self.assertGreaterEqual(restarted.state["ends_at"], 3700)
        for at in (3600.0, 3700.0, 3800.0):
            now = at
            self.assertEqual(restarted.snapshot(), control.snapshot())
        restarted.checkpoint()
        self.assertEqual(WorkState(self.path, clock=lambda: now).snapshot(), control.snapshot())

    def test_bad_checkpoints_are_quarantined_and_replaced(self):
        valid = json.loads(self.path.read_text(encoding="utf-8"))
        bad_values = ["{", "", "not JSON", "[]"]
        mutations = [
            ("version", 2), ("ends_at", "1060"), ("ends_at", True),
            ("paused_remaining", "10"), ("speed_multiplier", 1e308),
            ("speed_multiplier", 0), ("speed_multiplier", float("nan")),
            ("speed_multiplier", 1e305),
            ("salary", -1), ("focus_seconds", float("inf")),
            ("round", 0), ("events", []), ("reaction", {}),
        ]
        for key, value in mutations:
            bad_values.append(json.dumps({**valid, key: value}))
        missing = dict(valid)
        del missing["ends_at"]
        bad_values.append(json.dumps(missing))
        for index, content in enumerate(bad_values, 1):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertLogs("utils.work_state", level="WARNING"):
                    fresh = WorkState(self.path, clock=lambda: self.now)
                quarantined = list(self.path.parent.glob("work-state.json.bad-*"))
                self.assertEqual(len(quarantined), index)
                self.assertIn(content.encode(), [path.read_bytes() for path in quarantined])
                self.assertEqual(fresh.snapshot()["salary"], 0)
                self.assertEqual(fresh.snapshot()["speed_multiplier"], 1)
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["version"], 1)

    def test_late_settlement_reaction_is_fresh_and_emitted_once(self):
        self.work.start_event("late")
        self.now += 3600
        view = self.work.snapshot()
        self.assertEqual(view["event"]["ends_at"], 1030)
        self.assertEqual(view["event"]["settled_at"], self.now)
        self.assertEqual(view["reaction"]["at"], self.now)
        self.assertTrue(-2 <= self.now - view["reaction"]["at"] < 45)
        self.now += 1
        self.assertEqual(self.work.checkpoint()["reaction"], view["reaction"])
        self.work = WorkState(self.path, clock=lambda: self.now)
        self.assertEqual(self.work.snapshot()["event"]["settlement_count"], 1)
        self.assertEqual(self.work.snapshot()["reaction"], view["reaction"])

    def test_snapshot_settles_in_memory_without_writing(self):
        initial = self.path.read_bytes()
        deadline = self.work.snapshot()["ends_at"]
        with patch.object(self.work, "_save", wraps=self.work._save) as save:
            for step in range(1, 11):
                self.now = 1000 + step / 4
                view = self.work.snapshot()
                self.assertEqual(view["salary"], step * 2.5)
                self.assertEqual(view["ends_at"], deadline)
                self.assertEqual(deadline - view["server_now"], 60 - step / 4)
            self.assertEqual(save.call_count, 0)
            self.assertEqual(self.path.read_bytes(), initial)
            self.work.checkpoint()
            self.assertEqual(save.call_count, 1)
        self.assertEqual(WorkState(self.path, clock=lambda: self.now).snapshot(), view)

    def test_event_summaries_are_bounded_and_drop_voter_details(self):
        for index in range(50):
            event_id = f"event-{index}"
            self.work.start_event(event_id)
            self.work.vote(event_id, "viewer", 1)
            self.now += 31
            view = self.work.checkpoint()
            self.assertEqual(view["event"]["counts"], [1, 0])
            self.assertEqual(view["event"]["result"]["counts"], [1, 0])
            self.assertEqual(view["event"]["settlement_count"], 1)
            self.assertEqual(self.work.start_event(event_id)["event"], view["event"])
        self.assertEqual(len(self.work.state["events"]), EVENT_HISTORY_LIMIT + 1)
        self.assertTrue(all("votes" not in event for event in self.work.state["events"].values()))
        self.assertEqual(self.work.vote("event-0", "viewer", 1)["reason"], "unknown_event")
        self.assertEqual(self.work.vote("event-49", "other", 1)["reason"], "closed")
        before = self.work.snapshot()
        self.work = WorkState(self.path, clock=lambda: self.now)
        self.assertEqual(self.work.snapshot(), before)
        self.work.start_event("active")
        self.assertEqual(len(self.work.state["events"]), EVENT_HISTORY_LIMIT + 1)
        self.assertEqual(self.work.state["events"]["active"]["votes"], {})

    def test_eight_hour_in_process_gap_pays_only_the_capped_interval(self):
        reference_now = self.now
        reference = WorkState(self.path.with_name("reference.json"), clock=lambda: reference_now)
        reference_now += MAX_SETTLEMENT_GAP
        expected = reference.snapshot()
        self.now += 8 * 3600
        actual = self.work.snapshot()
        for field in ("salary", "focus_seconds", "round", "slack_count", "status"):
            self.assertEqual(actual[field], expected[field])
        self.assertEqual(actual["ends_at"] - self.now, expected["ends_at"] - reference_now)
        self.assertEqual(self.work.snapshot(), actual)
        self.now += 1
        self.assertEqual(self.work.snapshot()["salary"], actual["salary"] + 10)

    def test_anonymous_event_retries_and_empty_ids_are_idempotent(self):
        first = self.work.start_event()
        for event_id in (None, "", "different-id"):
            self.assertEqual(self.work.start_event(event_id), first)
        self.assertEqual(len(self.work.state["events"]), 1)
        self.now += 30
        settled = self.work.snapshot()["event"]
        self.assertEqual(self.work.start_event(settled["id"])["event"], settled)
        next_event = self.work.start_event("")["event"]
        self.assertNotEqual(next_event["id"], settled["id"])

    def test_voter_identity_normalizes_case_unicode_and_invisible_characters(self):
        self.work.start_event("identities")
        self.assertTrue(self.work.vote("identities", "alice", 1)["accepted"])
        aliases = ["alice ", " alice", "ALICE", "Alice", "alice\xa0", "alice\u200b",
                   "ａｌｉｃｅ", "alice\u034f", "alice\u0890", "alice\u0891"]
        # Cover every Cf code point supported by this Python Unicode database.
        aliases.extend("alice" + chr(point) for point in range(sys.maxunicode + 1)
                       if unicodedata.category(chr(point)) == "Cf")
        for alias in aliases:
            with self.subTest(alias=ascii(alias)):
                self.assertEqual(self.work.vote("identities", alias, 2)["reason"], "duplicate")
        self.assertEqual(self.work.snapshot()["event"]["counts"], [1, 0])
        self.assertTrue(self.work.vote("identities", "café", 1)["accepted"])
        self.assertEqual(self.work.vote("identities", "cafe\u0301", 1)["reason"], "duplicate")
        for blank in (" ", "\xa0", "\u200b\u034f"):
            with self.assertRaises(ValueError):
                self.work.vote("identities", blank, 1)
        self.assertEqual(self.work.snapshot()["event"]["counts"], [2, 0])

    @unittest.skipUnless(hasattr(os, "O_DIRECTORY"), "POSIX directory fsync")
    def test_checkpoint_fsyncs_file_then_parent_directory(self):
        calls = []
        real_fsync = os.fsync
        real_replace = os.replace

        def fsync(descriptor):
            calls.append("fsync")
            return real_fsync(descriptor)

        def replace(source, destination):
            calls.append("replace")
            return real_replace(source, destination)

        with patch("utils.work_state.os.fsync", side_effect=fsync), patch("utils.work_state.os.replace", side_effect=replace):
            self.work.checkpoint()
        self.assertEqual(calls, ["fsync", "replace", "fsync"])

    @unittest.skipUnless(hasattr(os, "O_DIRECTORY"), "POSIX directory fsync")
    def test_directory_sync_failure_keeps_committed_memory_and_disk_consistent(self):
        with patch("utils.work_state.os.fsync", side_effect=[None, OSError("fsync unavailable")]):
            with self.assertLogs("utils.work_state", level="WARNING"):
                view = self.work.control("pause")
        self.assertEqual(view["status"], "paused")
        self.assertEqual(self.work.state, json.loads(self.path.read_text(encoding="utf-8")))

    def test_loading_old_settled_events_preserves_counts_and_prunes_history(self):
        self.work.start_event("legacy")
        self.work.vote("legacy", "viewer", 2)
        self.now += 30
        self.work.checkpoint()
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        event = stored["events"]["legacy"]
        event["votes"] = {"0" * 64: 2}  # Original v1 kept votes after settlement.
        for index in range(50):
            stored["events"][f"old-{index}"] = {**event, "id": f"old-{index}"}
        self.path.write_text(json.dumps(stored), encoding="utf-8")
        restored = WorkState(self.path, clock=lambda: self.now)
        self.assertEqual(restored.snapshot()["event"]["counts"], [0, 1])
        self.assertEqual(restored.snapshot()["event"]["settlement_count"], 1)
        self.assertEqual(len(restored.state["events"]), EVENT_HISTORY_LIMIT + 1)
        self.assertTrue(all("votes" not in item for item in restored.state["events"].values()))

    def test_state_file_is_utf8_regardless_of_system_locale(self):
        # The service always writes UTF-8. A reader that follows the system
        # default (GBK on Windows) fails on non-ASCII content.
        self.work.set_message("观众甲", "今天先做完哪件小事")
        self.work.checkpoint()
        raw = self.path.read_bytes()
        self.assertIn("今天先做完哪件小事".encode("utf-8"), raw)
        self.assertEqual(json.loads(raw.decode("utf-8"))["message"]["text"], "今天先做完哪件小事")

    def test_long_paused_gap_still_pays_nothing(self):
        self.now += 7
        before = self.work.control("pause")
        self.now += 8 * 3600
        after = self.work.snapshot()
        for field in ("salary", "focus_seconds", "paused_remaining", "round", "slack_count"):
            self.assertEqual(after[field], before[field])

    def test_capped_gap_keeps_daily_reset_semantics(self):
        self.now = 57598
        self.path.unlink()
        self.work = WorkState(self.path, clock=lambda: self.now)
        self.now += 2 * 86400 + 5
        view = self.work.snapshot()
        self.assertEqual(view["day"], "1970-01-04")
        self.assertEqual(view["salary"], 30)
        self.assertEqual(view["focus_seconds"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
