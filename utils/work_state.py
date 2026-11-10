"""Durable, server-authoritative work simulation. Standard library only.

One service owns this file. Commands checkpoint atomically before publication.
Snapshots settle in memory. A one-second service tick checkpoints that progress.
Crash recovery restores the last successful checkpoint, including its cursor.
Process downtime freezes the work clock. In-process settlement gaps pay at most
120 seconds and freeze the excess. Votes retain their wall-clock deadline.
After a backwards clock adjustment the work cursor stays pinned until wall time
catches up. That interval earns no back pay. Timestamps use Unix seconds and
daily totals use UTC+08:00.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata
import uuid


WORK_SECONDS = 60
REST_SECONDS = 15
VOTE_SECONDS = 30
PAY_PER_SECOND = 10
SALARY_GOAL = 6000
MAX_SETTLEMENT_GAP = 120
EVENT_HISTORY_LIMIT = 10
RULES = ("完成一轮后，记得让眼睛休息", "休息时间也要坐稳工位", "暂停后计时与工资一起停下")
LOCAL_TZ = timezone(timedelta(hours=8))
LOGGER = logging.getLogger(__name__)


def _day(at):
    return datetime.fromtimestamp(at, LOCAL_TZ).date().isoformat()


def _next_midnight(at):
    tomorrow = datetime.fromtimestamp(at, LOCAL_TZ).date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), LOCAL_TZ).timestamp()


class WorkState:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()
        now = clock()
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
                self._validate(state)
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                quarantine = self.path.with_name(
                    f"{self.path.name}.bad-{time.time_ns()}-{uuid.uuid4().hex[:8]}")
                os.replace(self.path, quarantine)
                LOGGER.warning("Quarantined invalid work state at %s (%s)", quarantine, error)
                state = self._new_state(now)
            # Never pay for process downtime or replay already checkpointed time.
            cursor = state["settled_at"]
            base = max(now, cursor)
            if state["ends_at"] is not None:
                remaining = max(0, state["ends_at"] - cursor)
                state["ends_at"] = base + remaining
            state["settled_at"] = base
        else:
            state = self._new_state(now)
        self.state = state
        self.checkpoint()

    @staticmethod
    def _new_state(now):
        return {
            "version": 1, "day": _day(now), "salary": 0.0,
            "focus_seconds": 0.0, "slack_count": 0, "speed_multiplier": 1.0,
            "status": "working", "resume_status": "working",
            "ends_at": now + WORK_SECONDS, "paused_remaining": None,
            "settled_at": now, "round": 1, "events": {},
            "event_id": None, "reaction": None, "message": None,
        }

    @staticmethod
    def _validate(state):
        """Reject corrupt checkpoints before any arithmetic or state publication."""
        def number(value, name, minimum=0):
            if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
                raise ValueError(f"invalid {name}")

        def timestamp(value, name):
            number(value, name)
            _day(value)  # Also reject finite timestamps outside datetime's range.

        def counter(value, name, minimum=0):
            if type(value) is not int or value < minimum:
                raise ValueError(f"invalid {name}")

        required = set(WorkState._new_state(0))
        if not isinstance(state, dict) or not required.issubset(state):
            raise ValueError("incomplete work state")
        if type(state["version"]) is not int or state["version"] != 1:
            raise ValueError("unsupported work state version")
        if not isinstance(state["day"], str):
            raise ValueError("invalid day")
        datetime.strptime(state["day"], "%Y-%m-%d")
        for name in ("salary", "focus_seconds", "speed_multiplier"):
            number(state[name], name)
        # No settings endpoint exists. New states always use 1.0. A future setting
        # must validate its rate and checkpoint through a transaction before use.
        if state["speed_multiplier"] <= 0:
            raise ValueError("invalid speed_multiplier")
        number(state["salary"] + 86400 * PAY_PER_SECOND * state["speed_multiplier"],
               "maximum daily salary")
        counter(state["slack_count"], "slack_count")
        counter(state["round"], "round", 1)
        timestamp(state["settled_at"], "settled_at")
        if state["status"] not in ("working", "resting", "paused"):
            raise ValueError("invalid status")
        if state["resume_status"] not in ("working", "resting"):
            raise ValueError("invalid resume_status")
        if state["ends_at"] is not None:
            timestamp(state["ends_at"], "ends_at")
            if state["ends_at"] < state["settled_at"]:
                raise ValueError("ends_at precedes settlement cursor")
        if state["paused_remaining"] is not None:
            number(state["paused_remaining"], "paused_remaining")
        if state["status"] == "paused":
            if state["ends_at"] is not None or state["paused_remaining"] is None:
                raise ValueError("invalid paused timer")
            duration = WORK_SECONDS if state["resume_status"] == "working" else REST_SECONDS
            remaining = state["paused_remaining"]
        elif state["ends_at"] is None or state["paused_remaining"] is not None:
            raise ValueError("invalid running timer")
        else:
            duration = WORK_SECONDS if state["status"] == "working" else REST_SECONDS
            remaining = state["ends_at"] - state["settled_at"]
        if remaining > duration:
            raise ValueError("remaining time exceeds round duration")
        if not isinstance(state["events"], dict):
            raise ValueError("invalid events")
        if state["event_id"] is not None and (
                not isinstance(state["event_id"], str) or state["event_id"] not in state["events"]):
            raise ValueError("invalid current event")
        for event_id, event in state["events"].items():
            if not isinstance(event, dict) or event["id"] != event_id:
                raise ValueError("invalid event")
            timestamp(event["ends_at"], "event ends_at")
            number(event["duration"], "event duration", 1)
            if not isinstance(event["question"], str) or not isinstance(event["options"], list):
                raise ValueError("invalid event text")
            if len(event["options"]) != 2 or not all(isinstance(item, str) for item in event["options"]):
                raise ValueError("invalid event options")
            votes = event.get("votes", {})
            if not isinstance(votes, dict):
                raise ValueError("invalid votes")
            for voter, option in votes.items():
                if not re.fullmatch(r"[0-9a-f]{64}", voter) or type(option) is not int or option not in (1, 2):
                    raise ValueError("invalid vote")
            if event["result"] is None:
                if ("votes" not in event or type(event["settlement_count"]) is not int
                        or event["settlement_count"] != 0 or event["settled_at"] is not None):
                    raise ValueError("invalid open event")
                if event_id != state["event_id"]:
                    raise ValueError("non-current open event")
            else:
                result = event["result"]
                if not isinstance(result, dict) or type(result["winner"]) is not int or result["winner"] not in (0, 1, 2):
                    raise ValueError("invalid event result")
                counts = result["counts"]
                if not isinstance(counts, list) or len(counts) != 2:
                    raise ValueError("invalid event counts")
                for count in counts:
                    counter(count, "event count")
                if type(event["settlement_count"]) is not int or event["settlement_count"] != 1:
                    raise ValueError("invalid settlement count")
                timestamp(event["settled_at"], "event settled_at")
        if state["reaction"] is not None:
            reaction = state["reaction"]
            if not isinstance(reaction, dict) or not isinstance(reaction["id"], str) or not isinstance(reaction["text"], str):
                raise ValueError("invalid reaction")
            timestamp(reaction["at"], "reaction at")
        if state["message"] is not None:
            message = state["message"]
            if not isinstance(message, dict) or not all(isinstance(message[key], str) for key in ("nickname", "text")):
                raise ValueError("invalid message")
        json.dumps(state, allow_nan=False)

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="work-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            # Windows does not expose O_DIRECTORY. POSIX needs the rename itself
            # flushed as well as the file contents for power-loss durability.
            if hasattr(os, "O_DIRECTORY"):
                try:
                    directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except OSError as error:
                    # The rename is already committed. Publish the same state in
                    # memory and retry directory durability on the next tick.
                    LOGGER.warning("Work state replaced but directory fsync failed (%s)", error)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _settle(state, now):
        cursor = state["settled_at"]
        work_now = max(cursor, now)  # A backwards adjustment cannot repay time.
        frozen = max(0, work_now - cursor - MAX_SETTLEMENT_GAP)
        if frozen:
            cursor += frozen
            if state["ends_at"] is not None:
                state["ends_at"] += frozen
        while cursor < work_now:
            if state["day"] != _day(cursor):
                state.update(day=_day(cursor), salary=0.0, focus_seconds=0.0, slack_count=0)
            boundary = min(work_now, _next_midnight(cursor))
            if state["ends_at"] is not None:
                boundary = min(boundary, state["ends_at"])
            elapsed = max(0, boundary - cursor)
            if state["status"] == "working":
                state["focus_seconds"] += elapsed
                state["salary"] += elapsed * PAY_PER_SECOND * state["speed_multiplier"]
            cursor = boundary
            if state["ends_at"] is not None and cursor >= state["ends_at"]:
                if state["status"] == "working":
                    state.update(status="resting", ends_at=cursor + REST_SECONDS)
                    state["slack_count"] += 1
                else:
                    state.update(status="working", ends_at=cursor + WORK_SECONDS)
                    state["round"] += 1
        if state["day"] != _day(work_now):
            state.update(day=_day(work_now), salary=0.0, focus_seconds=0.0, slack_count=0)
        state["settled_at"] = work_now
        event = state["events"].get(state["event_id"])
        if event and event["result"] is None and now >= event["ends_at"]:
            counts = list(event["votes"].values())
            first, second = counts.count(1), counts.count(2)
            winner = 1 if first > second else 2 if second > first else 0
            text = {0: "票数打平，先按原计划完成这一轮。", 1: "收到，下一轮先整理任务清单。", 2: "收到，下一轮做一次桌面巡检。"}[winner]
            event.update(result={"winner": winner, "counts": [first, second]},
                         settled_at=now, settlement_count=1)
            state["reaction"] = {"id": event["id"], "text": text, "at": now}
        WorkState._prune_events(state)

    @staticmethod
    def _prune_events(state):
        history = []
        for event_id, event in state["events"].items():
            if event["result"] is not None:
                event.pop("votes", None)
                if event_id != state["event_id"]:
                    history.append(event_id)
        for event_id in history[:-EVENT_HISTORY_LIMIT]:
            # Pruned ids become unknown. Retrying an id is idempotent only while
            # its event is retained. Callers should generate fresh ids otherwise.
            del state["events"][event_id]

    def _transaction(self, change, persist=True):
        with self.lock:
            state = deepcopy(self.state)
            self._settle(state, self.clock())
            result = change(state)
            if persist:
                self._save(state)
            self.state = state
            return result

    @staticmethod
    def _view(state):
        event = deepcopy(state["events"].get(state["event_id"]))
        if event:
            votes = list(event.pop("votes", {}).values())
            event["counts"] = (list(event["result"]["counts"]) if event["result"] is not None
                               else [votes.count(1), votes.count(2)])
        duration = WORK_SECONDS if state["status"] == "working" else REST_SECONDS
        if state["status"] == "paused":
            duration = WORK_SECONDS if state["resume_status"] == "working" else REST_SECONDS
        return {
            "server_now": state["settled_at"], "day": state["day"],
            "salary": round(state["salary"], 2), "salary_goal": SALARY_GOAL,
            "focus_seconds": round(state["focus_seconds"], 3),
            "slack_count": state["slack_count"], "speed_multiplier": state["speed_multiplier"],
            "status": state["status"], "ends_at": state["ends_at"],
            "paused_remaining": state["paused_remaining"], "round_seconds": duration,
            "round": state["round"], "rule": RULES[(state["round"] - 1) % len(RULES)],
            "event": event, "reaction": deepcopy(state["reaction"]),
            "message": deepcopy(state["message"]),
        }

    def snapshot(self):
        return self._transaction(self._view, persist=False)

    def checkpoint(self):
        return self._transaction(self._view)

    def control(self, action):
        if action not in ("pause", "resume"):
            raise ValueError("action must be pause or resume")

        def change(state):
            now = state["settled_at"]
            if action == "pause" and state["status"] != "paused":
                state.update(resume_status=state["status"], status="paused",
                             paused_remaining=max(0, state["ends_at"] - now), ends_at=None)
            elif action == "resume" and state["status"] == "paused":
                state.update(status=state["resume_status"],
                             ends_at=now + state["paused_remaining"], paused_remaining=None)
            return self._view(state)
        return self._transaction(change)

    def start_event(self, event_id=None):
        if event_id is None or event_id == "":
            event_id = uuid.uuid4().hex
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", event_id):
            raise ValueError("invalid event_id")

        def change(state):
            if event_id in state["events"]:
                return self._view(state)  # Retained events are never reopened.
            current = state["events"].get(state["event_id"])
            if current and current["result"] is None:
                return self._view(state)
            state["events"][event_id] = {
                "id": event_id, "ends_at": state["settled_at"] + VOTE_SECONDS,
                "duration": VOTE_SECONDS, "votes": {}, "result": None,
                "settlement_count": 0, "settled_at": None,
                "question": "下一轮，想看工位完成哪项任务？",
                "options": ["整理任务清单", "桌面巡检"],
            }
            state["event_id"] = event_id
            self._prune_events(state)
            return self._view(state)
        return self._transaction(change)

    def vote(self, event_id, user_id, option):
        if not isinstance(user_id, str) or len(user_id) > 200:
            raise ValueError("user_id is required (max 200 characters)")
        # Local demo identities only. Platform integration must supply a trusted
        # platform uid. Strip invisible formatting before compatibility folding.
        user_id = "".join(char for char in user_id
                          if unicodedata.category(char) != "Cf" and char != "\u034f")
        user_id = unicodedata.normalize("NFKC", user_id).strip().casefold()
        if not user_id:
            raise ValueError("user_id is required (max 200 characters)")
        if type(option) is not int or option not in (1, 2):
            raise ValueError("option must be 1 or 2")
        if not isinstance(event_id, str):
            raise ValueError("event_id is required")
        voter = hashlib.sha256(user_id.encode("utf-8")).hexdigest()

        def change(state):
            event = state["events"].get(event_id)
            if event is None:
                reason = "unknown_event"
            elif self.clock() >= event["ends_at"] or event["result"] is not None:
                reason = "closed"
            elif voter in event["votes"]:
                reason = "duplicate"
            else:
                event["votes"][voter] = option
                reason = "accepted"
            return {"accepted": reason == "accepted", "reason": reason, "work": self._view(state)}
        return self._transaction(change)

    def set_message(self, nickname, text):
        if not isinstance(nickname, str) or not isinstance(text, str):
            raise ValueError("nickname and text must be strings")

        def change(state):
            state["message"] = {"nickname": nickname[:80], "text": text[:500]}
            return self._view(state)
        return self._transaction(change)
