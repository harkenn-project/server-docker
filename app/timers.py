"""Timers, alarms and notifications. Timers and alarms live on the speakers (they ring on time without Harkenn, and
survive restarts); Harkenn sets and cancels them, and knows what each speaker has from what it reports. Notifications
are cards a speaker shows (an envelope stays on its screen until someone looks), sent by agents or the assistant.

The assistant uses one marker, any number of times in a reply (never spoken):
    [[timer {"start": "9m", "label": "pasta"}]]                 a timer
    [[timer {"alarm": "07:30", "days": "weekdays", "label": "work"}]]   an alarm (also "date": "2026-11-05")
    [[timer {"cancel": "pasta"}]]   by label or id, or "all", "timers", "alarms"
    [[timer {"show": true}]]        the countdown (or the alarms) on the screen
    [[timer {"stop": true}]]        stop what's ringing
with "speaker": "<name>" for another speaker than the one asked. It's told what's set on that speaker in every request.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

MARK_RE = re.compile(r"\[\[\s*timer\b", re.I)
DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", re.I)
EVENT_KEEP_S = 30 * 60  # what happened to timers stays in the assistant's context this long
MAX_NOTES = 5
DAY_BITS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

PROMPT = """

Timers and alarms (they live on the speaker and ring there): add markers to your reply, never spoken, one per action:
[[timer {"start": "9m", "label": "pasta"}]] / [[timer {"alarm": "07:30", "days": "weekdays", "label": "work"}]] \
(days: daily, weekdays, weekends, or "mon,wed"; leave out for once; "date": "YYYY-MM-DD" for one day) / \
[[timer {"cancel": "pasta"}]] (a label, an id, "all", "timers" or "alarms") / [[timer {"show": true}]] / \
[[timer {"stop": true}]] (stop what's ringing). Add "speaker": "<name>" for another speaker. Say briefly what you set.
"""


def duration_s(value: Any) -> int:
    """Seconds from 540, "540", "9m", "1h30m", "90 seconds" or "1.5 hours"; 0 if it can't be read."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip().lower()
    if text.isdigit():
        return int(text)
    total = 0.0
    for amount, unit in DURATION_RE.findall(text):
        total += float(amount) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    if not total and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):  # 9:00 or 1:30:00
        parts = [int(p) for p in text.split(":")]
        total = parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
    return int(total)


def split_markers(reply: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Take every timer marker out of a reply: (the reply without them, the actions, problems)."""
    actions, problems = [], []
    while True:
        m = MARK_RE.search(reply)
        if not m:
            return reply, actions, problems
        close = reply.find("]]", m.end())
        brace = reply.find("{", m.end())
        if brace < 0 or (0 <= close < brace):
            reply = reply[:m.start()] + (reply[close + 2:] if close >= 0 else "")
            problems.append("a timer marker without an action")
            continue
        try:
            spec, end = json.JSONDecoder().raw_decode(reply, brace)
        except json.JSONDecodeError as e:
            reply = reply[:m.start()] + (reply[close + 2:] if close >= 0 else "")
            problems.append(f"invalid timer JSON: {e}")
            continue
        close = reply.find("]]", end)
        reply = reply[:m.start()] + (reply[close + 2:] if close >= 0 else reply[end:])
        if isinstance(spec, dict):
            actions.append(spec)


def days_mask(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value & 0x7F
    text = (",".join(map(str, value)) if isinstance(value, list) else str(value or "")).lower()
    if "daily" in text or "every" in text:
        return 0x7F
    if "weekday" in text:
        return 0x3E
    if "weekend" in text:
        return 0x41
    return sum(1 << bit for name, bit in DAY_BITS.items() if name in text)


def message(action: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The speaker's `timer` message for an action (marker or tool), or (None, the problem)."""
    label = str(action.get("label") or "").strip()[:40]
    if action.get("start") not in (None, "", False) or action.get("seconds"):
        seconds = duration_s(action.get("start") or action.get("seconds"))
        if not 1 <= seconds <= 24 * 3600:
            return None, f"can't set a timer for {action.get('start') or action.get('seconds')!r}"
        return {"type": "timer", "action": "set", "kind": "timer", "seconds": seconds, "label": label}, ""
    if action.get("alarm") or action.get("time"):
        at = str(action.get("alarm") or action.get("time")).strip().lower()
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", at.replace(".", ""))
        if not m:
            return None, f"can't read the alarm time {at!r}"
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3) == "pm" and hour < 12:
            hour += 12
        if m.group(3) == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None, f"can't read the alarm time {at!r}"
        out = {"type": "timer", "action": "set", "kind": "alarm", "time": f"{hour:02d}:{minute:02d}", "label": label,
               "days": days_mask(action.get("days"))}
        date = str(action.get("date") or "").strip()
        if date:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                return None, f"the alarm date should be YYYY-MM-DD, not {date!r}"
            out["date"] = date
        return out, ""
    if action.get("cancel") not in (None, "", False):
        return {"type": "timer", "action": "cancel", "what": str(action["cancel"]).strip()[:40]}, ""
    if action.get("show"):
        return {"type": "timer", "action": "show"}, ""
    if action.get("stop"):
        return {"type": "timer", "action": "stop"}, ""
    return None, f"a timer marker with nothing to do: {json.dumps(action)[:80]}"


