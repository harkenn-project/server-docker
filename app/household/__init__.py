"""Household modules: what a plain chat model needs to be useful at home, kept by Harkenn. An agent such as Hermes
has its own memory and tools; it reaches these through the MCP server instead (mcp_server.py).

    memories    things to remember, for the household or one person (on by default)
    todo        the household's to-do list
    shopping    the shopping list
    reminders   said (and shown) on a speaker at a time, or rung like an alarm; once or repeating
    calendar    Harkenn's own events, plus read-only subscriptions (ICS links from Google, iCloud, Outlook...)
    messages    for someone in the household, passed on when the voiceprints recognize them
    chores      a rota: whose turn it is, with an optional daily reminder
    briefing    "good morning": the weather, the day's events, reminders, to-dos, chores and news in one answer
    news        headlines from RSS or Atom feeds

The assistant uses markers, as for timers (timers.py), never spoken:
    [[memory {"remember": "...", "for": "me"}]]    [[shopping {"add": ["milk"]}]]    [[reminder {...}]]  ...
It's told what it needs (what's remembered, the lists, today's events...) on the newest message, only when that
changed (brain.Conversations.news). Looking further (news, the briefing, other days of the calendar) takes a second
request: the first reply is only the marker, and Harkenn asks again with what it found.

Personal things (memories, reminders, events) belong to the person the voiceprints recognized ("for": "me"), or to
the household. Times are local, in the time zone of the group new speakers join.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from .. import timers
from . import feeds

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("harkenn.household")

SETTINGS_KEY = "household"
TICK_S = 20
DONE_KEEP_S = 30 * 86400  # done to-dos, fired reminders and delivered messages are kept this long
MAX_ITEMS = 500           # per module
MAX_IN_NOTE = 40          # items told to the assistant per module

MODULES: dict[str, dict[str, Any]] = {
    "memories": {"title": "Memories", "marker": "memory", "default": True,
                 "about": "Things to remember, for the household or for one person (\"remember that my sister is "
                          "Anna\")."},
    "todo": {"title": "To-do", "marker": "todo", "about": "The household's to-do list, with due dates."},
    "shopping": {"title": "Shopping list", "marker": "shopping", "about": "Add to it from any speaker; tick things "
                                                                           "off at the shop from this page."},
    "reminders": {"title": "Reminders", "marker": "reminder",
                  "about": "Said and shown on a speaker at a time, or rung like an alarm; once or repeating."},
    "calendar": {"title": "Calendar", "marker": "calendar",
                 "about": "Harkenn's own events, plus your Google, iCloud or Outlook calendars (read-only, by their "
                          "ICS links)."},
    "messages": {"title": "Messages", "marker": "message",
                 "about": "\"Tell Jules dinner's ready\": passed on when the voiceprints recognize Jules, or shown "
                          "on a speaker straight away."},
    "chores": {"title": "Chores", "marker": "chore", "about": "A rota: whose turn it is, with an optional reminder."},
    "briefing": {"title": "Morning briefing", "marker": "briefing",
                 "about": "\"Good morning\" (or stopping a morning alarm): the weather, the day's events, reminders, "
                          "to-dos, chores and news in one short answer."},
    "news": {"title": "News headlines", "marker": "news", "about": "Headlines from the RSS or Atom feeds you add."},
}
MARKERS = {m["marker"]: name for name, m in MODULES.items()}
MARKER_RE = re.compile(r"\[\[\s*(" + "|".join(MARKERS) + r")\b", re.I)
MAIN_FIELD = {"memories": "text", "todo": "text", "shopping": "text", "reminders": "text", "calendar": "title",
              "messages": "text", "chores": "name"}
PERSONAL = {"memories", "reminders", "calendar"}  # have an owner: a person, or the household ("")
BRIEFING_SECTIONS = {"weather": "Weather", "calendar": "Today's events", "reminders": "Reminders",
                     "todo": "To-dos due", "chores": "Chores", "messages": "Messages", "news": "News"}
REPEATS = {"reminders": ("", "daily", "weekdays", "weekly", "monthly"),
           "calendar": ("", "daily", "weekly", "monthly", "yearly")}


class Link(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=8, max_length=1000, pattern=r"(?i)^(https?|webcal)://")


class HouseholdSettings(BaseModel):
    enabled: dict[str, bool] = Field(default_factory=dict)
    calendars: list[Link] = Field(default_factory=list, max_length=10)
    news_feeds: list[Link] = Field(default_factory=list, max_length=10)
    briefing_sections: list[str] = Field(default_factory=lambda: list(BRIEFING_SECTIONS))
    briefing_groups: dict[str, list[str]] = Field(default_factory=dict, description="group id -> its own sections")
    briefing_after_alarm: bool = Field(True, description="stopping an alarm before 11:00 gives the briefing")
    reminders_speak: bool = Field(True, description="say reminders aloud, as well as showing them")


class Problem(ValueError):
    pass


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bool(value: Any) -> bool:
    return value is True or str(value).lower() in ("true", "1", "yes", "on")


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}):(\d{2})")
TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def parse_time_of_day(text: str) -> tuple[int, int] | None:
    m = TIME_RE.match(text.strip().lower().replace(".", ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    if m.group(3) == "am" and hour == 12:
        hour = 0
    return (hour, minute) if hour < 24 and minute < 60 else None


def parse_when(value: Any, now: datetime, date_only_ok: bool = False) -> str:
    """A local time as "YYYY-MM-DDTHH:MM" (or "YYYY-MM-DD" when allowed) from "2026-11-04 19:00", "19:00", "7pm"
    (today, or tomorrow once it's past) or "2026-11-04". Raises Problem."""
    text = str(value or "").strip()
    m = DATETIME_RE.match(text)
    if m:
        hm = parse_time_of_day(f"{m.group(2)}:{m.group(3)}")
        if hm:
            return f"{m.group(1)}T{hm[0]:02d}:{hm[1]:02d}"
    if DATE_RE.match(text):
        if date_only_ok:
            return text
        return f"{text}T09:00"
    hm = parse_time_of_day(text)
    if hm:
        at = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        return at.strftime("%Y-%m-%dT%H:%M")
    raise Problem(f"can't read the time {text!r}")


def local_dt(value: str) -> datetime:
    return datetime.fromisoformat(value) if "T" in value else datetime.fromisoformat(value + "T00:00")


def say_when(value: str, now: datetime) -> str:
    d = local_dt(value)
    day = feeds.day_label(d.date(), now.date())
    return day if "T" not in value else f"{day} {d:%H:%M}"


def next_repeat(due: datetime, repeat: str) -> datetime:
    if repeat == "daily":
        return due + timedelta(days=1)
    if repeat == "weekdays":
        nxt = due + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    if repeat == "weekly":
        return due + timedelta(days=7)
    if repeat == "monthly":
        month = due.month % 12 + 1
        year = due.year + (due.month == 12)
        for day in (due.day, 30, 29, 28):
            try:
                return due.replace(year=year, month=month, day=day)
            except ValueError:
                continue
    return due


def chore_due_today(chore: dict[str, Any], today: date, now_ts: float) -> bool:
    every = chore.get("every") or "daily"
    if every == "weekly":
        return now_ts - float(chore.get("last_done") or 0) > 6 * 86400
    mask = timers.days_mask(every) or 0x7F
    return bool(mask & (1 << ((today.weekday() + 1) % 7)))


def whose_turn(chore: dict[str, Any]) -> str:
    people = chore.get("people") or []
    return people[int(chore.get("turn") or 0) % len(people)] if people else ""


PROMPTS = {
    "memories": '[[memory {"remember": "Marika\'s sister is Anna", "for": "me"}]] keeps something for later ("for": '
                '"me" is the person talking, when you know who; "household" is everyone). [[memory {"forget": '
                '"sister"}]] forgets what matches. You\'re told what\'s remembered. Remember only what someone asks '
                'you to, or clearly wants kept.',
    "todo": '[[todo {"add": "call the plumber", "due": "2026-11-05"}]] (due is optional) / [[todo {"done": '
            '"plumber"}]] / [[todo {"remove": "plumber"}]].',
    "shopping": '[[shopping {"add": ["milk", "2 lemons"]}]] / [[shopping {"got": "milk"}]] / [[shopping {"remove": '
                '"milk"}]] / [[shopping {"clear": true}]] (takes off what\'s been got).',
    "reminders": '[[reminder {"text": "take the bins out", "at": "2026-11-04 19:00", "repeat": "weekly", "for": '
                 '"me"}]] ("in": "20m" instead of "at"; repeat: daily, weekdays, weekly, monthly, or leave it out for '
                 'once; "alarm": true to ring like an alarm; "speaker": a speaker\'s name or "all", otherwise this '
                 'one) / [[reminder {"cancel": "bins"}]].',
    "calendar": '[[calendar {"add": "Dentist", "start": "2026-11-06 14:30", "end": "15:30", "where": "Main St", '
                '"for": "me"}]] ("start": "YYYY-MM-DD" for all day; "repeat": daily, weekly, monthly or yearly, e.g. '
                'birthdays) / [[calendar {"remove": "Dentist", "date": "2026-11-06"}]]. You\'re told today\'s and '
                'tomorrow\'s events; for other days reply with only [[calendar {"find": "2026-11-10", "to": '
                '"2026-11-16"}]] and you\'ll be told what\'s on, then answer.',
    "messages": '[[message {"to": "Jules", "text": "dinner is ready"}]] keeps a message for someone, passed on when '
                'they next talk to a speaker (add "speaker": "Kitchen" or "all" to also show it there now). When '
                'you\'re told of messages waiting for the person talking, pass them on first, in your own words. '
                'Pass a message only to the person it\'s for; then add [[message {"delivered": "Jules"}]].',
    "chores": '[[chore {"done": "dishes", "by": "Jules"}]] when someone has done a chore (the turn moves on). You\'re '
              'told whose turn each chore is.',
    "news": 'For the news, reply with only [[news]] (or [[news {"topic": "hockey"}]]) and you\'ll get the latest '
            'headlines; then tell a few briefly, and more if asked.',
    "briefing": 'When someone says good morning or asks for their briefing, reply with only [[briefing]] and you\'ll '
                'get the day\'s details; then give a short, warm briefing.',
}


class Household:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.calendar_cache = feeds.Cache(feeds.parse_ics)
        self.news_cache = feeds.Cache(feeds.parse_feed)
        self._task: asyncio.Task | None = None

    # settings

    def settings(self) -> HouseholdSettings:
        raw = self.ctx.db.get_setting(SETTINGS_KEY)
        try:
            return HouseholdSettings.model_validate(raw) if raw else HouseholdSettings()
        except ValidationError as e:
            log.warning("stored household settings are invalid, using defaults: %s", e)
            return HouseholdSettings()

    def save_settings(self, s: HouseholdSettings) -> None:
        s.enabled = {k: bool(v) for k, v in s.enabled.items() if k in MODULES}
        s.briefing_sections = [x for x in s.briefing_sections if x in BRIEFING_SECTIONS]
        s.briefing_groups = {g: [x for x in v if x in BRIEFING_SECTIONS] for g, v in s.briefing_groups.items()}
        self.ctx.db.set_setting(SETTINGS_KEY, s.model_dump())

    def enabled(self, module: str, s: HouseholdSettings | None = None) -> bool:
        s = s or self.settings()
        return s.enabled.get(module, MODULES[module].get("default", False))

    def enabled_modules(self) -> list[str]:
        s = self.settings()
        return [m for m in MODULES if self.enabled(m, s)]

    def tz(self) -> tzinfo:
        from .. import groups
        group = groups.get(self.ctx.db, groups.default_id(self.ctx.db)) or {}
        name = (group.get("config") or {}).get("timezone_name") or ""
        if name:
            try:
                return ZoneInfo(name)
            except Exception:
                pass
        return datetime.now().astimezone().tzinfo

    def now(self) -> datetime:
        return datetime.now(self.tz()).replace(tzinfo=None)

    # people

    def people(self) -> list[dict[str, Any]]:
        return [{"id": p["id"], "name": p["name"]} for p in self.ctx.voice.voiceprints.people()]

    def person_name(self, pid: str) -> str:
        return next((p["name"] for p in self.people() if p["id"] == pid), "")

    def owner_for(self, value: Any, person: str) -> str:
        """The owner a marker means: "me" (the person talking, or the household when no one was recognized),
        "household", or a person's name."""
        text = str(value or "").strip().lower()
        if text in ("", "household", "everyone", "us", "all", "home"):
            return ""
        if text in ("me", "mine", "my", "myself"):
            return person
        found = next((p["id"] for p in self.people() if p["name"].lower() == text or p["id"] == text), None)
        if found is None:
            raise Problem(f"no one called {value!r} on the People page")
        return found

    # items

    def items(self, module: str, owner: str | None = None) -> list[dict[str, Any]]:
        out = self.ctx.db.household_items(module)
        return out if owner is None else [i for i in out if i["owner"] in ("", owner)]

    def add(self, module: str, data: dict[str, Any], owner: str = "") -> dict[str, Any]:
        if len(self.ctx.db.household_items(module)) >= MAX_ITEMS:
            raise Problem(f"the {MODULES[module]['title'].lower()} is full ({MAX_ITEMS})")
        now = time.time()
        item = {"id": secrets.token_hex(5), "module": module, "owner": owner if module in PERSONAL else "",
                "data": self.normalize(module, data), "created_at": now, "updated_at": now}
        self.ctx.db.put_household_item(item)
        self.ctx.hub.publish({"type": "household", "module": module})
        return item

    def update(self, item: dict[str, Any], data: dict[str, Any], owner: str | None = None) -> dict[str, Any]:
        item = {**item, "data": self.normalize(item["module"], {**item["data"], **data}),
                "updated_at": time.time()}
        if owner is not None and item["module"] in PERSONAL:
            item["owner"] = owner
        self.ctx.db.put_household_item(item)
        self.ctx.hub.publish({"type": "household", "module": item["module"]})
        return item

    def delete(self, item: dict[str, Any]) -> None:
        self.ctx.db.delete_household_item(item["id"])
        self.ctx.hub.publish({"type": "household", "module": item["module"]})

    def find(self, module: str, what: Any, owner: str | None = None,
             where=lambda i: True) -> list[dict[str, Any]]:
        """Items whose id is `what`, or whose text contains it (whole match first)."""
        text = _clean(what, 200).lower()
        if not text:
            return []
        items = [i for i in self.items(module, owner) if where(i)]
        by_id = [i for i in items if i["id"] == text]
        if by_id:
            return by_id
        field = MAIN_FIELD[module]
        exact = [i for i in items if str(i["data"].get(field, "")).lower() == text]
        return exact or [i for i in items if text in str(i["data"].get(field, "")).lower()]

    def normalize(self, module: str, d: dict[str, Any]) -> dict[str, Any]:
        """What's stored for an item, checked and cleaned. Raises Problem."""
        now = self.now()
        field = MAIN_FIELD.get(module)
        if field and not _clean(d.get(field), 300):
            raise Problem(f"{module}: the {field} is empty")
        if module == "memories":
            return {"text": _clean(d["text"], 300)}
        if module == "todo":
            due = str(d.get("due") or "").strip()
            if due and not DATE_RE.match(due):
                due = parse_when(due, now, date_only_ok=True)[:10]
            done = _bool(d.get("done"))
            return {"text": _clean(d["text"], 200), "due": due, "done": done,
                    "done_at": (d.get("done_at") or time.time()) if done else None}
        if module == "shopping":
            got = _bool(d.get("got"))
            return {"text": _clean(d["text"], 100), "got": got, "got_at": (d.get("got_at") or time.time()) if got
                    else None}
        if module == "reminders":
            repeat = str(d.get("repeat") or "").lower()
            if repeat not in REPEATS["reminders"]:
                raise Problem(f"reminders repeat daily, weekdays, weekly or monthly, not {repeat!r}")
            return {"text": _clean(d["text"], 200), "due": parse_when(d.get("due"), now), "repeat": repeat,
                    "alarm": _bool(d.get("alarm")), "speaker": _clean(d.get("speaker"), 40),
                    "done": _bool(d.get("done")), "fired_at": d.get("fired_at")}
        if module == "calendar":
            start = parse_when(d.get("start"), now, date_only_ok=True)
            end = str(d.get("end") or "").strip()
            if end:
                hm = parse_time_of_day(end)
                end = (f"{start[:10]}T{hm[0]:02d}:{hm[1]:02d}" if hm and "T" in start
                       else parse_when(end, now, date_only_ok=True))
                if local_dt(end) < local_dt(start):
                    raise Problem("the event ends before it starts")
            repeat = str(d.get("repeat") or "").lower()
            if repeat not in REPEATS["calendar"]:
                raise Problem(f"events repeat daily, weekly, monthly or yearly, not {repeat!r}")
            return {"title": _clean(d["title"], 120), "start": start, "end": end, "where": _clean(d.get("where"), 120),
                    "repeat": repeat, "notes": _clean(d.get("notes"), 500)}
        if module == "messages":
            if not _clean(d.get("to"), 40):
                raise Problem("a message needs someone to go to")
            return {"to": _clean(d["to"], 40), "from": _clean(d.get("from"), 40), "text": _clean(d["text"], 300),
                    "delivered_at": d.get("delivered_at")}
        if module == "chores":
            people = d.get("people") or []
            if isinstance(people, str):
                people = people.split(",")
            people = [p for p in (_clean(x, 40) for x in people) if p][:12]
            every = str(d.get("every") or "daily").lower().strip()
            if every not in ("daily", "weekly", "weekdays", "weekends") and not timers.days_mask(every):
                raise Problem(f"chores happen daily, weekly, on weekdays or weekends, or on days (mon,thu), not "
                              f"{every!r}")
            remind = str(d.get("remind") or "").strip()
            if remind:
                hm = parse_time_of_day(remind)
                if not hm:
                    raise Problem(f"can't read the reminder time {remind!r}")
                remind = f"{hm[0]:02d}:{hm[1]:02d}"
            return {"name": _clean(d["name"], 60), "people": people, "every": every, "remind": remind,
                    "turn": int(d.get("turn") or 0) % max(1, len(people)), "last_done": d.get("last_done"),
                    "last_by": _clean(d.get("last_by"), 40), "reminded_on": d.get("reminded_on") or ""}
        raise Problem(f"{module} has no items")

    # events, for the calendar and the briefing

    def events(self, frm: datetime, to: datetime, person: str | None = None) -> list[dict[str, Any]]:
        """Local and subscribed events between two local times, soonest first."""
        out = []
        for item in self.items("calendar", person):
            d = item["data"]
            start = local_dt(d["start"])
            length = local_dt(d["end"]) - start if d.get("end") else timedelta(days=1 if "T" not in d["start"] else 0)
            for at in feeds.repeats(start, d.get("repeat", ""), frm, to):
                out.append({"id": item["id"], "title": d["title"], "start": at, "end": at + length,
                            "all_day": "T" not in d["start"], "where": d.get("where", ""), "source": "",
                            "owner": item["owner"]})
        tz = self.tz()
        for link in self.settings().calendars:
            cal = self.calendar_cache.get(link.url)
            if cal is not None:
                out += [{**e, "owner": ""} for e in feeds.ics_events(cal, frm, to, tz, link.name)]
        return sorted(out, key=lambda e: (e["start"], not e["all_day"]))

    def describe_events(self, events: list[dict[str, Any]], now: datetime) -> str:
        parts = []
        for e in events[:MAX_IN_NOTE]:
            when = feeds.day_label(e["start"].date(), now.date())
            if not e["all_day"]:
                when += f" {e['start']:%H:%M}" + (f"-{e['end']:%H:%M}" if e["end"] > e["start"] else "")
            whose = f" ({self.person_name(e['owner'])}'s)" if e.get("owner") else ""
            where = f" at {e['where']}" if e.get("where") else ""
            parts.append(f"{when}: {e['title']}{where}{whose}")
        return "; ".join(parts)

    # the assistant

    def prompt(self) -> str:
        """The instructions for the modules that are on (none for an agent, which uses the MCP tools)."""
        on = [m for m in self.enabled_modules() if m in PROMPTS]
        if not on:
            return ""
        return ("\n\nHousehold (Harkenn keeps these; add markers to your reply, never spoken, one per action, and say "
                "briefly what you did):\n" + "\n".join(f"- {PROMPTS[m]}" for m in on))

    def notes(self, device_id: str, person: str, now: datetime | None = None) -> dict[str, str]:
        """What the assistant should know for a request, in parts (each sent only when it changes)."""
        now = now or self.now()
        on = set(self.enabled_modules())
        name = self.person_name(person)
        out: dict[str, str] = {}
        if "memories" in on:
            mine = self.items("memories", person)
            house = [i["data"]["text"] for i in mine if not i["owner"]][-MAX_IN_NOTE:]
            theirs = [i["data"]["text"] for i in mine if i["owner"] and i["owner"] == person][-MAX_IN_NOTE:]
            parts = []
            if house:
                parts.append("Remembered for the household: " + "; ".join(house) + ".")
            if theirs:
                parts.append(f"Remembered for {name}: " + "; ".join(theirs) + ".")
            out["memories"] = " ".join(parts)
        if "todo" in on:
            todo = [i["data"] for i in self.items("todo") if not i["data"]["done"]]
            out["todo"] = ("To-do: " + "; ".join(t["text"] + (f" (due {say_when(t['due'], now)})" if t["due"] else "")
                                                for t in todo[:MAX_IN_NOTE]) + ".") if todo else ""
        if "shopping" in on:
            items = [i["data"] for i in self.items("shopping")]
            need = [i["text"] for i in items if not i["got"]]
            got = [i["text"] for i in items if i["got"]]
            out["shopping"] = ("Shopping list: " + (", ".join(need[:MAX_IN_NOTE]) or "empty")
                               + (f" (already got: {', '.join(got[:10])})" if got else "") + ".") if items else ""
        if "reminders" in on:
            rems = [i for i in self.items("reminders", person) if not i["data"]["done"]]
            rems.sort(key=lambda i: i["data"]["due"])
            out["reminders"] = ("Reminders set: " + "; ".join(
                f"{i['data']['text']} ({say_when(i['data']['due'], now)}"
                + (f", {i['data']['repeat']}" if i["data"]["repeat"] else "")
                + (", alarm" if i["data"]["alarm"] else "")
                + (f", {self.person_name(i['owner'])}'s" if i["owner"] else "") + ")"
                for i in rems[:MAX_IN_NOTE]) + ".") if rems else ""
        if "calendar" in on:
            today = datetime(now.year, now.month, now.day)
            events = self.events(today, today + timedelta(days=2), person)
            out["calendar"] = ("Calendar: " + self.describe_events(events, now) + ".") if events else \
                "Calendar: nothing today or tomorrow."
        if "messages" in on:
            out["messages"] = self._messages_note(person, name)
        if "chores" in on:
            chores = [i["data"] for i in self.items("chores")]
            out["chores"] = ("Chores: " + "; ".join(
                f"{c['name']}: {whose_turn(c) or 'no one'}'s turn ({c['every']}"
                + (", due today" if chore_due_today(c, now.date(), time.time()) else "")
                + (f", last done by {c['last_by']}" if c.get("last_by") else "") + ")"
                for c in chores[:MAX_IN_NOTE]) + ".") if chores else ""
        return out

    def _messages_note(self, person: str, name: str) -> str:
        waiting = [i for i in self.items("messages") if not i["data"].get("delivered_at")]
        if not waiting:
            return ""
        if name:  # recognized: theirs are passed on now
            theirs = [i for i in waiting if i["data"]["to"].lower() == name.lower()]
            for i in theirs:
                self.update(i, {"delivered_at": time.time()})
            others = sorted({i["data"]["to"] for i in waiting if i not in theirs})
            text = ""
            if theirs:
                text = f"Messages for {name}, who is talking: " + "; ".join(
                    f"from {i['data']['from'] or 'someone'}: \"{i['data']['text']}\"" for i in theirs) + "."
            if others:
                text += f" Messages waiting for: {', '.join(others)}."
            return text.strip()
        return "Messages waiting (pass each on only to the person it's for): " + "; ".join(
            f"for {i['data']['to']} from {i['data']['from'] or 'someone'}: \"{i['data']['text']}\"" for i in waiting) + "."

    # markers

    def split(self, reply: str) -> tuple[str, list[tuple[str, dict[str, Any]]], list[str]]:
        """The reply without household markers, the actions they ask for (module, body), and problems."""
        actions, problems, out, pos = [], [], [], 0
        on = set(self.enabled_modules())
        for m in MARKER_RE.finditer(reply):
            if m.start() < pos:
                continue
            out.append(reply[pos:m.start()])
            module = MARKERS[m.group(1).lower()]
            rest = reply[m.end():]
            body: dict[str, Any] = {}
            stripped = rest.lstrip()
            end = m.end() + len(rest) - len(stripped)
            if stripped.startswith("{"):
                try:
                    body, used = json.JSONDecoder().raw_decode(stripped)
                    end += used
                except ValueError as e:
                    problems.append(f"{module}: unreadable marker ({e})")
            close = reply.find("]]", end)
            pos = close + 2 if close >= 0 else len(reply)
            if not isinstance(body, dict):
                problems.append(f"{module}: the marker should hold an object")
            elif module not in on:
                problems.append(f"{module} is turned off")
            else:
                actions.append((module, body))
        out.append(reply[pos:])
        return "".join(out).strip(), actions, problems

    LOOKUPS = ("news", "briefing")

    @staticmethod
    def is_lookup(module: str, body: dict[str, Any]) -> bool:
        return module in Household.LOOKUPS or (module == "calendar" and "find" in body)

    def lookup(self, actions: list[tuple[str, dict[str, Any]]], device_id: str, person: str,
               now: datetime | None = None) -> str:
        """What the lookups in a reply found, for the second request."""
        now = now or self.now()
        found = []
        for module, body in actions:
            if module == "news":
                found.append(self.headlines(_clean(body.get("topic"), 60)))
            elif module == "briefing":
                found.append(self.briefing(device_id, person, now))
            elif module == "calendar":
                try:
                    frm = local_dt(parse_when(body.get("find"), now, date_only_ok=True)[:10])
                    to = local_dt(parse_when(body.get("to") or body.get("find"), now, date_only_ok=True)[:10])
                except Problem as e:
                    found.append(f"Calendar: {e}.")
                    continue
                to = max(to, frm) + timedelta(days=1)
                to = min(to, frm + timedelta(days=62))
                events = self.events(frm, to, person)
                found.append(f"Calendar {frm:%Y-%m-%d} to {(to - timedelta(days=1)):%Y-%m-%d}: "
                             + (self.describe_events(events, now) or "nothing") + ".")
        return " ".join(f for f in found if f)

    def headlines(self, topic: str = "", count: int = 8) -> str:
        feeds_ = self.settings().news_feeds
        if not feeds_:
            return "News: no news feeds are set up in Harkenn (Modules page)."
        lines = []
        for link in feeds_:
            for h in (self.news_cache.get(link.url) or [])[:15]:
                if topic and topic.lower() not in (h["title"] + " " + h["summary"]).lower():
                    continue
                lines.append(f"[{link.name}] {h['title']}" + (f" - {h['summary'][:200]}" if h["summary"] else ""))
        if not lines:
            return f"News: no headlines{' about ' + topic if topic else ''} right now."
        return "News headlines: " + " | ".join(lines[:count])

    def briefing(self, device_id: str, person: str, now: datetime) -> str:
        s = self.settings()
        from .. import groups
        device = self.ctx.db.get_device(device_id) or {}
        group = groups.of_device(self.ctx.db, device)["id"] if device else ""
        sections = s.briefing_groups.get(group, s.briefing_sections)
        on = set(self.enabled_modules())
        name = self.person_name(person)
        parts = [f"Morning briefing for {name or 'whoever is there'}, {now:%A %B} {now.day}, {now:%H:%M}."]
        notes = self.notes(device_id, person, now) if on else {}
        if "weather" in sections:
            parts.append(self.ctx.weather.summary().strip() or "Weather: no place is set.")
        if "calendar" in sections and "calendar" in on:
            today = datetime(now.year, now.month, now.day)
            events = self.events(today, today + timedelta(days=1), person)
            parts.append("Today: " + (self.describe_events(events, now) or "nothing on the calendar") + ".")
        if "reminders" in sections and "reminders" in on:
            today = now.strftime("%Y-%m-%d")
            rems = [i["data"] for i in self.items("reminders", person)
                    if not i["data"]["done"] and i["data"]["due"].startswith(today)]
            if rems:
                parts.append("Reminders today: " + "; ".join(f"{r['text']} at {r['due'][11:]}" for r in rems) + ".")
        if "todo" in sections and "todo" in on:
            due = [i["data"]["text"] for i in self.items("todo")
                   if not i["data"]["done"] and i["data"]["due"] and i["data"]["due"] <= now.strftime("%Y-%m-%d")]
            if due:
                parts.append("To-dos due: " + "; ".join(due) + ".")
        if "chores" in sections and "chores" in on:
            chores = [f"{c['name']} ({whose_turn(c) or 'no one'})" for c in (i["data"] for i in self.items("chores"))
                      if chore_due_today(c, now.date(), time.time())]
            if chores:
                parts.append("Chores today: " + "; ".join(chores) + ".")
        if "messages" in sections and "messages" in on and notes.get("messages"):
            parts.append(notes["messages"])
        if "news" in sections and "news" in on and s.news_feeds:
            parts.append(self.headlines(count=3))
        return " ".join(parts)

    async def apply(self, module: str, body: dict[str, Any], device_id: str, person: str,
                    sender: str = "") -> str:
        """Carry out a marker (or MCP tool call). Returns what was done, for the logs; raises Problem."""
        now = self.now()
        if module == "memories":
            if body.get("forget"):
                found = self.find("memories", body["forget"], person)
                for i in found:
                    self.delete(i)
                return f"forgot {len(found)}"
            item = self.add("memories", {"text": body.get("remember") or body.get("text")},
                            self.owner_for(body.get("for"), person))
            return f"remembered \"{item['data']['text']}\""
        if module == "todo":
            if body.get("add"):
                adds = body["add"] if isinstance(body["add"], list) else [body["add"]]
                for text in adds[:20]:
                    self.add("todo", {"text": text, "due": body.get("due")})
                return f"added {len(adds)} to-do"
            for key, change in (("done", {"done": True}), ("undo", {"done": False})):
                if body.get(key):
                    found = self.find("todo", body[key])[:1]
                    if not found:
                        raise Problem(f"nothing on the to-do list like {body[key]!r}")
                    self.update(found[0], change)
                    return f"to-do {key}: {found[0]['data']['text']}"
            if body.get("remove"):
                found = self.find("todo", body["remove"])[:1]
                for i in found:
                    self.delete(i)
                return f"removed {len(found)} to-do"
        if module == "shopping":
            if body.get("add"):
                adds = body["add"] if isinstance(body["add"], list) else [body["add"]]
                have = {i["data"]["text"].lower(): i for i in self.items("shopping")}
                for text in adds[:30]:
                    old = have.get(_clean(text, 100).lower())
                    if old:
                        self.update(old, {"got": False})
                    else:
                        self.add("shopping", {"text": text})
                return f"added {len(adds)} to the shopping list"
            if body.get("got"):
                gots = body["got"] if isinstance(body["got"], list) else [body["got"]]
                n = 0
                for text in gots[:30]:
                    for i in self.find("shopping", text)[:1]:
                        self.update(i, {"got": True})
                        n += 1
                return f"got {n}"
            if body.get("remove"):
                found = self.find("shopping", body["remove"])[:1]
                for i in found:
                    self.delete(i)
                return f"removed {len(found)} from the shopping list"
            if body.get("clear"):
                got = [i for i in self.items("shopping") if i["data"]["got"]]
                for i in got:
                    self.delete(i)
                return f"cleared {len(got)} got"
        if module == "reminders":
            if body.get("cancel"):
                found = self.find("reminders", body["cancel"], person, where=lambda i: not i["data"]["done"])
                if not found:
                    raise Problem(f"no reminder like {body['cancel']!r}")
                for i in found[:1]:
                    self.delete(i)
                return f"cancelled reminder {found[0]['data']['text']}"
            due = body.get("at") or body.get("due")
            if body.get("in"):
                seconds = timers.duration_s(body["in"])
                if seconds <= 0:
                    raise Problem(f"can't read {body['in']!r}")
                due = (now + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M")
            speaker = _clean(body.get("speaker"), 40)
            if speaker and speaker.lower() != "all":
                found = [d for d in self.ctx.db.list_devices() if speaker.lower() in (d["id"], d["name"].lower())]
                if not found:
                    raise Problem(f"no speaker called {speaker!r}")
                speaker = found[0]["id"]
            item = self.add("reminders", {"text": body.get("text"), "due": due, "repeat": body.get("repeat"),
                                          "alarm": body.get("alarm"), "speaker": speaker or device_id},
                            self.owner_for(body.get("for"), person))
            return f"reminder \"{item['data']['text']}\" at {item['data']['due']}"
        if module == "calendar":
            if body.get("remove"):
                when = str(body.get("date") or "")[:10]
                found = self.find("calendar", body["remove"], person,
                                  where=lambda i: not when or i["data"]["start"].startswith(when) or i["data"]["repeat"])
                if not found:
                    raise Problem(f"no event like {body['remove']!r}")
                self.delete(found[0])
                return f"removed event {found[0]['data']['title']}"
            if body.get("add"):
                item = self.add("calendar", {"title": body["add"], "start": body.get("start"), "end": body.get("end"),
                                             "where": body.get("where"), "repeat": body.get("repeat"),
                                             "notes": body.get("notes")}, self.owner_for(body.get("for"), person))
                return f"added event {item['data']['title']} {item['data']['start']}"
            return ""  # a lookup
        if module == "messages":
            if body.get("delivered"):
                to = _clean(body["delivered"], 40).lower()
                for i in self.items("messages"):
                    if i["data"]["to"].lower() == to and not i["data"].get("delivered_at"):
                        self.update(i, {"delivered_at": time.time()})
                return f"messages for {body['delivered']} delivered"
            to = _clean(body.get("to"), 40)
            known = next((p["name"] for p in self.people() if p["name"].lower() == to.lower()), to)
            item = self.add("messages", {"to": known, "from": sender or self.person_name(person),
                                         "text": body.get("text")})
            if body.get("speaker"):
                await self.show(str(body["speaker"]), f"For {known}", item["data"]["text"])
            return f"message for {known}"
        if module == "chores":
            if body.get("done"):
                found = self.find("chores", body["done"])[:1]
                if not found:
                    raise Problem(f"no chore like {body['done']!r}")
                c = found[0]["data"]
                by = _clean(body.get("by"), 40) or whose_turn(c)
                people = [p.lower() for p in c["people"]]
                turn = (people.index(by.lower()) + 1) if by.lower() in people else int(c["turn"]) + 1
                self.update(found[0], {"turn": turn, "last_done": time.time(), "last_by": by})
                return f"chore {c['name']} done by {by}"
        if self.is_lookup(module, body):
            return ""
        raise Problem(f"{module}: nothing to do in {json.dumps(body)[:80]}")

    # showing things on speakers

    def _targets(self, speaker: str) -> list[dict[str, Any]]:
        devices = self.ctx.db.list_devices()
        if not speaker or speaker.lower() == "all":
            return devices
        return [d for d in devices if speaker.lower() in (d["id"], d["name"].lower())]

    async def show(self, speaker: str, title: str, text: str, say: str = "", alarm: bool = False) -> list[str]:
        """A notification card (and maybe speech, or an alarm's ring) on a speaker or all of them. Returns where."""
        shown = []
        for d in self._targets(speaker):
            if not self.ctx.hub.online(d["id"]):
                continue
            self.ctx.board.notify(d["id"], title, text)
            await self.ctx.hub.send(d["id"], {"type": "notify", "title": title[:60], "text": text[:300],
                                              "seconds": 20})
            if alarm:  # a 1-second timer: the speaker rings it, with Stop
                await self.ctx.hub.send(d["id"], {"type": "timer", "action": "set", "kind": "timer", "seconds": 1,
                                                  "label": text[:40]})
            elif say:
                await self.ctx.voice.say(d["id"], say)
            self.ctx.hub.publish({"type": "device", "id": d["id"]})
            shown.append(d["name"])
        return shown

    # the clock: reminders, chore reminders, tidying up

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("household tick failed")
            await asyncio.sleep(TICK_S)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or self.now()
        on = set(self.enabled_modules())
        s = self.settings()
        if "reminders" in on:
            for item in self.items("reminders"):
                d = item["data"]
                if d["done"] or local_dt(d["due"]) > now:
                    continue
                whose = self.person_name(item["owner"])
                title = f"Reminder for {whose}" if whose else "Reminder"
                shown = await self.show(d["speaker"] or "all", title, d["text"],
                                        say=f"{title}: {d['text']}" if s.reminders_speak else "", alarm=d["alarm"])
                if not shown and d["speaker"] and d["speaker"] != "all":  # its speaker is offline: anywhere
                    shown = await self.show("all", title, d["text"],
                                            say=f"{title}: {d['text']}" if s.reminders_speak else "",
                                            alarm=d["alarm"])
                log.info("reminder %r shown on %s", d["text"], ", ".join(shown) or "nothing (all offline)")
                if d["repeat"]:
                    nxt = local_dt(d["due"])
                    while nxt <= now:
                        nxt = next_repeat(nxt, d["repeat"])
                    self.update(item, {"due": nxt.strftime("%Y-%m-%dT%H:%M"), "fired_at": time.time()})
                else:
                    self.update(item, {"done": True, "fired_at": time.time()})
        if "chores" in on:
            today = now.strftime("%Y-%m-%d")
            for item in self.items("chores"):
                c = item["data"]
                if (not c["remind"] or c.get("reminded_on") == today or now.strftime("%H:%M") < c["remind"]
                        or not chore_due_today(c, now.date(), time.time())):
                    continue
                turn = whose_turn(c)
                await self.show("all", c["name"], f"{turn}'s turn" if turn else "Today",
                                say=f"{c['name']}: it's {turn}'s turn today." if turn and s.reminders_speak else "")
                self.update(item, {"reminded_on": today})
        # what's done stays a while, for the admin pages, then goes
        old = time.time() - DONE_KEEP_S
        for module, key in (("todo", "done_at"), ("reminders", "fired_at"), ("messages", "delivered_at"),
                            ("shopping", "got_at")):
            for item in self.ctx.db.household_items(module):
                d = item["data"]
                finished = d.get("done", True) if module in ("todo", "reminders") else True
                if finished and d.get(key) and d[key] < old and not d.get("repeat"):
                    self.ctx.db.delete_household_item(item["id"])

    async def alarm_stopped(self, device_id: str, data: dict[str, Any]) -> None:
        """A morning alarm was stopped on a speaker: the briefing, if that's on."""
        s = self.settings()
        if not (self.enabled("briefing", s) and s.briefing_after_alarm) or data.get("kind") != "alarm":
            return
        now = self.now()
        if not 4 <= now.hour < 11:
            return
        await self.ctx.voice.proactive(device_id, "(The morning alarm was just stopped. Give the morning briefing.)")
