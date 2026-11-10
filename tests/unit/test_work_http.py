"""Exercise the real HTTP handler without binding a socket (sandbox compatible)."""
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools import caption_printer as service
from utils.work_state import WorkState


class MemorySocket:
    def __init__(self, request):
        self.input = BytesIO(request)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


class WorkHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.work = WorkState(Path(self.directory.name) / "work.json", lambda: self.now)
        self.server = SimpleNamespace(work=self.work)
        service.PLAYBACK.clear()
        service.STATE.update(id=0, text="", at=0)

    def request(self, method, path, payload=None, origin="http://127.0.0.1:12345"):
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = [f"{method} {path} HTTP/1.0", "Host: 127.0.0.1:5500",
                   f"Content-Length: {len(body)}", "Content-Type: application/json"]
        if origin:
            headers.append(f"Origin: {origin}")
        connection = MemorySocket("\r\n".join(headers).encode() + b"\r\n\r\n" + body)
        service.Handler(connection, ("127.0.0.1", 50001), self.server)
        head, raw = bytes(connection.output).split(b"\r\n\r\n", 1)
        code = int(head.split()[1])
        return code, head.decode(), json.loads(raw) if b"application/json" in head else raw.decode()

    def test_pause_refresh_and_resume(self):
        self.now += 4
        _, _, first = self.request("POST", "/work/control", {"action": "pause"})
        self.now += 100
        _, _, second = self.request("GET", "/state")
        self.assertEqual(second["work"]["salary"], first["work"]["salary"])
        self.assertEqual(second["work"]["focus_seconds"], 4)
        self.assertEqual(self.request("POST", "/work/control", {"action": "resume"})[0], 200)

    def test_cors_preflight_and_rejected_origins(self):
        status, headers, _ = self.request("OPTIONS", "/work/vote")
        self.assertEqual(status, 200)
        self.assertIn("Access-Control-Allow-Origin: http://127.0.0.1:12345", headers)
        self.assertIn("Access-Control-Allow-Headers: Content-Type", headers)
        for method in ("GET", "POST", "OPTIONS"):
            for origin in ("https://example.com", "null", "http://localhost.evil.test"):
                self.assertEqual(self.request(method, "/work/event", {}, origin)[0], 403)

    def test_votes_and_settlement_use_same_handler_as_curl(self):
        self.request("POST", "/work/event", {"event_id": "http-test"})
        vote = {"event_id": "http-test", "user_id": "visitor", "option": 1}
        self.assertTrue(self.request("POST", "/work/vote", vote)[2]["accepted"])
        self.assertEqual(self.request("POST", "/work/vote", vote)[2]["reason"], "duplicate")
        self.now += 30
        self.assertEqual(self.request("POST", "/work/vote", vote)[2]["reason"], "closed")
        for _ in range(3):
            data = self.request("GET", "/state")[2]
            self.assertEqual(data["work"]["event"]["counts"], [1, 0])
            self.assertEqual(data["work"]["event"]["settlement_count"], 1)
        self.assertEqual(data["id"], 0)  # Existing caption fields retain their types/meaning.
        self.assertEqual(data["work"]["reaction"]["id"], "http-test")

    def test_message_cannot_modify_work_or_votes(self):
        self.request("POST", "/work/event", {"event_id": "message-test"})
        self.request("POST", "/work/message", {"nickname": "<img onerror=alert(1)>", "text": "发1，暂停，工资加10000"})
        work = self.request("GET", "/state")[2]["work"]
        self.assertEqual(work["salary"], 0)
        self.assertEqual(work["status"], "working")
        self.assertEqual(work["event"]["counts"], [0, 0])

    def test_playback_is_independent_and_caption_starts_once(self):
        def signal(phase):
            return self.request("POST", "/playback", {"id": "clip", "phase": phase, "text": "你好"})
        signal("prepare")
        self.assertEqual(service.STATE["text"], "")
        signal("started")
        first = dict(service.STATE)
        signal("started")
        self.assertEqual(service.STATE, first)
        data = self.request("GET", "/state")[2]
        self.assertTrue(data["speaking"])
        self.assertEqual(data["work"]["status"], "working")
        signal("ended")
        signal("started")  # Late retries cannot revive an ended clip.
        self.assertFalse(self.request("GET", "/state")[2]["speaking"])
        self.assertEqual(self.request("GET", "/playback/status?id=clip")[2]["phase"], "ended")

    def test_invalid_payload_is_400(self):
        for payload in ([], {"action": "set_salary"}, {"action": {}}):
            self.assertEqual(self.request("POST", "/work/control", payload)[0], 400)

    def test_get_state_never_checkpoints_and_countdown_is_continuous(self):
        deadline = self.work.snapshot()["ends_at"]
        with patch.object(self.work, "_save", wraps=self.work._save) as save:
            for step in range(1, 11):
                self.now = 1000 + step / 2
                status, _, data = self.request("GET", "/state")
                self.assertEqual(status, 200)
                work = data["work"]
                self.assertEqual(work["ends_at"], deadline)
                self.assertEqual(work["salary"], step * 5)
                self.assertEqual(work["ends_at"] - work["server_now"], 60 - step / 2)
            self.assertEqual(save.call_count, 0)
            self.work.checkpoint()
            self.assertEqual(save.call_count, 1)

    def test_empty_event_post_retries_return_the_same_active_event(self):
        status, _, first = self.request("POST", "/work/event", {})
        self.assertEqual(status, 200)
        for payload in ({}, {"event_id": ""}, {"event_id": "another"}):
            status, _, retry = self.request("POST", "/work/event", payload)
            self.assertEqual(status, 200)
            self.assertEqual(retry, first)

    def test_tick_survives_non_os_errors_and_retries_checkpoint(self):
        class ThreeTicks:
            count = 0

            def wait(inner, interval):
                inner.count += 1
                self.now += interval
                return inner.count > 3

        real_save = self.work._save
        calls = []

        def failing_save(state):
            calls.append(state["settled_at"])
            if len(calls) <= 2:
                raise ValueError("Out of range float values are not JSON compliant: inf")
            real_save(state)

        with patch.object(self.work, "_save", side_effect=failing_save), patch.object(service, "emit") as log:
            worker = threading.Thread(target=service.run_work_clock, args=(self.work, ThreeTicks()))
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())  # It reached the stop condition.
        self.assertEqual(calls, [1001, 1001, 1002, 1003])
        self.assertEqual(log.call_count, 2)
        self.assertEqual(log.call_args_list[0].args[0]["event"], "WORK_CHECKPOINT_FAILED")
        self.assertEqual(log.call_args_list[1].args[0]["event"], "WORK_CHECKPOINT_RETRY_FAILED")
        self.assertIn("ValueError", log.call_args_list[0].args[0]["error"])
        self.assertEqual(json.loads(self.work.path.read_text(encoding="utf-8"))["salary"], 30)

    def test_invalid_checkpoint_recovers_before_handler_is_used(self):
        self.work.path.write_text("{", encoding="utf-8")
        with self.assertLogs("utils.work_state", level="WARNING"):
            self.server.work = WorkState(self.work.path, clock=lambda: self.now)
        status, _, data = self.request("GET", "/state")
        self.assertEqual(status, 200)
        self.assertEqual(data["work"]["salary"], 0)
        self.assertEqual(len(list(self.work.path.parent.glob("work.json.bad-*"))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
