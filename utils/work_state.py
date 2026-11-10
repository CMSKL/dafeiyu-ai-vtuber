"""Durable, server-authoritative work simulation. Standard library only.

One service owns this file. Each transaction checkpoints atomically before it
becomes visible. Process downtime freezes the work clock, while votes keep their
wall-clock deadline. Timestamps are Unix seconds. Daily totals use UTC+08:00.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid


WORK_SECONDS = 60
REST_SECONDS = 15
VOTE_SECONDS = 30
PAY_PER_SECOND = 10
SALARY_GOAL = 6000
RULES = ("完成一轮后，记得让眼睛休息", "休息时间也要坐稳工位", "暂停后计时与工资一起停下")
LOCAL_TZ = timezone(timedelta(hours=8))


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
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("version") != 1:
                raise ValueError("unsupported work state version")
            # Never pay for process downtime or replay already checkpointed time.
            if state["ends_at"] is not None:
                remaining = max(0, state["ends_at"] - state["settled_at"])
                state["ends_at"] = now + remaining
            state["settled_at"] = now
        else:
            state = {
                "version": 1, "day": _day(now), "salary": 0.0,
                "focus_seconds": 0.0, "slack_count": 0, "speed_multiplier": 1.0,
                "status": "working", "resume_status": "working",
                "ends_at": now + WORK_SECONDS, "paused_remaining": None,
                "settled_at": now, "round": 1, "events": {},
                "event_id": None, "reaction": None, "message": None,
            }
        self.state = state
        self.snapshot()

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="work-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _settle(state, now):
        cursor = state["settled_at"]
        now = max(cursor, now)  # A backwards wall-clock adjustment cannot repay time.
        while cursor < now:
            if state["day"] != _day(cursor):
                state.update(day=_day(cursor), salary=0.0, focus_seconds=0.0, slack_count=0)
            boundary = min(now, _next_midnight(cursor))
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
        if state["day"] != _day(now):
            state.update(day=_day(now), salary=0.0, focus_seconds=0.0, slack_count=0)
        state["settled_at"] = now
        event = state["events"].get(state["event_id"])
        if event and event["result"] is None and now >= event["ends_at"]:
            counts = list(event["votes"].values())
            first, second = counts.count(1), counts.count(2)
            winner = 1 if first > second else 2 if second > first else 0
            text = {0: "票数打平，先按原计划完成这一轮。", 1: "收到，下一轮先整理任务清单。", 2: "收到，下一轮做一次桌面巡检。"}[winner]
            event.update(result={"winner": winner, "counts": [first, second]},
                         settled_at=event["ends_at"], settlement_count=1)
            state["reaction"] = {"id": event["id"], "text": text, "at": event["ends_at"]}

    def _transaction(self, change):
        with self.lock:
            state = deepcopy(self.state)
            self._settle(state, self.clock())
            result = change(state)
            self._save(state)
            self.state = state
            return result

    @staticmethod
    def _view(state):
        event = deepcopy(state["events"].get(state["event_id"]))
        if event:
            votes = list(event.pop("votes").values())
            event["counts"] = [votes.count(1), votes.count(2)]
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
        event_id = event_id or uuid.uuid4().hex
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", event_id):
            raise ValueError("invalid event_id")

        def change(state):
            if event_id in state["events"]:
                return self._view(state)  # Retried submissions never reopen an event.
            current = state["events"].get(state["event_id"])
            if current and current["result"] is None:
                raise ValueError("an inspection is already running")
            state["events"][event_id] = {
                "id": event_id, "ends_at": state["settled_at"] + VOTE_SECONDS,
                "duration": VOTE_SECONDS, "votes": {}, "result": None,
                "settlement_count": 0, "settled_at": None,
                "question": "下一轮，想看工位完成哪项任务？",
                "options": ["整理任务清单", "桌面巡检"],
            }
            state["event_id"] = event_id
            return self._view(state)
        return self._transaction(change)

    def vote(self, event_id, user_id, option):
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 200:
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
            elif state["settled_at"] >= event["ends_at"] or event["result"] is not None:
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