def describe_left(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds // 60 % 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds} s"


DAY_SETS = {0x7F: "daily", 0x3E: "weekdays", 0x41: "weekends"}


def describe_item(item: dict[str, Any], now: float) -> str:
    left = describe_left(int(item.get("due", now) - now))
    name = f'"{item["label"]}" ' if item.get("label") else ""
    if item.get("kind") == "alarm":
        days = int(item.get("days") or 0)
        when = DAY_SETS.get(days) or ", ".join(n.title() for n, b in DAY_BITS.items() if days & (1 << b)) or "once"
        return f"alarm {name}(id {item.get('id')}) at {item.get('time')} {when}, next in {left}"
    total = describe_left(int(item.get("duration") or 0))
    return f"timer {name}(id {item.get('id')}) {left} left of {total}"


@dataclass
class Board:
    """What each speaker has: its timers and alarms (as it last reported them), what happened to them lately, and its
    notifications."""
    items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    ringing: dict[str, int] = field(default_factory=dict)
    reported_at: dict[str, float] = field(default_factory=dict)
    events: dict[str, list[tuple[float, str]]] = field(default_factory=dict)
    notes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def report(self, device_id: str, msg: dict[str, Any]) -> None:
        items = [i for i in msg.get("items") or [] if isinstance(i, dict)][:20]
        now = time.time()
        for i in items:  # the speaker's clock may be off: trust its remaining time over its due time
            if isinstance(i.get("remaining_s"), (int, float)):
                i["due"] = now + i["remaining_s"]
        self.items[device_id] = items
        self.ringing[device_id] = int(msg.get("ringing") or 0)
        self.reported_at[device_id] = now

    def event(self, device_id: str, name: str, data: dict[str, Any]) -> None:
        what = {"timer_ringing": "is ringing", "timer_stopped": "was stopped", "timer_snoozed": "was snoozed",
                "timer_missed": "rang unanswered and stopped by itself"}.get(name)
        if not what:
            return
        label = f' "{data.get("label")}"' if data.get("label") else ""
        how = f' ({data["how"]})' if data.get("how") else ""
        now = time.time()
        events = [e for e in self.events.get(device_id, []) if now - e[0] < EVENT_KEEP_S]
        events.append((now, f"{data.get('kind', 'timer')}{label} {what}{how}"))
        self.events[device_id] = events[-6:]

    def notify(self, device_id: str, title: str, text: str) -> dict[str, Any]:
        note = {"title": title[:60], "text": text[:300], "at": time.time()}
        notes = self.notes.setdefault(device_id, [])
        notes.append(note)
        del notes[:-MAX_NOTES]
        return note

    def seen(self, device_id: str) -> None:
        self.notes.pop(device_id, None)

    def forget(self, device_id: str) -> None:
        for d in (self.items, self.ringing, self.reported_at, self.events, self.notes):
            d.pop(device_id, None)

    def summary(self, device_id: str) -> str:
        """For the assistant, on the newest message: what's set, what happened lately, unread notifications."""
        now = time.time()
        items = self.items.get(device_id)
        parts = []
        if items is not None:
            parts.append("Timers and alarms on this speaker: " +
                         ("; ".join(describe_item(i, now) for i in items) if items else "none") + ".")
        events = [f"{e} {describe_left(int(now - t))} ago" for t, e in self.events.get(device_id, [])
                  if now - t < EVENT_KEEP_S]
        if events:
            parts.append("Lately: " + "; ".join(events) + ".")
        notes = self.notes.get(device_id) or []
        if notes:
            parts.append("Unread notifications on its screen: " + "; ".join(
                f'"{n["title"] + ": " if n["title"] else ""}{n["text"]}" ({describe_left(int(now - n["at"]))} ago)'
                for n in notes) + ".")
        return (" " + " ".join(parts)) if parts else ""
