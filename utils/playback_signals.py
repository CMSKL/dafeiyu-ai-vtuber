"""Loopback playback lifecycle protocol, independent of audio/LLM dependencies.

External players must POST started/heartbeat/ended with this id to callback_url.
An enqueue acknowledgement is deliberately insufficient to start captions.
"""
import json
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid


class PlaybackSignals:
    def __init__(self, speech=True):
        self.id = uuid.uuid4().hex
        self.speech = speech
        self.stopped = threading.Event()
        self.metadata = {"id": self.id, "callback_url": "http://127.0.0.1:5500/playback", "protocol": 1}

    def send(self, phase):
        payload = {"id": self.id, "phase": phase, "speech": self.speech}
        request = Request(self.metadata["callback_url"],
                          data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=0.5) as response:
                return json.load(response)
        except (OSError, URLError, ValueError):
            return None  # A missing HUD service must never prevent audio playback.

    def start(self):
        self.send("started")

        def heartbeat():
            while not self.stopped.wait(5):
                self.send("heartbeat")
        threading.Thread(target=heartbeat, daemon=True, name="speech-heartbeat").start()

    def end(self):
        self.stopped.set()
        self.send("ended")

    def watch_external(self, on_start):
        """Observe actual remote lifecycle callbacks, including very short clips."""
        def watch():
            emitted = False
            expires = time.monotonic() + 3600
            while not self.stopped.is_set() and time.monotonic() < expires:
                try:
                    url = "http://127.0.0.1:5500/playback/status?id=" + self.id
                    with urlopen(url, timeout=0.5) as response:
                        value = json.load(response)
                    if value.get("started_at") and not emitted:
                        emitted = True
                        on_start()
                    if value.get("phase") == "ended":
                        return
                except (OSError, URLError, ValueError):
                    pass
                self.stopped.wait(0.25)
        threading.Thread(target=watch, daemon=True, name="external-playback-captions").start()
