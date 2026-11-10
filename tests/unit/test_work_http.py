"""Exercise the real HTTP handler without binding a socket (sandbox compatible)."""
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
